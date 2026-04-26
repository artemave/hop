# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownMemberType=false

import json
import os
import socket
import termios
import tty
from pathlib import Path
from typing import Mapping

import pytest
from hop.kitty import (
    KITTY_COMMAND_PREFIX,
    KITTY_COMMAND_SUFFIX,
    KITTY_WINDOW_ID_ENV_VAR,
    ControllingTtyKittyTransport,
    KittyCommandError,
    KittyConnectionError,
    KittyRemoteControlAdapter,
    SocketKittyTransport,
    _build_default_transport,
    _coerce_response_data,
    _coerce_string_mapping,
    _decode_response,
    _encode_command,
    _parse_window,
    _parse_window_context,
    _path_from_text,
    _read_tty_chunk,
    _read_until,
    _socket_address,
    _window_cwd_text,
)
from hop.session import ProjectSession


class StubTransport:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.commands: list[tuple[str, Mapping[str, object] | None]] = []

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if not self._responses:
            return {"ok": True}
        return self._responses.pop(0)


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
        workspace_name=f"p:{project_root}",
    )


def encoded_response(payload: object) -> bytes:
    return KITTY_COMMAND_PREFIX + json.dumps(payload).encode() + KITTY_COMMAND_SUFFIX


def test_inspect_window_rejects_invalid_payload_shape() -> None:
    adapter = KittyRemoteControlAdapter(transport=StubTransport([{"ok": True, "data": {}}]))

    with pytest.raises(KittyCommandError, match="invalid window listing"):
        adapter.inspect_window(17)


def test_inspect_window_skips_malformed_entries_and_returns_none_without_valid_match() -> None:
    adapter = KittyRemoteControlAdapter(
        transport=StubTransport(
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
    )

    assert adapter.inspect_window(17) is None


def test_run_in_terminal_raises_when_new_window_cannot_be_rediscovered() -> None:
    adapter = KittyRemoteControlAdapter(
        transport=StubTransport(
            [
                {"ok": True, "data": []},
                {"ok": True},
                {"ok": True, "data": []},
            ]
        )
    )

    with pytest.raises(KittyCommandError, match="could not find it again"):
        adapter.run_in_terminal(build_session(), role="shell", command="pytest -q")


def test_list_session_windows_skips_malformed_entries_and_uses_env_fallbacks() -> None:
    adapter = KittyRemoteControlAdapter(
        transport=StubTransport(
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
    )

    windows = adapter.list_session_windows(build_session())

    assert [window.id for window in windows] == [17]


def test_list_session_windows_rejects_invalid_payload_shape() -> None:
    adapter = KittyRemoteControlAdapter(transport=StubTransport([{"ok": True, "data": {}}]))

    with pytest.raises(KittyCommandError, match="invalid window listing"):
        adapter.list_session_windows(build_session())


def test_controlling_tty_transport_sends_commands_and_restores_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(os, "open", lambda path, flags: 9)
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["saved"])
    monkeypatch.setattr(tty, "setraw", lambda fd: events.append(("setraw", fd)))
    monkeypatch.setattr(os, "write", lambda fd, payload: events.append(("write", payload)))
    monkeypatch.setattr(
        "hop.kitty._read_until",
        lambda read_chunk, suffix: encoded_response({"ok": True, "data": {"status": "ok"}}),
    )
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, settings: events.append(("restore", settings)))
    monkeypatch.setattr(os, "close", lambda fd: events.append(("close", fd)))

    transport = ControllingTtyKittyTransport(tty_path="/dev/tty-hop", kitty_window_id="17")

    assert transport.send_command("focus-window", {"match": "id:17"}) == {"ok": True, "data": {"status": "ok"}}
    assert events == [
        ("setraw", 9),
        ("write", _encode_command("focus-window", payload={"match": "id:17"}, kitty_window_id="17")),
        ("restore", ["saved"]),
        ("close", 9),
    ]


def test_controlling_tty_transport_wraps_open_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "open", lambda path, flags: (_ for _ in ()).throw(OSError("boom")))

    with pytest.raises(KittyConnectionError, match="Could not open"):
        ControllingTtyKittyTransport(tty_path="/dev/missing").send_command("ls")


def test_controlling_tty_transport_closes_fd_when_tcgetattr_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(os, "open", lambda path, flags: 5)
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(os, "close", lambda fd: events.append(("close", fd)))

    with pytest.raises(KittyConnectionError, match="did not respond over the controlling terminal"):
        ControllingTtyKittyTransport().send_command("ls")

    assert events == [("close", 5)]


def test_controlling_tty_transport_wraps_exchange_failures_and_restores_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(os, "open", lambda path, flags: 7)
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["saved"])
    monkeypatch.setattr(tty, "setraw", lambda fd: events.append(("setraw", fd)))
    monkeypatch.setattr(os, "write", lambda fd, payload: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, settings: events.append(("restore", settings)))
    monkeypatch.setattr(os, "close", lambda fd: events.append(("close", fd)))

    with pytest.raises(KittyConnectionError, match="did not respond over the controlling terminal"):
        ControllingTtyKittyTransport().send_command("ls")

    assert events == [("setraw", 7), ("restore", ["saved"]), ("close", 7)]


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


def test_build_default_transport_prefers_controlling_tty_inside_kitty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KITTY_WINDOW_ID_ENV_VAR, "17")
    assert isinstance(_build_default_transport(), ControllingTtyKittyTransport)

    monkeypatch.delenv(KITTY_WINDOW_ID_ENV_VAR)
    assert isinstance(_build_default_transport(), SocketKittyTransport)


def test_encode_and_decode_command_cover_optional_fields_and_errors() -> None:
    encoded = _encode_command("ls", payload={"output_format": "json"}, kitty_window_id="17")
    decoded_payload = json.loads(encoded[len(KITTY_COMMAND_PREFIX) : -len(KITTY_COMMAND_SUFFIX)])

    assert decoded_payload == {
        "cmd": "ls",
        "version": [0, 39, 0],
        "kitty_window_id": "17",
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
    parsed_window = _parse_window(
        {
            "id": 17,
            "vars": [
                "hop_session=demo",
                "hop_role=shell",
                "hop_project_root=/tmp/demo",
            ],
        }
    )
    assert parsed_window is not None
    assert parsed_window.session_name == "demo"
    assert parsed_window.role == "shell"
    assert parsed_window.project_root == Path("/tmp/demo")

    assert _parse_window_context({"id": "17"}) is None
    parsed_context = _parse_window_context(
        {
            "id": 18,
            "env": {
                "HOP_SESSION": "demo",
                "HOP_ROLE": "shell",
                "HOP_PROJECT_ROOT": "/tmp/demo",
            },
            "foreground_processes": ["invalid", {"cwd": "/tmp/demo/src"}],
        }
    )
    assert parsed_context is not None
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


def test_read_tty_chunk_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("select.select", lambda reads, writes, errors, timeout: ([], [], []))

    with pytest.raises(KittyConnectionError, match="Timed out"):
        _read_tty_chunk(7, 4096, timeout_seconds=0.01)


def test_read_tty_chunk_reads_when_fd_becomes_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("select.select", lambda reads, writes, errors, timeout: ([7], [], []))
    monkeypatch.setattr(os, "read", lambda fd, remaining: b"ok")

    assert _read_tty_chunk(7, 4096, timeout_seconds=0.01) == b"ok"


def test_socket_address_normalizes_unix_and_abstract_addresses() -> None:
    assert _socket_address("unix:/tmp/kitty.sock") == "/tmp/kitty.sock"
    assert _socket_address("unix:@kitty") == "\0kitty"
    assert _socket_address("/tmp/kitty.sock") == "/tmp/kitty.sock"


def test_window_cwd_text_returns_none_without_known_cwd_fields() -> None:
    assert _window_cwd_text({"foreground_processes": None}) is None
