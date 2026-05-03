from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import gettempdir
from typing import Any, Callable, Mapping, Protocol, Sequence, cast

from hop.backends import SHELL_FALLBACK, HostBackend, SessionBackend
from hop.config import SHELL_ROLE
from hop.errors import HopError
from hop.layouts import WindowSpec, find_window
from hop.session import ProjectSession

# `shell-2`, `shell-3`, ... — ad-hoc shells spawned by `hop` from inside an
# existing session. These are shells, not user commands, so they don't get
# the "drop into shell on exit" composition that other roles do.
ADHOC_SHELL_ROLE_PREFIX = "shell-"

KITTY_LISTEN_ON_ENV_VAR = "KITTY_LISTEN_ON"
KITTY_PROTOCOL_VERSION = (0, 39, 0)
KITTY_COMMAND_PREFIX = b"\x1bP@kitty-cmd"
KITTY_COMMAND_SUFFIX = b"\x1b\\"
HOP_ROLE_VAR = "hop_role"
KITTY_BOOTSTRAP_TIMEOUT_SECONDS = 5.0
KITTY_BOOTSTRAP_POLL_INTERVAL_SECONDS = 0.1
SESSION_SOCKET_FILENAME_PREFIX = "kitty-"
SESSION_SOCKET_FILENAME_SUFFIX = ".sock"


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


TransportFactory = Callable[[str | None], KittyTransport]
KittyLauncher = Callable[[Sequence[str], Mapping[str, str]], None]
SessionBootstrapHook = Callable[["ProjectSession", SessionBackend], None]
SessionBackendFactory = Callable[["ProjectSession"], SessionBackend]
SessionWindowsFactory = Callable[["ProjectSession"], Sequence[WindowSpec]]


@dataclass(frozen=True, slots=True)
class KittyWindow:
    id: int
    role: str | None


@dataclass(frozen=True, slots=True)
class KittyWindowContext:
    id: int
    role: str | None
    cwd: Path | None


@dataclass(frozen=True, slots=True)
class KittyWindowState:
    at_prompt: bool
    last_cmd_exit_status: int


def session_socket_address(session_name: str) -> str:
    return f"unix:{session_socket_path(session_name)}"


def session_socket_path(session_name: str) -> Path:
    runtime_root = os.environ.get("XDG_RUNTIME_DIR") or gettempdir()
    runtime_dir = Path(runtime_root).expanduser() / "hop"
    return runtime_dir / f"{SESSION_SOCKET_FILENAME_PREFIX}{session_name}{SESSION_SOCKET_FILENAME_SUFFIX}"


def session_name_from_listen_on(listen_on: str) -> str | None:
    if not listen_on.startswith("unix:"):
        return None
    socket_path = listen_on.removeprefix("unix:")
    if socket_path.startswith("@"):
        return None
    name = Path(socket_path).name
    if not name.startswith(SESSION_SOCKET_FILENAME_PREFIX) or not name.endswith(SESSION_SOCKET_FILENAME_SUFFIX):
        return None
    return name[len(SESSION_SOCKET_FILENAME_PREFIX) : -len(SESSION_SOCKET_FILENAME_SUFFIX)]


class KittyRemoteControlAdapter:
    def __init__(
        self,
        *,
        session_backend_for: SessionBackendFactory | None = None,
        session_windows_for: SessionWindowsFactory | None = None,
        transport_factory: TransportFactory | None = None,
        launcher: KittyLauncher | None = None,
        on_session_bootstrap: SessionBootstrapHook | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_backend_for: SessionBackendFactory = session_backend_for or (lambda _session: HostBackend())
        # Resolves the window list for this session — used by `_launch_payload`
        # to pick the role's command. Default returns no windows so ad-hoc
        # roles fall through to the empty-shell sentinel (host default shell).
        self._session_windows_for: SessionWindowsFactory = session_windows_for or (lambda _session: ())
        self._transport_factory: TransportFactory = transport_factory or _default_transport_factory
        self._launcher: KittyLauncher = launcher or _default_launcher
        self._on_session_bootstrap: SessionBootstrapHook = on_session_bootstrap or (lambda _session, _backend: None)
        self._sleep = sleep
        self._clock = clock

    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
        window = self._find_window(session, role=role)
        if window is not None:
            self._send_to(session.session_name, "focus-window", {"match": f"id:{window.id}"})
            return

        self._launch_window(session, role=role, keep_focus=False)

    def run_in_terminal(
        self,
        session: ProjectSession,
        *,
        role: str,
        command: str,
    ) -> int:
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
        self._send_to(
            session.session_name,
            "send-text",
            {"match": f"id:{window.id}", "data": f"text:{text}"},
        )
        return window.id

    def get_window_state(self, session_name: str, window_id: int) -> KittyWindowState:
        response = self._send_to(
            session_name,
            "ls",
            {"match": f"id:{window_id}", "output_format": "json"},
        )
        payload = _coerce_response_data(response)

        for os_window in cast(list[Any], payload):
            for tab in os_window.get("tabs", ()):
                for window_entry in tab.get("windows", ()):
                    if window_entry.get("id") != window_id:
                        continue
                    return KittyWindowState(
                        at_prompt=bool(window_entry["at_prompt"]),
                        last_cmd_exit_status=int(window_entry["last_cmd_exit_status"]),
                    )

        msg = f"Kitty has no window with id {window_id}."
        raise KittyCommandError(msg)

    def get_last_cmd_output(self, session_name: str, window_id: int) -> str:
        response = self._send_to(
            session_name,
            "get-text",
            {"match": f"id:{window_id}", "extent": "last_cmd_output"},
        )
        if isinstance(response, Mapping):
            return str(cast(Any, response).get("data", ""))
        return str(response)

    def inspect_window(self, window_id: int, *, listen_on: str | None = None) -> KittyWindowContext | None:
        # Used by the open_selection kitten. Callers in kitty's boss process should
        # pass `boss.listening_on` because os.environ["KITTY_LISTEN_ON"] inside the
        # boss may have been inherited from a different kitty instance.
        transport = self._transport_factory(listen_on)
        response = transport.send_command(
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

        for os_window in cast(list[Any], payload):
            if not isinstance(os_window, Mapping):
                continue
            for tab in cast(Any, os_window).get("tabs", ()):
                if not isinstance(tab, Mapping):
                    continue
                for window_entry in cast(Any, tab).get("windows", ()):
                    if not isinstance(window_entry, Mapping):
                        continue
                    window = _parse_window_context(cast(Any, window_entry))
                    if window is not None:
                        return window

        return None

    def list_session_windows(self, session: ProjectSession) -> tuple[KittyWindow, ...]:
        addr = session_socket_address(session.session_name)
        try:
            return self._list_windows_via(addr)
        except KittyConnectionError:
            return ()

    def close_window(self, session_name: str, window_id: int) -> None:
        self._send_to(session_name, "close-window", {"match": f"id:{window_id}"})

    def _find_window(self, session: ProjectSession, *, role: str) -> KittyWindow | None:
        addr = session_socket_address(session.session_name)
        try:
            windows = self._list_windows_via(addr)
        except KittyConnectionError:
            return None
        matches = [window for window in windows if window.role == role]
        if not matches:
            return None
        return min(matches, key=lambda window: window.id)

    def _launch_window(
        self,
        session: ProjectSession,
        *,
        role: str,
        keep_focus: bool,
    ) -> None:
        backend = self._session_backend_for(session)
        try:
            self._send_to(
                session.session_name,
                "launch",
                self._launch_payload(session, backend=backend, role=role, keep_focus=keep_focus),
            )
        except KittyConnectionError:
            self._bootstrap_session_kitty(
                session_socket_address(session.session_name),
                session,
                backend=backend,
                role=role,
            )

    def _bootstrap_session_kitty(
        self,
        addr: str,
        session: ProjectSession,
        *,
        backend: SessionBackend,
        role: str,
    ) -> None:
        backend.prepare(session)

        # Ensure the parent dir for the filesystem socket exists; kitty
        # creates the socket itself, but its parent must be writable.
        socket_path = session_socket_path(session.session_name)
        socket_path.parent.mkdir(parents=True, exist_ok=True)

        kitty_args = [
            "kitty",
            "--directory",
            str(session.project_root),
            "--listen-on",
            addr,
            "--title",
            role,
            # `--class` sets Sway's `app_id` on Wayland (and the WM_CLASS
            # class half on X11). `--name` would only cover the X11 name
            # half and leave Wayland app_id at the default `kitty`.
            "--class",
            _os_window_name(role),
            "--override",
            "allow_remote_control=yes",
        ]
        # Bootstrap always launches the shell role: a freshly created kitty
        # process needs at least one window. Resolve the shell command from
        # the active layout/windows config (built-in default is "" → kitty's
        # platform-default shell on host).
        shell_command = self._command_for_role(session, SHELL_ROLE)
        shell_argv = list(backend.wrap(shell_command, session))
        if shell_argv:
            kitty_args.append("--")
            kitty_args.extend(shell_argv)
        self._launcher(tuple(kitty_args), dict(os.environ))
        self._wait_for_session_kitty(addr)
        # Kitty's CLI doesn't accept --var, so the bootstrap window has no
        # user_vars by default. Tag it now so role-window discovery treats it
        # the same as windows added via `kitty @ launch --var=...`.
        self._send_to(
            session.session_name,
            "set-user-vars",
            {"match": "all", "var": [f"{HOP_ROLE_VAR}={role}"]},
        )
        self._on_session_bootstrap(session, backend)

    def _wait_for_session_kitty(self, addr: str) -> None:
        deadline = self._clock() + KITTY_BOOTSTRAP_TIMEOUT_SECONDS
        while self._clock() < deadline:
            if self._is_session_kitty_listening(addr):
                return
            self._sleep(KITTY_BOOTSTRAP_POLL_INTERVAL_SECONDS)
        msg = f"Kitty did not start listening on {addr!r} within {KITTY_BOOTSTRAP_TIMEOUT_SECONDS:.0f}s."
        raise KittyConnectionError(msg)

    def _is_session_kitty_listening(self, addr: str) -> bool:
        try:
            self._transport_factory(addr).send_command("ls", {"output_format": "json"})
        except (KittyConnectionError, KittyCommandError):
            return False
        return True

    def _send_to(
        self,
        session_name: str,
        command: str,
        payload: Mapping[str, object] | None = None,
    ) -> object:
        addr = session_socket_address(session_name)
        return self._transport_factory(addr).send_command(command, payload)

    def _list_windows_via(self, addr: str) -> tuple[KittyWindow, ...]:
        response = self._transport_factory(addr).send_command("ls", {"output_format": "json"})
        payload = _coerce_response_data(response)

        if not isinstance(payload, list):
            msg = "Kitty returned an invalid window listing."
            raise KittyCommandError(msg)

        windows: list[KittyWindow] = []
        for os_window in cast(list[Any], payload):
            if not isinstance(os_window, Mapping):
                continue
            for tab in cast(Any, os_window).get("tabs", ()):
                if not isinstance(tab, Mapping):
                    continue
                for window_entry in cast(Any, tab).get("windows", ()):
                    if not isinstance(window_entry, Mapping):
                        continue
                    window = _parse_window(cast(Any, window_entry))
                    if window is not None:
                        windows.append(window)

        return tuple(windows)

    def _launch_payload(
        self,
        session: ProjectSession,
        *,
        backend: SessionBackend,
        role: str,
        keep_focus: bool,
    ) -> dict[str, object]:
        # Declared roles (server, test, console, ...) launch the resolved
        # command from layouts / top-level windows. Ad-hoc roles like
        # `shell-2` aren't declared, so they fall back to the shell role's
        # command — preserving "ask for any role, get a shell" ergonomic.
        args = list(self._launch_args(session, backend=backend, role=role))
        return {
            "args": args,
            "cwd": str(session.project_root),
            "type": "os-window",
            "keep_focus": keep_focus,
            "allow_remote_control": True,
            "window_title": role,
            "os_window_title": role,
            # `os_window_class` sets Sway's `app_id` on Wayland;
            # `os_window_name` only sets the X11 WM_CLASS-name half.
            "os_window_class": _os_window_name(role),
            "var": [f"{HOP_ROLE_VAR}={role}"],
        }

    def _launch_args(
        self,
        session: ProjectSession,
        *,
        backend: SessionBackend,
        role: str,
    ) -> Sequence[str]:
        command = self._command_for_role(session, role)
        # The shell role IS the post-exit fallback, so no composition needed.
        # An empty command (no resolved spec, no windows resolver wired) is
        # also treated as shell-like — the wrap path's empty-command branch
        # handles it (kitty default on host, ${SHELL:-sh} inside a prefix).
        if _is_shell_role(role) or not command:
            return backend.wrap(command, session)
        # For everything else (server, log, console, custom, ...), compose
        # `<command>; <shell>` so the kitty window stays open if the role's
        # process exits cleanly or is Ctrl-C'd. Each piece is wrapped
        # through the prefix individually (via inline) before a single
        # outer sh -c, so the `;` runs each side as its own backend exec.
        shell_command = self._command_for_role(session, SHELL_ROLE) or SHELL_FALLBACK
        command_inline = backend.inline(command, session)
        shell_inline = backend.inline(shell_command, session)
        return ("sh", "-c", f"{command_inline}; {shell_inline}")

    def _command_for_role(self, session: ProjectSession, role: str) -> str:
        windows = self._session_windows_for(session)
        spec = find_window(windows, role)
        if spec is not None:
            return spec.command
        # Ad-hoc role (e.g. shell-2) or no resolver wired (default factory):
        # fall back to the shell role's command, then to the empty sentinel
        # (handled by backend.wrap as "use platform default").
        shell_spec = find_window(windows, SHELL_ROLE)
        if shell_spec is not None:
            return shell_spec.command
        return ""


class SocketKittyTransport:
    def __init__(self, listen_on: str | None = None) -> None:
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


def _default_transport_factory(listen_on: str | None) -> KittyTransport:
    return SocketKittyTransport(listen_on=listen_on)


def _default_launcher(args: Sequence[str], env: Mapping[str, str]) -> None:
    subprocess.Popen(
        list(args),
        env=dict(env),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def _encode_command(
    command_name: str,
    *,
    payload: Mapping[str, object] | None = None,
) -> bytes:
    request: dict[str, object] = {
        "cmd": command_name,
        "version": list(KITTY_PROTOCOL_VERSION),
    }
    if payload is not None:
        request["payload"] = dict(payload)

    return KITTY_COMMAND_PREFIX + json.dumps(request).encode() + KITTY_COMMAND_SUFFIX


def _decode_response(response: bytes) -> object:
    if not response.startswith(KITTY_COMMAND_PREFIX) or not response.endswith(KITTY_COMMAND_SUFFIX):
        msg = "Kitty returned a malformed remote control response."
        raise KittyConnectionError(msg)

    response_payload = response[len(KITTY_COMMAND_PREFIX) : -len(KITTY_COMMAND_SUFFIX)]
    parsed: Any = json.loads(response_payload.decode())
    if isinstance(parsed, Mapping) and cast(Any, parsed).get("ok") is False:
        error_message = cast(Any, parsed).get("error")
        msg = str(error_message) if error_message else "Kitty rejected a remote control command."
        raise KittyCommandError(msg)
    return cast(Any, parsed)


def _coerce_response_data(response: object) -> Any:
    if isinstance(response, Mapping):
        data = cast(Any, response).get("data")
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
        window_entry.get("user_vars") or window_entry.get("user_variables") or window_entry.get("vars")
    )

    return KittyWindow(id=window_id, role=user_vars.get(HOP_ROLE_VAR))


def _parse_window_context(window_entry: Mapping[str, object]) -> KittyWindowContext | None:
    window = _parse_window(window_entry)
    if window is None:
        return None

    cwd_text = _window_cwd_text(window_entry)

    return KittyWindowContext(
        id=window.id,
        role=window.role,
        cwd=_path_from_text(cwd_text),
    )


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


def _window_cwd_text(window_entry: Mapping[str, object]) -> str | None:
    for key in ("cwd", "current_working_directory", "last_reported_cwd"):
        value = window_entry.get(key)
        if isinstance(value, str) and value:
            return value

    foreground_processes = window_entry.get("foreground_processes")
    if isinstance(foreground_processes, list):
        for process in cast(list[Any], foreground_processes):
            if not isinstance(process, Mapping):
                continue
            cwd = cast(Any, process).get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd

    return None


def _path_from_text(value: str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser().resolve(strict=False)


def _read_until(read_chunk: Callable[[int], bytes], suffix: bytes) -> bytes:
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


def _socket_address(listen_on: str) -> str:
    if listen_on.startswith("unix:"):
        socket_path = listen_on.removeprefix("unix:")
    else:
        socket_path = listen_on

    if socket_path.startswith("@"):
        return "\0" + socket_path.removeprefix("@")
    return socket_path


def _os_window_name(role: str) -> str:
    return f"hop:{role}"


def _is_shell_role(role: str) -> bool:
    return role == SHELL_ROLE or role.startswith(ADHOC_SHELL_ROLE_PREFIX)
