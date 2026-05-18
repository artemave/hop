"""Bridge acceptor: HTTP-over-unix-socket entry point for hop CLI calls.

Editor plugins running inside devcontainer/ssh backends use this socket
to call back to host hop. See
``.dust/tasks/add-host-side-bridge-acceptor.md`` for the design.
"""

from __future__ import annotations

import base64
import os
import socketserver
import subprocess
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable, Sequence, cast

from hop.commands.session import SESSION_WORKSPACE_PREFIX
from hop.editor import EDITOR_MARK_PREFIX
from hop.session import ProjectSession
from hop.state import load_sessions
from hop.sway import SwayWindow

SwaySource = Callable[[], Sequence[SwayWindow]]
Dispatcher = Callable[[ProjectSession, Sequence[str]], "CompletedProcess[bytes]"]


# POSIX-sh client for the bridge acceptor. Rendered by
# ``render_bridge_shim()`` and printed verbatim by ``hop bridge shim``;
# install into the backend at ``/usr/local/bin/hop``. Inside the backend
# the shim's socket path is ``${HOP_SOCKET:-<baked default>}``. The
# default is set at render time from ``--socket`` (or
# ``BRIDGE_SHIM_DEFAULT_SOCKET`` if unspecified) so a recipe can bake the
# host's actual runtime path in and skip touching the container's compose
# environment.
#
# Dependencies inside the backend: ``curl``, ``awk``, ``base64``,
# ``mktemp``, ``tr`` — all coreutils-universal or near-universal in dev
# container base images.
BRIDGE_SHIM_DEFAULT_SOCKET = "/run/hop.sock"

_BRIDGE_SHIM_TEMPLATE = r"""#!/bin/sh
sock=${HOP_SOCKET:-__SOCKET_DEFAULT__}
hdr=$(mktemp) || exit 2
body=$(mktemp) || { rm -f "$hdr"; exit 2; }
trap 'rm -f "$hdr" "$body"' EXIT

status=$(printf '%s\0' "$0" "$@" | curl -sS --unix-socket "$sock" \
    -D "$hdr" -o "$body" -w '%{http_code}' \
    --data-binary @- "http://_/call") || exit 2

case "$status" in
    200)
        ec=$(awk 'tolower($1)=="x-hop-exit:" {print $2+0; exit}' "$hdr")
        err=$(awk 'tolower($1)=="x-hop-stderr:" {print $2; exit}' "$hdr" | tr -d '\r')
        if [ -n "$err" ]; then
            printf '%s' "$err" | base64 -d >&2
        fi
        cat "$body"
        exit "${ec:-0}"
        ;;
    *)
        cat "$body" >&2
        exit 1
        ;;
esac
"""


def render_bridge_shim(socket_default: str = BRIDGE_SHIM_DEFAULT_SOCKET) -> str:
    """Render the POSIX-sh client with the given default socket path baked in."""

    return _BRIDGE_SHIM_TEMPLATE.replace("__SOCKET_DEFAULT__", socket_default)


# Default-rendered shim text — convenience for callers that don't customize the
# socket path (tests, anything that wants to inspect the canonical script).
BRIDGE_SHIM = render_bridge_shim()


def default_api_socket_path() -> Path:
    """Canonical bridge socket path under ``$XDG_RUNTIME_DIR/hop``."""

    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "hop" / "api.sock"


class BridgeError(Exception):
    """Internal signal — translated to a 4xx/5xx response by the handler."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def resolve_session_from_focus(
    sway_source: SwaySource,
    *,
    sessions_dir: Path | None = None,
) -> ProjectSession:
    """Resolve the focused Sway window to a hop session.

    Resolution order:

    1. ``_hop_editor:<session>`` mark on the focused window (set by the editor
       adapter; most specific — the editor window may have drifted off its
       session workspace and the mark still identifies it).
    2. The focused window's workspace name matches ``p:<session>``. Covers
       kitty role terminals (test/server/console/…) and any other window the
       user happens to be on while inside a session workspace.

    Raises ``BridgeError(400, ...)`` if neither path yields a session known to
    ``load_sessions()``.
    """

    windows = sway_source()
    focused = next((window for window in windows if window.focused), None)
    if focused is None:
        raise BridgeError(400, "no focused Sway window")

    sessions = load_sessions(sessions_dir=sessions_dir)

    candidate_name: str | None = None
    editor_mark = next(
        (mark for mark in focused.marks if mark.startswith(EDITOR_MARK_PREFIX)),
        None,
    )
    if editor_mark is not None:
        candidate_name = editor_mark[len(EDITOR_MARK_PREFIX) :]
    elif focused.workspace_name and focused.workspace_name.startswith(SESSION_WORKSPACE_PREFIX):
        candidate_name = focused.workspace_name[len(SESSION_WORKSPACE_PREFIX) :]

    if candidate_name is None:
        raise BridgeError(
            400,
            "focused window is neither a hop editor nor on a session workspace",
        )

    state = sessions.get(candidate_name)
    if state is None:
        raise BridgeError(
            400,
            f"session {candidate_name!r} from focused window is not in hop state",
        )
    # Construct directly from persisted state — re-deriving via
    # ``resolve_project_session`` would recompute ``session_name`` from
    # ``project_root.name``, which doesn't match in tests and is redundant in
    # production (the persisted name is already the canonical value).
    return ProjectSession(
        project_root=state.project_root,
        session_name=state.name,
        workspace_name=f"{SESSION_WORKSPACE_PREFIX}{state.name}",
    )


def dispatch_via_subprocess(
    session: ProjectSession,
    argv: Sequence[str],
) -> CompletedProcess[bytes]:
    """Production dispatcher: run ``hop`` as a subprocess rooted at the session."""

    return subprocess.run(
        [sys.executable, "-m", "hop", *argv],
        cwd=session.project_root,
        input=b"",
        capture_output=True,
        check=False,
    )


class BridgeRequestHandler(BaseHTTPRequestHandler):
    @property
    def bridge_server(self) -> "BridgeServer":
        return cast("BridgeServer", self.server)

    def log_message(self, format: str, *args: object) -> None:
        # Suppress per-request stderr noise. The acceptor runs inside hopd,
        # which has its own debug-log channel for anything worth recording.
        del format, args

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler hook name
        if self.path != "/call":
            self._send_text(404, f"unknown path {self.path!r}")
            return
        content_length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(content_length) if content_length else b""
        argv = [piece.decode("utf-8", errors="replace") for piece in body.split(b"\x00") if piece]
        # First element is the shim's $0 — ignored.
        hop_argv = argv[1:]
        bridge_server = self.bridge_server
        try:
            session = resolve_session_from_focus(
                bridge_server.sway_source,
                sessions_dir=bridge_server.sessions_dir,
            )
            result = bridge_server.dispatcher(session, hop_argv)
        except BridgeError as error:
            self._send_text(error.status, error.message)
            return
        except Exception as error:
            self._send_text(500, f"bridge dispatch failed: {error}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(result.stdout)))
        self.send_header("X-Hop-Exit", str(result.returncode))
        self.send_header(
            "X-Hop-Stderr",
            base64.b64encode(result.stderr).decode("ascii"),
        )
        self.end_headers()
        self.wfile.write(result.stdout)

    def _send_text(self, status: int, message: str) -> None:
        body = (message + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BridgeServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        socket_path: str,
        sway_source: SwaySource,
        dispatcher: Dispatcher,
        *,
        sessions_dir: Path | None = None,
    ) -> None:
        super().__init__(socket_path, BridgeRequestHandler)
        self.sway_source = sway_source
        self.dispatcher = dispatcher
        self.sessions_dir = sessions_dir


def serve_forever(
    socket_path: Path | str,
    sway_source: SwaySource,
    dispatcher: Dispatcher,
    *,
    sessions_dir: Path | None = None,
) -> None:
    """Bind ``socket_path`` (unlinking any stale entry) and serve until shutdown."""

    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists() or socket_path.is_symlink():
        socket_path.unlink()
    with BridgeServer(
        str(socket_path),
        sway_source=sway_source,
        dispatcher=dispatcher,
        sessions_dir=sessions_dir,
    ) as server:
        server.serve_forever()
