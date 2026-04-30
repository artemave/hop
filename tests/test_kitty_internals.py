# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownMemberType=false

import json
import os
import socket
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from hop.kitty import (
    KITTY_COMMAND_PREFIX,
    KITTY_COMMAND_SUFFIX,
    KittyCommandError,
    KittyConnectionError,
    KittyRemoteControlAdapter,
    KittyTransport,
    SocketKittyTransport,
    _coerce_response_data,
    _coerce_string_mapping,
    _decode_response,
    _encode_command,
    _parse_window,
    _parse_window_context,
    _path_from_text,
    _read_until,
    _socket_address,
    _window_cwd_text,
)
from hop.session import ProjectSession


class StubKittyFactory:
    def __init__(self, responses: list[object | KittyConnectionError]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str | None, str, Mapping[str, object] | None]] = []

    def __call__(self, listen_on: str | None = None) -> KittyTransport:
        return _StubTransport(listen_on, self)


class _StubTransport:
    def __init__(self, listen_on: str | None, factory: StubKittyFactory) -> None:
        self._listen_on = listen_on
        self._factory = factory

    def send_command(
        self,
        command_name: str,
        payload: Mapping[str, object] | None = None,
    ) -> object:
        self._factory.calls.append((self._listen_on, command_name, payload))
        if not self._factory.responses:
            return {"ok": True}
        next_response = self._factory.responses.pop(0)
        if isinstance(next_response, KittyConnectionError):
            raise next_response
        return next_response


class StubLauncher:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> None:
        self.calls.append((tuple(args), dict(env)))


class FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.connected_to: str | None = None
        self.sent: bytes | None = None

    def __enter__(self) -> "FakeSocket":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def connect(self, address: str) -> None:
        self.connected_to = address

    def sendall(self, request: bytes) -> None:
        self.sent = request

    def recv(self, _remaining: int) -> bytes:
        response, self.response = self.response, b""
        return response


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name=f"p:{project_root.name}",
    )


def encoded_response(payload: object) -> bytes:
    return KITTY_COMMAND_PREFIX + json.dumps(payload).encode() + KITTY_COMMAND_SUFFIX


def test_inspect_window_rejects_invalid_payload_shape() -> None:
    factory = StubKittyFactory([{"ok": True, "data": {}}])
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    with pytest.raises(KittyCommandError, match="invalid window listing"):
        adapter.inspect_window(17)


def test_inspect_window_skips_malformed_entries_and_returns_none_without_valid_match() -> None:
    factory = StubKittyFactory(
        [
            {
                "ok": True,
                "data": [
                    "invalid",
                    {"tabs": ["invalid"]},
                    {
                        "tabs": [
                            {
                                "windows": [
                                    "invalid",
                                    {"id": "17"},
                                ]
                            }
                        ]
                    },
                ],
            }
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    assert adapter.inspect_window(17) is None


def test_run_in_terminal_raises_when_new_window_cannot_be_rediscovered() -> None:
    factory = StubKittyFactory(
        [
            {"ok": True, "data": []},  # _find_window: no existing
            {"ok": True},  # _launch_window's launch RPC succeeds
            {"ok": True, "data": []},  # _find_window after launch: still none
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    with pytest.raises(KittyCommandError, match="could not find it again"):
        adapter.run_in_terminal(build_session(), role="shell", command="pytest -q")


def test_list_session_windows_skips_malformed_entries_and_uses_env_fallbacks() -> None:
    factory = StubKittyFactory(
        [
            {
                "ok": True,
                "data": [
                    "invalid",
                    {"tabs": ["invalid"]},
                    {
                        "tabs": [
                            {
                                "windows": [
                                    "invalid",
                                    {
                                        "id": "17",
                                        "env": {
                                            "HOP_SESSION": "demo",
                                            "HOP_ROLE": "shell",
                                            "HOP_PROJECT_ROOT": str(build_session().project_root),
                                        },
                                    },
                                    {
                                        "id": 17,
                                        "env": {
                                            "HOP_SESSION": "demo",
                                            "HOP_ROLE": "shell",
                                            "HOP_PROJECT_ROOT": str(build_session().project_root),
                                        },
                                    },
                                ]
                            }
                        ]
                    },
                ],
            }
        ]
    )
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    windows = adapter.list_session_windows(build_session())

    assert [window.id for window in windows] == [17]


def test_list_session_windows_rejects_invalid_payload_shape() -> None:
    factory = StubKittyFactory([{"ok": True, "data": {}}])
    adapter = KittyRemoteControlAdapter(transport_factory=factory, launcher=StubLauncher())

    with pytest.raises(KittyCommandError, match="invalid window listing"):
        adapter.list_session_windows(build_session())


def test_socket_transport_requires_listen_on() -> None:
    with pytest.raises(KittyConnectionError, match="KITTY_LISTEN_ON"):
        SocketKittyTransport(listen_on=None).send_command("ls")


def test_socket_transport_wraps_fd_transport_failures() -> None:
    transport = SocketKittyTransport(listen_on="fd:7")
    transport._send_via_fd = lambda fd, request: (_ for _ in ()).throw(OSError("boom"))  # type: ignore[method-assign]

    with pytest.raises(KittyConnectionError, match="fd:7"):
        transport.send_command("ls")


def test_socket_transport_selects_fd_and_unix_socket_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = SocketKittyTransport(listen_on="fd:7")
    transport._send_via_fd = lambda fd, request: encoded_response({"ok": True, "data": fd})  # type: ignore[method-assign]

    assert transport.send_command("ls") == {"ok": True, "data": 7}

    unix_transport = SocketKittyTransport(listen_on="unix:/tmp/kitty.sock")
    unix_transport._send_via_unix_socket = lambda listen_on, request: encoded_response({"ok": True, "data": listen_on})  # type: ignore[method-assign]

    assert unix_transport.send_command("ls") == {"ok": True, "data": "unix:/tmp/kitty.sock"}


def test_socket_transport_fd_and_unix_helpers_use_socket_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    fd_socket = FakeSocket(encoded_response({"ok": True}))
    unix_socket = FakeSocket(encoded_response({"ok": True}))

    monkeypatch.setattr(os, "dup", lambda fd: fd + 100)
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args, **kwargs: fd_socket if kwargs.get("fileno") == 103 else unix_socket,
    )
    monkeypatch.setattr("hop.kitty._read_until", lambda read_chunk, suffix: read_chunk(4096))

    transport = SocketKittyTransport(listen_on="fd:3")
    assert transport._send_via_fd(3, b"payload") == encoded_response({"ok": True})
    assert fd_socket.sent == b"payload"

    assert transport._send_via_unix_socket("unix:@kitty", b"other") == encoded_response({"ok": True})
    assert unix_socket.connected_to == "\0kitty"
    assert unix_socket.sent == b"other"


def test_encode_and_decode_command_cover_optional_fields_and_errors() -> None:
    encoded = _encode_command("ls", payload={"output_format": "json"})
    decoded_payload = json.loads(encoded[len(KITTY_COMMAND_PREFIX) : -len(KITTY_COMMAND_SUFFIX)])

    assert decoded_payload == {
        "cmd": "ls",
        "version": [0, 39, 0],
        "payload": {"output_format": "json"},
    }

    assert _decode_response(encoded_response({"ok": True, "data": {"status": "ok"}})) == {
        "ok": True,
        "data": {"status": "ok"},
    }

    with pytest.raises(KittyConnectionError, match="malformed"):
        _decode_response(b"invalid")

    with pytest.raises(KittyCommandError, match="denied"):
        _decode_response(encoded_response({"ok": False, "error": "denied"}))


def test_data_and_window_helpers_cover_mapping_list_and_fallback_cases() -> None:
    assert _coerce_response_data({"data": json.dumps([1, 2])}) == [1, 2]
    assert _coerce_response_data({"data": [1, 2]}) == [1, 2]
    assert _coerce_response_data([1, 2]) == [1, 2]
    assert _coerce_string_mapping({"one": "1", "two": 2}) == {"one": "1"}
    assert _coerce_string_mapping(["one=1", "two=2", "ignored"]) == {"one": "1", "two": "2"}
    assert _coerce_string_mapping(object()) == {}

    assert _parse_window({"id": "17"}) is None
    parsed_window = _parse_window({"id": 17, "vars": ["hop_role=shell"]})
    assert parsed_window is not None
    assert parsed_window.role == "shell"

    assert _parse_window_context({"id": "17"}) is None
    parsed_context = _parse_window_context(
        {
            "id": 18,
            "user_vars": {"hop_role": "shell"},
            "foreground_processes": ["invalid", {"cwd": "/tmp/demo/src"}],
        }
    )
    assert parsed_context is not None
    assert parsed_context.role == "shell"
    assert parsed_context.cwd == Path("/tmp/demo/src")
    assert _path_from_text(None) is None


def test_window_cwd_text_prefers_direct_keys_then_foreground_processes() -> None:
    assert _window_cwd_text({"cwd": "/tmp/demo"}) == "/tmp/demo"
    assert _window_cwd_text({"current_working_directory": "/tmp/current"}) == "/tmp/current"
    assert _window_cwd_text({"last_reported_cwd": "/tmp/last"}) == "/tmp/last"
    assert _window_cwd_text({"foreground_processes": ["invalid", {"cwd": "/tmp/child"}]}) == "/tmp/child"
    assert _window_cwd_text({"foreground_processes": [{"cwd": ""}]}) is None


def test_read_until_collects_chunks_and_rejects_empty_responses() -> None:
    chunks = iter([b"prefix", KITTY_COMMAND_SUFFIX])

    assert _read_until(lambda _remaining: next(chunks, b""), KITTY_COMMAND_SUFFIX) == b"prefix" + KITTY_COMMAND_SUFFIX

    with pytest.raises(KittyConnectionError, match="did not return any data"):
        _read_until(lambda _remaining: b"", KITTY_COMMAND_SUFFIX)


def test_socket_address_normalizes_unix_and_abstract_addresses() -> None:
    assert _socket_address("unix:/tmp/kitty.sock") == "/tmp/kitty.sock"
    assert _socket_address("unix:@kitty") == "\0kitty"
    assert _socket_address("/tmp/kitty.sock") == "/tmp/kitty.sock"


def test_window_cwd_text_returns_none_without_known_cwd_fields() -> None:
    assert _window_cwd_text({"foreground_processes": None}) is None


def test_default_transport_factory_constructs_socket_transport() -> None:
    from hop.kitty import _default_transport_factory

    transport = _default_transport_factory("unix:@hop-demo")
    assert isinstance(transport, SocketKittyTransport)
    assert transport._listen_on == "unix:@hop-demo"


def test_default_launcher_invokes_subprocess_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    from hop.kitty import _default_launcher

    captured_args: list[Sequence[str]] = []
    captured_kwargs: list[Mapping[str, object]] = []

    class FakePopen:
        def __init__(self, args: Sequence[str], **kwargs: object) -> None:
            captured_args.append(args)
            captured_kwargs.append(kwargs)

    monkeypatch.setattr("subprocess.Popen", FakePopen)

    _default_launcher(("kitty", "--listen-on", "unix:@hop-demo"), {"HOP_SESSION": "demo"})

    assert captured_args == [["kitty", "--listen-on", "unix:@hop-demo"]]
    assert captured_kwargs[0]["env"] == {"HOP_SESSION": "demo"}
    assert captured_kwargs[0]["start_new_session"] is True


def test_session_name_from_listen_on_returns_none_for_non_unix_prefix() -> None:
    from hop.kitty import session_name_from_listen_on

    assert session_name_from_listen_on("tcp:127.0.0.1:1234") is None


def test_session_name_from_listen_on_returns_none_for_abstract_socket() -> None:
    from hop.kitty import session_name_from_listen_on

    assert session_name_from_listen_on("unix:@hop-demo") is None


def test_session_name_from_listen_on_returns_none_when_filename_does_not_match_prefix() -> None:
    from hop.kitty import session_name_from_listen_on

    assert session_name_from_listen_on("unix:/run/user/1000/other-demo.sock") is None


def test_session_name_from_listen_on_extracts_session_name_from_socket_filename() -> None:
    from hop.kitty import session_name_from_listen_on, session_socket_path

    socket_path = session_socket_path("demo")
    assert session_name_from_listen_on(f"unix:{socket_path}") == "demo"
