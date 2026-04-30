# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportArgumentType=false

import json
import socket
import struct

import pytest

from hop.sway import (
    IPC_HEADER_FORMAT,
    IPC_MAGIC,
    SWAY_SOCKET_ENV_VAR,
    SwayConnectionError,
    SwayMessageType,
    SwayWindow,
    UnixSocketSwayIpcTransport,
    _collect_windows,
    _extract_window_class,
    _recv_exact,
)


class FakeSocket:
    def __init__(self, *, response_parts: list[bytes], connect_error: OSError | None = None) -> None:
        self.response_parts = list(response_parts)
        self.connect_error = connect_error
        self.connected_to: str | None = None
        self.sent: bytes | None = None

    def __enter__(self) -> "FakeSocket":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def connect(self, address: str) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected_to = address

    def sendall(self, payload: bytes) -> None:
        self.sent = payload

    def recv(self, _remaining: int) -> bytes:
        if not self.response_parts:
            return b""
        chunk = self.response_parts.pop(0)
        if len(chunk) <= _remaining:
            return chunk
        self.response_parts.insert(0, chunk[_remaining:])
        return chunk[:_remaining]


def encode_reply(payload: bytes, *, magic: bytes = IPC_MAGIC) -> bytes:
    header = struct.pack(IPC_HEADER_FORMAT, magic, len(payload), int(SwayMessageType.GET_TREE))
    return header + payload


def test_unix_socket_transport_requests_bytes_from_sway(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps([{"success": True}]).encode()
    response = encode_reply(payload)
    fake_socket = FakeSocket(response_parts=[response[:4], response[4:20], response[20:]])
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: fake_socket)

    transport = UnixSocketSwayIpcTransport("/tmp/sway.sock")

    assert transport.request(SwayMessageType.RUN_COMMAND, b"workspace 1") == payload
    assert fake_socket.connected_to == "/tmp/sway.sock"
    assert fake_socket.sent == (
        struct.pack(IPC_HEADER_FORMAT, IPC_MAGIC, 11, int(SwayMessageType.RUN_COMMAND)) + b"workspace 1"
    )


def test_unix_socket_transport_wraps_connect_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_socket = FakeSocket(response_parts=[], connect_error=OSError("boom"))
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: fake_socket)

    with pytest.raises(SwayConnectionError, match="Could not connect"):
        UnixSocketSwayIpcTransport("/tmp/sway.sock").request(SwayMessageType.GET_TREE)


def test_unix_socket_transport_rejects_invalid_magic(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_socket = FakeSocket(response_parts=[encode_reply(b"{}", magic=b"broken!")])
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: fake_socket)

    with pytest.raises(SwayConnectionError, match="invalid response"):
        UnixSocketSwayIpcTransport("/tmp/sway.sock").request(SwayMessageType.GET_TREE)


def test_unix_socket_transport_resolves_socket_path_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"[]"
    fake_socket = FakeSocket(response_parts=[encode_reply(payload)])
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: fake_socket)
    monkeypatch.setenv(SWAY_SOCKET_ENV_VAR, "/tmp/from-env.sock")

    assert UnixSocketSwayIpcTransport().request(SwayMessageType.GET_WORKSPACES) == payload
    assert fake_socket.connected_to == "/tmp/from-env.sock"


def test_recv_exact_rejects_early_socket_close() -> None:
    client = FakeSocket(response_parts=[b"abc", b""])

    with pytest.raises(SwayConnectionError, match="closed before the full response"):
        _recv_exact(client, 5)


def test_collect_windows_ignores_non_dict_nodes_and_tracks_workspace_context() -> None:
    windows: list[SwayWindow] = []

    _collect_windows("invalid", windows=windows)
    _collect_windows(
        {
            "type": "workspace",
            "name": "p:demo",
            "nodes": [
                {"id": 17, "focused": True},
                {
                    "id": 23,
                    "app_id": "firefox",
                    "marks": ["_hop_browser:demo", 1],
                    "focused": False,
                },
            ],
            "floating_nodes": [
                {
                    "id": 29,
                    "window_properties": {"class": "kitty"},
                    "focused": False,
                }
            ],
        },
        windows=windows,
    )

    assert windows == [
        SwayWindow(
            id=23,
            workspace_name="p:demo",
            app_id="firefox",
            window_class=None,
            marks=("_hop_browser:demo",),
            focused=False,
        ),
        SwayWindow(
            id=29,
            workspace_name="p:demo",
            app_id=None,
            window_class="kitty",
            marks=(),
            focused=False,
        ),
    ]


def test_extract_window_class_requires_string_mapping() -> None:
    assert _extract_window_class(None) is None
    assert _extract_window_class({"class": 7}) is None
    assert _extract_window_class({"class": "kitty"}) == "kitty"
