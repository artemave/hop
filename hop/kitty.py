from __future__ import annotations

import json
import os
import select
import socket
import termios
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from hop.errors import HopError
from hop.session import ProjectSession

KITTY_LISTEN_ON_ENV_VAR = "KITTY_LISTEN_ON"
KITTY_WINDOW_ID_ENV_VAR = "KITTY_WINDOW_ID"
KITTY_PROTOCOL_VERSION = (0, 39, 0)
KITTY_COMMAND_PREFIX = b"\x1bP@kitty-cmd"
KITTY_COMMAND_SUFFIX = b"\x1b\\"
HOP_SESSION_ENV_VAR = "HOP_SESSION"
HOP_ROLE_ENV_VAR = "HOP_ROLE"
HOP_PROJECT_ROOT_ENV_VAR = "HOP_PROJECT_ROOT"
HOP_SESSION_VAR = "hop_session"
HOP_ROLE_VAR = "hop_role"
HOP_PROJECT_ROOT_VAR = "hop_project_root"
COMMAND_TIMEOUT_SECONDS = 2.0


class KittyError(HopError):
    """Base error for Kitty integration failures."""


class KittyConnectionError(KittyError):
    """Raised when hop cannot reach a Kitty remote control endpoint."""


class KittyCommandError(KittyError):
    """Raised when Kitty rejects a remote control command."""


class KittyTransport(Protocol):
    def send_command(
        self,
        command_name: str,
        payload: Mapping[str, object] | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class KittyWindow:
    id: int
    session_name: str | None
    role: str | None


@dataclass(frozen=True, slots=True)
class KittyWindowContext:
    id: int
    session_name: str | None
    role: str | None
    project_root: Path | None
    cwd: Path | None


class KittyRemoteControlAdapter:
    def __init__(self, transport: KittyTransport | None = None) -> None:
        self._transport = transport or _build_default_transport()

    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
        window = self._find_window(session, role=role)
        if window is not None:
            self._focus_window(window.id)
            return

        self._launch_window(session, role=role, keep_focus=False)

    def run_in_terminal(
        self,
        session: ProjectSession,
        *,
        role: str,
        command: str,
    ) -> None:
        window = self._find_window(session, role=role)
        if window is None:
            self._launch_window(session, role=role, keep_focus=True)
            window = self._find_window(session, role=role)

        if window is None:
            msg = (
                "Kitty created a terminal window, but hop could not find it again for "
                f"{session.session_name!r}:{role!r}."
            )
            raise KittyCommandError(msg)

        text = command if command.endswith("\n") else f"{command}\n"
        self._transport.send_command(
            "send-text",
            {
                "match": f"id:{window.id}",
                "data": f"text:{text}",
            },
        )

    def inspect_window(self, window_id: int) -> KittyWindowContext | None:
        response = self._transport.send_command(
            "ls",
            {
                "match": f"id:{window_id}",
                "output_format": "json",
                "all_env_vars": True,
            },
        )
        payload = _coerce_response_data(response)
        if not isinstance(payload, list):
            msg = "Kitty returned an invalid window listing."
            raise KittyCommandError(msg)

        for os_window in payload:
            if not isinstance(os_window, Mapping):
                continue
            for tab in os_window.get("tabs", ()):
                if not isinstance(tab, Mapping):
                    continue
                for window_entry in tab.get("windows", ()):
                    if not isinstance(window_entry, Mapping):
                        continue
                    window = _parse_window_context(window_entry)
                    if window is not None:
                        return window

        return None

    def _find_window(self, session: ProjectSession, *, role: str) -> KittyWindow | None:
        windows = [
            window
            for window in self._list_windows()
            if window.session_name == session.session_name and window.role == role
        ]
        if not windows:
            return None

        return min(windows, key=lambda window: window.id)

    def _focus_window(self, window_id: int) -> None:
        self._transport.send_command("focus-window", {"match": f"id:{window_id}"})

    def _launch_window(
        self,
        session: ProjectSession,
        *,
        role: str,
        keep_focus: bool,
    ) -> None:
        self._transport.send_command(
            "launch",
            {
                "args": [],
                "cwd": str(session.project_root),
                "type": "os-window",
                "keep_focus": keep_focus,
                "allow_remote_control": True,
                "window_title": _window_title(session, role=role),
                "os_window_title": _window_title(session, role=role),
                "os_window_name": _os_window_name(session, role=role),
                "env": [
                    f"{HOP_SESSION_ENV_VAR}={session.session_name}",
                    f"{HOP_ROLE_ENV_VAR}={role}",
                    f"{HOP_PROJECT_ROOT_ENV_VAR}={session.project_root}",
                ],
                "var": [
                    f"{HOP_SESSION_VAR}={session.session_name}",
                    f"{HOP_ROLE_VAR}={role}",
                    f"{HOP_PROJECT_ROOT_VAR}={session.project_root}",
                ],
            },
        )

    def _list_windows(self) -> tuple[KittyWindow, ...]:
        response = self._transport.send_command("ls", {"output_format": "json"})
        payload = _coerce_response_data(response)

        if not isinstance(payload, list):
            msg = "Kitty returned an invalid window listing."
            raise KittyCommandError(msg)

        windows: list[KittyWindow] = []
        for os_window in payload:
            if not isinstance(os_window, Mapping):
                continue
            for tab in os_window.get("tabs", ()):
                if not isinstance(tab, Mapping):
                    continue
                for window_entry in tab.get("windows", ()):
                    if not isinstance(window_entry, Mapping):
                        continue
                    window = _parse_window(window_entry)
                    if window is not None:
                        windows.append(window)

        return tuple(windows)


class ControllingTtyKittyTransport:
    def __init__(
        self,
        *,
        tty_path: Path | str = "/dev/tty",
        kitty_window_id: str | None = None,
        timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._tty_path = str(tty_path)
        self._kitty_window_id = kitty_window_id or os.environ.get(KITTY_WINDOW_ID_ENV_VAR)
        self._timeout_seconds = timeout_seconds

    def send_command(
        self,
        command_name: str,
        payload: Mapping[str, object] | None = None,
    ) -> object:
        request = _encode_command(
            command_name,
            payload=payload,
            kitty_window_id=self._kitty_window_id,
        )

        try:
            tty_fd = os.open(self._tty_path, os.O_RDWR | os.O_CLOEXEC)
        except OSError as error:
            msg = f"Could not open {self._tty_path!r} to talk to Kitty."
            raise KittyConnectionError(msg) from error

        original_settings: list[Any] | None = None
        try:
            original_settings = termios.tcgetattr(tty_fd)
            tty.setraw(tty_fd)
            os.write(tty_fd, request)
            response = _read_until(
                lambda remaining: _read_tty_chunk(tty_fd, remaining, timeout_seconds=self._timeout_seconds),
                KITTY_COMMAND_SUFFIX,
            )
        except OSError as error:
            msg = "Kitty did not respond over the controlling terminal."
            raise KittyConnectionError(msg) from error
        finally:
            if original_settings is not None:
                termios.tcsetattr(tty_fd, termios.TCSADRAIN, original_settings)
            os.close(tty_fd)

        return _decode_response(response)


class SocketKittyTransport:
    def __init__(
        self,
        listen_on: str | None = None,
    ) -> None:
        self._listen_on = listen_on or os.environ.get(KITTY_LISTEN_ON_ENV_VAR)

    def send_command(
        self,
        command_name: str,
        payload: Mapping[str, object] | None = None,
    ) -> object:
        request = _encode_command(command_name, payload=payload)
        listen_on = self._resolve_listen_on()

        try:
            if listen_on.startswith("fd:"):
                response = self._send_via_fd(int(listen_on.removeprefix("fd:")), request)
            else:
                response = self._send_via_unix_socket(listen_on, request)
        except OSError as error:
            msg = f"Could not talk to Kitty over {listen_on!r}."
            raise KittyConnectionError(msg) from error

        return _decode_response(response)

    def _resolve_listen_on(self) -> str:
        if self._listen_on:
            return self._listen_on

        msg = (
            "Kitty remote control is unavailable because hop is not running inside Kitty and "
            "KITTY_LISTEN_ON is not set."
        )
        raise KittyConnectionError(msg)

    def _send_via_fd(self, fd: int, request: bytes) -> bytes:
        duplicated_fd = os.dup(fd)
        with socket.socket(fileno=duplicated_fd) as client:
            client.sendall(request)
            return _read_until(client.recv, KITTY_COMMAND_SUFFIX)

    def _send_via_unix_socket(self, listen_on: str, request: bytes) -> bytes:
        socket_path = _socket_address(listen_on)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(request)
            return _read_until(client.recv, KITTY_COMMAND_SUFFIX)


def _build_default_transport() -> KittyTransport:
    if os.environ.get(KITTY_WINDOW_ID_ENV_VAR):
        return ControllingTtyKittyTransport()
    return SocketKittyTransport()


def _encode_command(
    command_name: str,
    *,
    payload: Mapping[str, object] | None = None,
    kitty_window_id: str | None = None,
) -> bytes:
    request: dict[str, object] = {
        "cmd": command_name,
        "version": list(KITTY_PROTOCOL_VERSION),
    }
    if kitty_window_id is not None:
        request["kitty_window_id"] = kitty_window_id
    if payload is not None:
        request["payload"] = dict(payload)

    return KITTY_COMMAND_PREFIX + json.dumps(request).encode() + KITTY_COMMAND_SUFFIX


def _decode_response(response: bytes) -> object:
    if not response.startswith(KITTY_COMMAND_PREFIX) or not response.endswith(KITTY_COMMAND_SUFFIX):
        msg = "Kitty returned a malformed remote control response."
        raise KittyConnectionError(msg)

    response_payload = response[len(KITTY_COMMAND_PREFIX) : -len(KITTY_COMMAND_SUFFIX)]
    parsed = json.loads(response_payload.decode())
    if isinstance(parsed, Mapping) and parsed.get("ok") is False:
        error_message = parsed.get("error")
        msg = str(error_message) if error_message else "Kitty rejected a remote control command."
        raise KittyCommandError(msg)
    return parsed


def _coerce_response_data(response: object) -> object:
    if isinstance(response, Mapping):
        data = response.get("data")
    else:
        data = response

    if isinstance(data, str):
        return json.loads(data)

    return data


def _parse_window(window_entry: Mapping[str, object]) -> KittyWindow | None:
    window_id = window_entry.get("id")
    if not isinstance(window_id, int):
        return None

    user_vars = _coerce_string_mapping(
        window_entry.get("user_vars")
        or window_entry.get("user_variables")
        or window_entry.get("vars")
    )
    env = _coerce_string_mapping(window_entry.get("env"))

    return KittyWindow(
        id=window_id,
        session_name=user_vars.get(HOP_SESSION_VAR) or env.get(HOP_SESSION_ENV_VAR),
        role=user_vars.get(HOP_ROLE_VAR) or env.get(HOP_ROLE_ENV_VAR),
    )


def _parse_window_context(window_entry: Mapping[str, object]) -> KittyWindowContext | None:
    window = _parse_window(window_entry)
    if window is None:
        return None

    user_vars = _coerce_string_mapping(
        window_entry.get("user_vars")
        or window_entry.get("user_variables")
        or window_entry.get("vars")
    )
    env = _coerce_string_mapping(window_entry.get("env"))

    project_root_text = user_vars.get(HOP_PROJECT_ROOT_VAR) or env.get(HOP_PROJECT_ROOT_ENV_VAR)
    cwd_text = _window_cwd_text(window_entry)

    return KittyWindowContext(
        id=window.id,
        session_name=window.session_name,
        role=window.role,
        project_root=_path_from_text(project_root_text),
        cwd=_path_from_text(cwd_text),
    )


def _coerce_string_mapping(value: object) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items() if isinstance(item, str)}

    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, item_value = item.split("=", 1)
            result[key] = item_value
        return result

    return {}


def _window_cwd_text(window_entry: Mapping[str, object]) -> str | None:
    for key in ("cwd", "current_working_directory", "last_reported_cwd"):
        value = window_entry.get(key)
        if isinstance(value, str) and value:
            return value

    foreground_processes = window_entry.get("foreground_processes")
    if isinstance(foreground_processes, list):
        for process in foreground_processes:
            if not isinstance(process, Mapping):
                continue
            cwd = process.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd

    return None


def _path_from_text(value: str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser().resolve(strict=False)


def _read_until(read_chunk: Any, suffix: bytes) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = read_chunk(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"".join(chunks).endswith(suffix):
            break

    response = b"".join(chunks)
    if not response:
        msg = "Kitty did not return any data."
        raise KittyConnectionError(msg)
    return response


def _read_tty_chunk(fd: int, remaining: int, *, timeout_seconds: float) -> bytes:
    readable, _, _ = select.select([fd], [], [], timeout_seconds)
    if not readable:
        msg = "Timed out waiting for Kitty to respond."
        raise KittyConnectionError(msg)
    return os.read(fd, remaining)


def _socket_address(listen_on: str) -> str:
    if listen_on.startswith("unix:"):
        socket_path = listen_on.removeprefix("unix:")
    else:
        socket_path = listen_on

    if socket_path.startswith("@"):
        return "\0" + socket_path.removeprefix("@")
    return socket_path


def _window_title(session: ProjectSession, *, role: str) -> str:
    return f"{session.session_name}:{role}"


def _os_window_name(session: ProjectSession, *, role: str) -> str:
    return f"hop:{session.session_name}:{role}"
