import json
import os
import socket
import struct
import threading
from pathlib import Path
from typing import Iterator, cast

from hop.sway import (
    EVENT_TYPE_FLAG,
    IPC_HEADER_FORMAT,
    IPC_MAGIC,
    WORKSPACE_EVENT_TYPE,
    SwayCommandError,
    SwayConnectionError,
    SwayIpcAdapter,
    SwayMessageType,
    SwaySubscriptionError,
    SwayWindow,
    UnixSocketSwayIpcTransport,
)


class StubSwayTransport:
    def __init__(
        self,
        responses: dict[SwayMessageType, bytes] | None = None,
        *,
        subscribe_acks: bytes | None = None,
        subscribe_events: tuple[bytes, ...] = (),
    ) -> None:
        self.responses = responses or {}
        self.requests: list[tuple[SwayMessageType, bytes]] = []
        self.subscribe_payloads: list[bytes] = []
        self._subscribe_ack = subscribe_acks
        self._subscribe_events = subscribe_events

    def request(self, message_type: SwayMessageType, payload: bytes = b"") -> bytes:
        self.requests.append((message_type, payload))
        return self.responses[message_type]

    def subscribe(self, payload: bytes) -> Iterator[bytes]:
        self.subscribe_payloads.append(payload)
        if self._subscribe_ack is not None:
            decoded: object = json.loads(self._subscribe_ack.decode())
            assert isinstance(decoded, dict)
            ack = cast("dict[str, object]", decoded)
            if ack.get("success") is not True:
                raise SwaySubscriptionError(f"Stub subscription refused: {ack!r}")
        for event in self._subscribe_events:
            yield event


def test_switch_to_workspace_uses_run_command_ipc_message() -> None:
    transport = StubSwayTransport(responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": True}]).encode()})
    sway = SwayIpcAdapter(transport=transport)

    sway.switch_to_workspace("p:demo")

    assert transport.requests == [
        (SwayMessageType.RUN_COMMAND, b'workspace "p:demo"'),
    ]


def test_set_workspace_layout_sends_bare_layout_command() -> None:
    """Caller is expected to have just switched to the workspace, so a bare
    `layout <mode>` operates on its root container. The chained
    `workspace foo; layout tabbed` form caused mis-focused window
    placement during the kitty bootstrap that follows."""
    transport = StubSwayTransport(responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": True}]).encode()})
    sway = SwayIpcAdapter(transport=transport)

    sway.set_workspace_layout("p:demo", "tabbed")

    assert transport.requests == [
        (SwayMessageType.RUN_COMMAND, b"layout tabbed"),
    ]


def test_set_workspace_layout_raises_when_sway_rejects_command() -> None:
    transport = StubSwayTransport(responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": False}]).encode()})
    sway = SwayIpcAdapter(transport=transport)

    try:
        sway.set_workspace_layout("p:demo", "tabbed")
    except SwayCommandError as error:
        assert "tabbed" in str(error)
    else:
        raise AssertionError("Expected SwayCommandError for a rejected layout command")


def test_switch_to_workspace_raises_when_sway_rejects_command() -> None:
    transport = StubSwayTransport(responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": False}]).encode()})
    sway = SwayIpcAdapter(transport=transport)

    try:
        sway.switch_to_workspace("p:demo")
    except SwayCommandError as error:
        assert "p:demo" in str(error)
    else:
        raise AssertionError("Expected SwayCommandError for a rejected workspace command")


def test_run_command_raises_with_rejected_command_text() -> None:
    transport = StubSwayTransport(responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": False}]).encode()})
    sway = SwayIpcAdapter(transport=transport)

    try:
        sway.run_command("[con_id=17] focus")
    except SwayCommandError as error:
        assert "[con_id=17] focus" in str(error)
    else:
        raise AssertionError("Expected SwayCommandError when a generic command is rejected")


def test_list_session_workspaces_filters_and_sorts_workspace_names() -> None:
    transport = StubSwayTransport(
        responses={
            SwayMessageType.GET_WORKSPACES: json.dumps(
                [
                    {"name": "scratch", "focused": False},
                    {"name": "p:zeta", "focused": False},
                    {"name": "p:alpha", "focused": True},
                ]
            ).encode()
        }
    )
    sway = SwayIpcAdapter(transport=transport)

    assert sway.list_session_workspaces() == ("p:alpha", "p:zeta")


def test_get_focused_workspace_returns_focused_workspace_name() -> None:
    transport = StubSwayTransport(
        responses={
            SwayMessageType.GET_WORKSPACES: json.dumps(
                [
                    {"name": "scratch", "focused": False},
                    {"name": "p:demo", "focused": True},
                ]
            ).encode()
        }
    )
    sway = SwayIpcAdapter(transport=transport)

    assert sway.get_focused_workspace() == "p:demo"


def test_get_focused_workspace_returns_empty_string_when_none_is_focused() -> None:
    transport = StubSwayTransport(
        responses={
            SwayMessageType.GET_WORKSPACES: json.dumps(
                [
                    {"name": "scratch", "focused": False},
                    {"name": "p:demo", "focused": False},
                ]
            ).encode()
        }
    )
    sway = SwayIpcAdapter(transport=transport)

    assert sway.get_focused_workspace() == ""


def test_list_windows_flattens_the_sway_tree_with_workspace_context() -> None:
    transport = StubSwayTransport(
        responses={
            SwayMessageType.GET_TREE: json.dumps(
                {
                    "nodes": [
                        {
                            "type": "workspace",
                            "name": "p:demo",
                            "nodes": [
                                {
                                    "id": 17,
                                    "app_id": "brave-browser",
                                    "marks": ["_hop_browser:demo"],
                                    "focused": True,
                                }
                            ],
                            "floating_nodes": [
                                {
                                    "id": 23,
                                    "window_properties": {"class": "firefox"},
                                    "marks": [],
                                    "focused": False,
                                }
                            ],
                        }
                    ]
                }
            ).encode()
        }
    )
    sway = SwayIpcAdapter(transport=transport)

    assert sway.list_windows() == (
        SwayWindow(
            id=17,
            workspace_name="p:demo",
            app_id="brave-browser",
            window_class=None,
            marks=("_hop_browser:demo",),
            focused=True,
        ),
        SwayWindow(
            id=23,
            workspace_name="p:demo",
            app_id=None,
            window_class="firefox",
            marks=(),
            focused=False,
        ),
    )


def test_window_commands_use_sway_criteria_by_container_id() -> None:
    transport = StubSwayTransport(responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": True}]).encode()})
    sway = SwayIpcAdapter(transport=transport)

    sway.focus_window(17)
    sway.move_window_to_workspace(17, "p:demo")
    sway.mark_window(17, "_hop_browser:demo")

    assert transport.requests == [
        (SwayMessageType.RUN_COMMAND, b"[con_id=17] focus"),
        (SwayMessageType.RUN_COMMAND, b'[con_id=17] move container to workspace "p:demo"'),
        (SwayMessageType.RUN_COMMAND, b'[con_id=17] mark --add "_hop_browser:demo"'),
    ]


def test_close_window_uses_kill_command_with_container_id() -> None:
    transport = StubSwayTransport(responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": True}]).encode()})
    sway = SwayIpcAdapter(transport=transport)

    sway.close_window(42)

    assert transport.requests == [
        (SwayMessageType.RUN_COMMAND, b"[con_id=42] kill"),
    ]


def test_remove_workspace_switches_focus_to_trigger_sway_cleanup() -> None:
    transport = StubSwayTransport(responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": True}]).encode()})
    sway = SwayIpcAdapter(transport=transport)

    sway.remove_workspace("p:/tmp/demo")

    assert transport.requests == [
        (SwayMessageType.RUN_COMMAND, b"workspace back_and_forth"),
    ]


def test_default_transport_requires_swaysock() -> None:
    original_value = os.environ.pop("SWAYSOCK", None)
    transport = UnixSocketSwayIpcTransport()

    try:
        transport.request(SwayMessageType.GET_WORKSPACES)
    except SwayConnectionError as error:
        assert "SWAYSOCK" in str(error)
    else:
        raise AssertionError("Expected SwayConnectionError when SWAYSOCK is unset")
    finally:
        if original_value is not None:
            os.environ["SWAYSOCK"] = original_value


def test_subscribe_to_workspace_events_yields_decoded_event_dicts() -> None:
    event_one = json.dumps({"change": "focus", "current": {"name": "p:demo"}}).encode()
    event_two = json.dumps({"change": "focus", "current": {"name": "scratch"}}).encode()
    transport = StubSwayTransport(
        subscribe_acks=json.dumps({"success": True}).encode(),
        subscribe_events=(event_one, event_two),
    )
    sway = SwayIpcAdapter(transport=transport)

    events = list(sway.subscribe_to_workspace_events())

    assert events == [
        {"change": "focus", "current": {"name": "p:demo"}},
        {"change": "focus", "current": {"name": "scratch"}},
    ]
    assert transport.subscribe_payloads == [b'["workspace"]']


def test_subscribe_raises_when_sway_refuses_subscription() -> None:
    transport = StubSwayTransport(subscribe_acks=json.dumps({"success": False}).encode())
    sway = SwayIpcAdapter(transport=transport)

    try:
        list(sway.subscribe_to_workspace_events())
    except SwaySubscriptionError as error:
        assert "subscription" in str(error).lower() or "refused" in str(error).lower()
    else:
        raise AssertionError("Expected SwaySubscriptionError when sway refuses the subscription")


def test_subscribe_against_real_unix_socket_yields_workspace_event(tmp_path: Path) -> None:
    """End-to-end check that the wire format matches sway's IPC protocol.

    Spins up a real `AF_UNIX` server bound to a tempdir path, speaks the
    sway IPC framing manually, and asserts the adapter parses both the
    success ack and a workspace event back into a Python dict. No
    mocks — this exercises the actual `recv` / `struct.unpack` path.
    """

    socket_path = tmp_path / "sway.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)

    def serve() -> None:
        client, _ = server.accept()
        try:
            header = client.recv(struct.calcsize(IPC_HEADER_FORMAT))
            magic, payload_size, message_type = struct.unpack(IPC_HEADER_FORMAT, header)
            assert magic == IPC_MAGIC
            assert message_type == int(SwayMessageType.SUBSCRIBE)
            payload = client.recv(payload_size)
            assert json.loads(payload.decode()) == ["workspace"]

            ack_payload = json.dumps({"success": True}).encode()
            client.sendall(
                struct.pack(IPC_HEADER_FORMAT, IPC_MAGIC, len(ack_payload), int(SwayMessageType.SUBSCRIBE))
                + ack_payload
            )

            event_payload = json.dumps({"change": "focus", "current": {"name": "p:demo"}}).encode()
            client.sendall(
                struct.pack(IPC_HEADER_FORMAT, IPC_MAGIC, len(event_payload), WORKSPACE_EVENT_TYPE) + event_payload
            )
            client.shutdown(socket.SHUT_RDWR)
        finally:
            client.close()

    server_thread = threading.Thread(target=serve)
    server_thread.start()
    try:
        transport = UnixSocketSwayIpcTransport(socket_path=socket_path)
        sway = SwayIpcAdapter(transport=transport)

        events = list(sway.subscribe_to_workspace_events())

        assert events == [{"change": "focus", "current": {"name": "p:demo"}}]
    finally:
        server_thread.join(timeout=2)
        server.close()


def test_workspace_event_type_constant_matches_sway_protocol() -> None:
    # Per sway-ipc(7), workspace events are sent with the high bit set on
    # message_type, with the low bits identifying the event kind (0).
    assert WORKSPACE_EVENT_TYPE == EVENT_TYPE_FLAG | 0


def test_subscribe_via_unix_socket_raises_when_path_is_unreachable(tmp_path: Path) -> None:
    transport = UnixSocketSwayIpcTransport(socket_path=tmp_path / "no-such-socket")

    try:
        list(transport.subscribe(b'["workspace"]'))
    except SwayConnectionError as error:
        assert "Could not connect" in str(error)
    else:
        raise AssertionError("Expected SwayConnectionError when subscribe target socket does not exist")


def test_subscribe_via_unix_socket_raises_when_sway_returns_failure_ack(tmp_path: Path) -> None:
    socket_path = tmp_path / "sway.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)

    def serve() -> None:
        client, _ = server.accept()
        try:
            header = client.recv(struct.calcsize(IPC_HEADER_FORMAT))
            _magic, payload_size, _message_type = struct.unpack(IPC_HEADER_FORMAT, header)
            client.recv(payload_size)
            ack = json.dumps({"success": False}).encode()
            client.sendall(struct.pack(IPC_HEADER_FORMAT, IPC_MAGIC, len(ack), int(SwayMessageType.SUBSCRIBE)) + ack)
            client.shutdown(socket.SHUT_RDWR)
        finally:
            client.close()

    server_thread = threading.Thread(target=serve)
    server_thread.start()
    try:
        transport = UnixSocketSwayIpcTransport(socket_path=socket_path)
        try:
            list(transport.subscribe(b'["workspace"]'))
        except SwaySubscriptionError as error:
            assert "refused" in str(error).lower()
        else:
            raise AssertionError("Expected SwaySubscriptionError when sway acks success=false")
    finally:
        server_thread.join(timeout=2)
        server.close()


def test_subscribe_raises_when_socket_closes_mid_message(tmp_path: Path) -> None:
    socket_path = tmp_path / "sway.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)

    def serve() -> None:
        client, _ = server.accept()
        try:
            header = client.recv(struct.calcsize(IPC_HEADER_FORMAT))
            _magic, payload_size, _message_type = struct.unpack(IPC_HEADER_FORMAT, header)
            client.recv(payload_size)
            # Send only half the ack header before closing — exercises the
            # "EOF mid-message" branch in _read_message.
            client.sendall(b"\x00\x00\x00")
            client.shutdown(socket.SHUT_RDWR)
        finally:
            client.close()

    server_thread = threading.Thread(target=serve)
    server_thread.start()
    try:
        transport = UnixSocketSwayIpcTransport(socket_path=socket_path)
        try:
            list(transport.subscribe(b'["workspace"]'))
        except SwayConnectionError as error:
            assert "closed before" in str(error)
        else:
            raise AssertionError("Expected SwayConnectionError when sway closes the socket mid-message")
    finally:
        server_thread.join(timeout=2)
        server.close()


def test_subscribe_raises_when_response_header_has_invalid_magic(tmp_path: Path) -> None:
    socket_path = tmp_path / "sway.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)

    def serve() -> None:
        client, _ = server.accept()
        try:
            header = client.recv(struct.calcsize(IPC_HEADER_FORMAT))
            _magic, payload_size, _message_type = struct.unpack(IPC_HEADER_FORMAT, header)
            client.recv(payload_size)
            # Send a 14-byte response with a bad magic prefix.
            bogus = struct.pack(IPC_HEADER_FORMAT, b"NOT-IT", 0, int(SwayMessageType.SUBSCRIBE))
            client.sendall(bogus)
            client.shutdown(socket.SHUT_RDWR)
        finally:
            client.close()

    server_thread = threading.Thread(target=serve)
    server_thread.start()
    try:
        transport = UnixSocketSwayIpcTransport(socket_path=socket_path)
        try:
            list(transport.subscribe(b'["workspace"]'))
        except SwayConnectionError as error:
            assert "invalid response" in str(error).lower()
        else:
            raise AssertionError("Expected SwayConnectionError on invalid magic header")
    finally:
        server_thread.join(timeout=2)
        server.close()
