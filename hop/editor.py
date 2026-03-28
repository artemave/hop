from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import gettempdir
from typing import Any, Mapping, Protocol, Sequence, cast

from hop.errors import HopError
from hop.kitty import KittyCommandError, KittyRemoteControlAdapter, KittyTransport
from hop.session import ProjectSession

NVIM_COMMAND = "nvim"
EDITOR_ROLE = "editor"
HOP_EDITOR_ENV_VAR = "HOP_EDITOR"
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


@dataclass(frozen=True, slots=True)
class EditorWindow:
    id: int
    session_name: str | None
    is_editor: bool
    project_root: Path | None


class SharedNeovimEditorAdapter:
    def __init__(
        self,
        *,
        kitty_transport: KittyTransport | None = None,
        process_runner: ProcessRunner | None = None,
        runtime_dir: Path | str | None = None,
        ready_timeout_seconds: float = EDITOR_READY_TIMEOUT_SECONDS,
        ready_poll_interval_seconds: float = EDITOR_READY_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._kitty = KittyRemoteControlAdapter(transport=kitty_transport)
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
        window = self._find_editor_window(session)
        if window is None:
            return
        self._kitty.send_command("focus-window", {"match": f"id:{window.id}"})

    def _launch_editor(self, session: ProjectSession, *, address: Path) -> None:
        self._kitty.send_command(
            "launch",
            {
                "args": [NVIM_COMMAND, "--listen", str(address)],
                "cwd": str(session.project_root),
                "type": "os-window",
                "keep_focus": False,
                "allow_remote_control": True,
                "window_title": _editor_window_title(session),
                "os_window_title": _editor_window_title(session),
                "os_window_name": _editor_os_window_name(session),
                "env": [
                    f"HOP_SESSION={session.session_name}",
                    f"HOP_PROJECT_ROOT={session.project_root}",
                    f"{HOP_EDITOR_ENV_VAR}=1",
                ],
                "var": [
                    f"hop_session={session.session_name}",
                    f"hop_project_root={session.project_root}",
                    f"{HOP_EDITOR_VAR}=1",
                ],
            },
        )

    def _find_editor_window(self, session: ProjectSession) -> EditorWindow | None:
        response = self._kitty.send_command("ls", {"output_format": "json"})
        payload = _coerce_response_data(response)
        if not isinstance(payload, list):
            raise KittyCommandError("Kitty returned an invalid window listing.")

        windows: list[EditorWindow] = []
        for os_window in cast(list[Any], payload):
            if not isinstance(os_window, Mapping):
                continue
            for tab in cast(Any, os_window).get("tabs", ()):
                if not isinstance(tab, Mapping):
                    continue
                for window_entry in cast(Any, tab).get("windows", ()):
                    if not isinstance(window_entry, Mapping):
                        continue
                    window = _parse_editor_window(cast(Any, window_entry))
                    if window is None:
                        continue
                    if window.project_root == session.project_root and window.is_editor:
                        windows.append(window)

        if not windows:
            return None

        return min(windows, key=lambda window: window.id)

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


def _coerce_response_data(response: object) -> Any:
    if isinstance(response, Mapping):
        data = cast(Any, response).get("data")
    else:
        data = response

    if data is None:
        return []

    if isinstance(data, str):
        return json.loads(data)

    return data


def _parse_editor_window(window_entry: Mapping[str, object]) -> EditorWindow | None:
    window_id = window_entry.get("id")
    if not isinstance(window_id, int):
        return None

    user_vars = _coerce_string_mapping(
        window_entry.get("user_vars") or window_entry.get("user_variables") or window_entry.get("vars")
    )
    env = _coerce_string_mapping(window_entry.get("env"))

    editor_flag = user_vars.get(HOP_EDITOR_VAR) or env.get(HOP_EDITOR_ENV_VAR)
    session_name = user_vars.get("hop_session") or env.get("HOP_SESSION")
    project_root_text = user_vars.get("hop_project_root") or env.get("HOP_PROJECT_ROOT")

    return EditorWindow(
        id=window_id,
        session_name=session_name,
        is_editor=editor_flag == "1",
        project_root=_path_from_text(project_root_text),
    )


def _path_from_text(value: str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser().resolve(strict=False)


def _coerce_string_mapping(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        m = cast(Any, value)
        return {str(key): str(item) for key, item in m.items() if isinstance(item, str)}

    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in cast(list[Any], value):
            if not isinstance(item, str) or "=" not in item:
                continue
            key, item_value = item.split("=", 1)
            result[key] = item_value
        return result

    return {}


def _editor_window_title(session: ProjectSession) -> str:
    return f"{session.session_name}:{EDITOR_ROLE}"


def _editor_os_window_name(session: ProjectSession) -> str:
    return f"hop:{session.session_name}:{EDITOR_ROLE}"
