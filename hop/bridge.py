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
from hop.session import ProjectSession
from hop.state import load_sessions
from hop.sway import SwayWindow

SwaySource = Callable[[], Sequence[SwayWindow]]
Dispatcher = Callable[[ProjectSession, Sequence[str]], "CompletedProcess[bytes]"]
RemoteDispatcher = Callable[[str, str, Sequence[str]], "CompletedProcess[bytes]"]
SessionlessDispatcher = Callable[[Sequence[str]], "CompletedProcess[bytes]"]

# Stateless ``hop`` subcommands that don't act on a session — they run
# regardless of which window is focused. The shim is a transparent proxy for
# these (e.g. a recipe running ``hop bridge shim`` on a remote where ``hop`` is
# the shim still gets the real shim text back from the host).
SESSIONLESS_COMMANDS = frozenset({"bridge", "path"})


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

# Default ssh host baked into a shim. ``hop ssh <host>`` bakes the real target so
# the remote-machine shim reports which host it came from; the recipe-installed
# in-container shim leaves it empty (the acceptor then resolves the session from
# the host's focused window, as before). Overridable at run time via
# ``$HOP_SSH_HOST``.
BRIDGE_SHIM_DEFAULT_HOST = ""

_BRIDGE_SHIM_TEMPLATE = r"""#!/bin/sh
sock=${HOP_SOCKET:-__SOCKET_DEFAULT__}
host=${HOP_SSH_HOST:-__HOST_DEFAULT__}
hdr=$(mktemp) || exit 2
body=$(mktemp) || { rm -f "$hdr"; exit 2; }
trap 'rm -f "$hdr" "$body"' EXIT

status=$(printf '%s\0' "$host" "$(pwd)" "$0" "$@" | curl -sS --unix-socket "$sock" \
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


def render_bridge_shim(
    socket_default: str = BRIDGE_SHIM_DEFAULT_SOCKET,
    host_default: str = BRIDGE_SHIM_DEFAULT_HOST,
) -> str:
    """Render the POSIX-sh client with the default socket + ssh host baked in."""

    return _BRIDGE_SHIM_TEMPLATE.replace("__SOCKET_DEFAULT__", socket_default).replace("__HOST_DEFAULT__", host_default)


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

    The focused window's workspace name must match ``p:<session>``. That covers
    every kitty role terminal — shell, editor, test/server/console/… — since
    they all live on the session workspace, plus any other window the user
    happens to be on while inside a session workspace.

    Raises ``BridgeError(400, ...)`` if the focused window isn't on a session
    workspace, or names a session unknown to ``load_sessions()``.
    """

    windows = sway_source()
    focused = next((window for window in windows if window.focused), None)
    if focused is None:
        raise BridgeError(400, "no focused Sway window")

    sessions = load_sessions(sessions_dir=sessions_dir)

    if not (focused.workspace_name and focused.workspace_name.startswith(SESSION_WORKSPACE_PREFIX)):
        raise BridgeError(400, "focused window is not on a session workspace")
    candidate_name = focused.workspace_name[len(SESSION_WORKSPACE_PREFIX) :]

    state = sessions.get(candidate_name)
    if state is None:
        raise BridgeError(
            400,
            f"session {candidate_name!r} from focused window is not in hop state",
        )
    # Construct directly from persisted state — re-deriving via
    # ``resolve_project_session`` would recompute ``session_name`` from
    # ``session_root.name``, which doesn't match in tests and is redundant in
    # production (the persisted name is already the canonical value). ``host``
    # rides along so a remote session's dispatch runs over the transport rather
    # than locally against a path that only exists on the remote.
    return ProjectSession(
        session_root=state.session_root,
        session_name=state.name,
        workspace_name=f"{SESSION_WORKSPACE_PREFIX}{state.name}",
        host=state.backend.transport_host,
    )


def dispatch_via_subprocess(
    session: ProjectSession,
    argv: Sequence[str],
    *,
    runner: Callable[..., "CompletedProcess[bytes]"] = subprocess.run,
) -> CompletedProcess[bytes]:
    """Production dispatcher: run ``hop`` as a subprocess for the focused session.

    A local session roots the subprocess at its project dir. A *remote* session's
    project dir only exists on the remote, so root the subprocess in the local
    home and pass identity via ``HOP_REMOTE_HOST`` / ``HOP_REMOTE_CWD`` — the
    command paths rebuild the remote session from those (``remote_session_from_env``)
    and drive the backend over the transport. This is what makes in-container
    ``hop open`` / ``hop run`` work for a remote devcontainer session.
    """

    env = dict(os.environ)
    cwd: Path = session.session_root
    if session.host is not None:
        env["HOP_REMOTE_HOST"] = session.host
        env["HOP_REMOTE_CWD"] = str(session.session_root)
        cwd = Path.home()
    return runner(
        [sys.executable, "-m", "hop", *argv],
        cwd=cwd,
        input=b"",
        capture_output=True,
        env=env,
        check=False,
    )


def dispatch_sessionless(argv: Sequence[str]) -> CompletedProcess[bytes]:
    """Run a stateless ``hop`` subcommand on the host, independent of any session.

    For ``bridge shim`` / ``path`` there's nothing to resolve from focus — the
    command is a pure function of its args — so run ``hop <argv>`` from the home
    directory and return its output. This is what lets a remote recipe's
    ``hop bridge shim`` (where ``hop`` is the shim) get the real shim text back.
    """

    return subprocess.run(
        [sys.executable, "-m", "hop", *argv],
        cwd=str(Path.home()),
        input=b"",
        capture_output=True,
        check=False,
    )


def dispatch_remote(
    host: str,
    cwd: str,
    argv: Sequence[str],
    *,
    runner: Callable[..., "CompletedProcess[bytes]"] = subprocess.run,
) -> CompletedProcess[bytes]:
    """Run ``hop <argv>`` for a remote session reported by the ``hop ssh`` shim.

    The shim runs on a remote machine and reports ``(host, cwd)``; the ``cwd``
    identifies the session exactly as it does for a local ``hop`` (no focus
    needed), and there is no local directory to root the subprocess in (``cwd``
    is a path on ``host``). So ``hop`` runs from the local home with the identity
    passed via environment — the CLI rebuilds the remote ``ProjectSession`` from
    it (``remote_session_from_env``) for *any* command: enter (empty argv),
    ``kill``, ``run``, ``open``, … all over the ssh transport.
    """

    env = dict(os.environ)
    env["HOP_REMOTE_HOST"] = host
    env["HOP_REMOTE_CWD"] = cwd
    return runner(
        [sys.executable, "-m", "hop", *argv],
        cwd=str(Path.home()),
        input=b"",
        capture_output=True,
        env=env,
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
        # Frame: host \0 cwd \0 $0 \0 *args. host/cwd are always positional
        # (either may be an empty string, so they can't be filtered); $0 is the
        # shim's own name and is ignored; the hop args are everything after it,
        # with the trailing empty field that printf '%s\0' leaves dropped.
        raw = body.split(b"\x00")
        host = raw[0].decode("utf-8", errors="replace") if raw else ""
        cwd = raw[1].decode("utf-8", errors="replace") if len(raw) > 1 else ""
        hop_argv = [piece.decode("utf-8", errors="replace") for piece in raw[3:] if piece]
        bridge_server = self.bridge_server
        try:
            if host:
                # Remote-machine shim (`hop ssh` installed it with the host baked):
                # the user ran `hop <cmd>` in a remote shell, so the cwd identifies
                # the session exactly as for a local `hop` — route by (host, cwd),
                # not the laptop's focused window. Covers enter, kill, run, open, …
                result = bridge_server.remote_dispatcher(host, cwd, hop_argv)
            elif hop_argv and hop_argv[0] in SESSIONLESS_COMMANDS:
                # Stateless command (e.g. `hop bridge shim`): no session to
                # resolve, so run it directly rather than requiring focus.
                result = bridge_server.sessionless_dispatcher(hop_argv)
            else:
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
        remote_dispatcher: RemoteDispatcher = dispatch_remote,
        sessionless_dispatcher: SessionlessDispatcher = dispatch_sessionless,
    ) -> None:
        super().__init__(socket_path, BridgeRequestHandler)
        self.sway_source = sway_source
        self.dispatcher = dispatcher
        self.sessions_dir = sessions_dir
        self.remote_dispatcher = remote_dispatcher
        self.sessionless_dispatcher = sessionless_dispatcher


def serve_forever(
    socket_path: Path | str,
    sway_source: SwaySource,
    dispatcher: Dispatcher,
    *,
    sessions_dir: Path | None = None,
    remote_dispatcher: RemoteDispatcher = dispatch_remote,
    sessionless_dispatcher: SessionlessDispatcher = dispatch_sessionless,
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
        remote_dispatcher=remote_dispatcher,
        sessionless_dispatcher=sessionless_dispatcher,
    ) as server:
        server.serve_forever()
