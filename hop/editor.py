from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from tempfile import gettempdir
from typing import Protocol, Sequence

from hop.errors import HopError
from hop.kitty import KittyTransport, SocketKittyTransport
from hop.session import ProjectSession
from hop.sway import SwayIpcAdapter, SwayWindow

NVIM_COMMAND = "nvim"
EDITOR_ROLE = "editor"
HOP_EDITOR_VAR = "hop_editor"
EDITOR_READY_TIMEOUT_SECONDS = 5.0
EDITOR_READY_POLL_INTERVAL_SECONDS = 0.05
DEFAULT_REMOTE_CHECK_EXPRESSION = "1"


class NeovimError(HopError):
    """Base error for Neovim lifecycle failures."""


class NeovimCommandError(NeovimError):
    """Raised when hop cannot start or control the shared Neovim instance."""


class ProcessRunner(Protocol):
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


class EditorSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def focus_window(self, window_id: int) -> None: ...


class SharedNeovimEditorAdapter:
    def __init__(
        self,
        *,
        sway: EditorSwayAdapter | None = None,
        kitty_transport: KittyTransport | None = None,
        process_runner: ProcessRunner | None = None,
        runtime_dir: Path | str | None = None,
        ready_timeout_seconds: float = EDITOR_READY_TIMEOUT_SECONDS,
        ready_poll_interval_seconds: float = EDITOR_READY_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._sway: EditorSwayAdapter = sway or SwayIpcAdapter()
        self._transport: KittyTransport = kitty_transport or SocketKittyTransport()
        self._process_runner = process_runner or _SubprocessRunner()
        self._runtime_dir = _resolve_runtime_dir(runtime_dir)
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
        app_id = _editor_os_window_name(session)
        matches = [
            window
            for window in self._sway.list_windows()
            if window.app_id == app_id or window.window_class == app_id
        ]
        if not matches:
            msg = f"Sway has no editor window for session {session.session_name!r}."
            raise NeovimCommandError(msg)
        window = min(matches, key=lambda candidate: candidate.id)
        self._sway.focus_window(window.id)

    def _launch_editor(self, session: ProjectSession, *, address: Path) -> None:
        self._transport.send_command(
            "launch",
            {
                "args": [NVIM_COMMAND, "--listen", str(address)],
                "cwd": str(session.project_root),
                "type": "os-window",
                "keep_focus": False,
                "allow_remote_control": True,
                "window_title": EDITOR_ROLE,
                "os_window_title": EDITOR_ROLE,
                "os_window_name": _editor_os_window_name(session),
                "var": [f"{HOP_EDITOR_VAR}=1"],
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
        root_hash = hashlib.sha256(str(session.project_root).encode()).hexdigest()[:16]
        return self._runtime_dir / f"hop-{root_hash}.sock"


class _SubprocessRunner:
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            check=False,
        )


def _resolve_runtime_dir(runtime_dir: Path | str | None) -> Path:
    if runtime_dir is not None:
        path = Path(runtime_dir).expanduser().resolve()
    else:
        runtime_root = os.environ.get("XDG_RUNTIME_DIR") or gettempdir()
        path = Path(runtime_root).expanduser().resolve() / "hop"

    path.mkdir(parents=True, exist_ok=True)
    return path


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


def _editor_os_window_name(session: ProjectSession) -> str:
    return f"hop:{session.session_name}:{EDITOR_ROLE}"
