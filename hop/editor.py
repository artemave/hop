from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence

from hop.backends import HostBackend, SessionBackend
from hop.errors import HopError
from hop.kitty import KittyTransport, SocketKittyTransport
from hop.session import ProjectSession
from hop.sway import SwayIpcAdapter, SwayWindow

NVIM_COMMAND = "nvim"
EDITOR_ROLE = "editor"
EDITOR_OS_WINDOW_NAME = f"hop:{EDITOR_ROLE}"
EDITOR_MARK_PREFIX = "_hop_editor:"
EDITOR_READY_TIMEOUT_SECONDS = 5.0
EDITOR_READY_POLL_INTERVAL_SECONDS = 0.05
DEFAULT_REMOTE_CHECK_EXPRESSION = "1"

SessionBackendFactory = Callable[[ProjectSession], SessionBackend]


class NeovimError(HopError):
    """Base error for Neovim lifecycle failures."""


class NeovimCommandError(NeovimError):
    """Raised when hop cannot start or control the shared Neovim instance."""


class ProcessRunner(Protocol):
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


class EditorSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def focus_window(self, window_id: int) -> None: ...

    def mark_window(self, window_id: int, mark: str) -> None: ...


class SharedNeovimEditorAdapter:
    def __init__(
        self,
        *,
        sway: EditorSwayAdapter | None = None,
        kitty_transport: KittyTransport | None = None,
        process_runner: ProcessRunner | None = None,
        session_backend_for: SessionBackendFactory | None = None,
        ready_timeout_seconds: float = EDITOR_READY_TIMEOUT_SECONDS,
        ready_poll_interval_seconds: float = EDITOR_READY_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._sway: EditorSwayAdapter = sway or SwayIpcAdapter()
        self._transport: KittyTransport = kitty_transport or SocketKittyTransport()
        self._process_runner = process_runner or _SubprocessRunner()
        self._session_backend_for: SessionBackendFactory = session_backend_for or (lambda _session: HostBackend())
        self._ready_timeout_seconds = ready_timeout_seconds
        self._ready_poll_interval_seconds = ready_poll_interval_seconds

    def focus(self, session: ProjectSession) -> None:
        self._ensure_editor(session)
        self._focus_editor_window(session)

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        self._ensure_editor(session)
        self._focus_editor_window(session)
        self._send_remote_keys(session, build_remote_open_command(target))

    def _ensure_editor(self, session: ProjectSession) -> None:
        address = self._remote_address(session)
        if self._server_is_running(address):
            return

        _remove_stale_socket(address)
        self._launch_editor(session, address=address)
        self._wait_for_server(address)

    def _focus_editor_window(self, session: ProjectSession) -> None:
        # Sway-driven focus (rather than Kitty's `focus-window`) so the focus
        # change escalates to a workspace switch when the editor lives on a
        # different Sway workspace than the caller — e.g. when the kitten
        # dispatches a file or URL from a terminal session.
        window = self._find_editor_window(session)
        if window is None:
            msg = f"Sway has no editor window for session {session.session_name!r}."
            raise NeovimCommandError(msg)
        self._sway.focus_window(window.id)

    def _find_editor_window(self, session: ProjectSession) -> SwayWindow | None:
        # The session's editor is identified across hop runs by a Sway mark.
        # On first sighting (or after a hop crash that lost the mark) fall back
        # to discovering the unmarked editor on this session's workspace, then
        # re-mark it for fast lookup later — and to survive drift onto other
        # workspaces.
        mark = _editor_mark(session)
        windows = list(self._sway.list_windows())

        marked = [window for window in windows if mark in window.marks]
        if marked:
            return min(marked, key=lambda candidate: candidate.id)

        candidates = [
            window
            for window in windows
            if (window.app_id == EDITOR_OS_WINDOW_NAME or window.window_class == EDITOR_OS_WINDOW_NAME)
            and window.workspace_name == session.workspace_name
            and not any(other_mark.startswith(EDITOR_MARK_PREFIX) for other_mark in window.marks)
        ]
        if not candidates:
            return None

        window = min(candidates, key=lambda candidate: candidate.id)
        self._sway.mark_window(window.id, mark)
        return window

    def _launch_editor(self, session: ProjectSession, *, address: Path) -> None:
        backend = self._session_backend_for(session)
        self._transport.send_command(
            "launch",
            {
                "args": list(backend.editor_args(session, address)),
                "cwd": str(session.project_root),
                "type": "os-window",
                "keep_focus": False,
                "allow_remote_control": True,
                "window_title": EDITOR_ROLE,
                "os_window_title": EDITOR_ROLE,
                "os_window_name": EDITOR_OS_WINDOW_NAME,
            },
        )

    def _server_is_running(self, address: Path) -> bool:
        result = self._process_runner.run(
            [
                NVIM_COMMAND,
                "--server",
                str(address),
                "--remote-expr",
                DEFAULT_REMOTE_CHECK_EXPRESSION,
            ]
        )
        return result.returncode == 0

    def _wait_for_server(self, address: Path) -> None:
        deadline = time.monotonic() + self._ready_timeout_seconds
        while time.monotonic() < deadline:
            if self._server_is_running(address):
                return
            time.sleep(self._ready_poll_interval_seconds)

        msg = f"Neovim did not become ready at {address!s}."
        raise NeovimCommandError(msg)

    def _send_remote_keys(self, session: ProjectSession, keys: str) -> None:
        address = self._remote_address(session)
        result = self._process_runner.run(
            [
                NVIM_COMMAND,
                "--server",
                str(address),
                "--remote-send",
                keys,
            ]
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            msg = stderr or f"Could not send keys to Neovim at {address!s}."
            raise NeovimCommandError(msg)

    def _remote_address(self, session: ProjectSession) -> Path:
        return self._session_backend_for(session).editor_remote_address(session)


class _SubprocessRunner:
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            check=False,
        )


def build_remote_open_command(target: str) -> str:
    path_text, line_number = _split_target(target)
    escaped_path = _quote_vimscript_string(path_text)
    commands = [f"<Cmd>execute 'drop ' . fnameescape('{escaped_path}')<CR>"]
    if line_number is not None:
        commands.append(f"<Cmd>{line_number}<CR>")
    return "".join(commands)


def _split_target(target: str) -> tuple[str, int | None]:
    path_text, separator, suffix = target.rpartition(":")
    if separator and suffix.isdigit() and path_text:
        return path_text, int(suffix)
    return target, None


def _quote_vimscript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


def _remove_stale_socket(address: Path) -> None:
    if address.exists() or address.is_socket():
        address.unlink(missing_ok=True)


def _editor_mark(session: ProjectSession) -> str:
    return f"{EDITOR_MARK_PREFIX}{session.session_name}"
