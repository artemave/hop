import os
import json

from hop.sway import (
    SwayCommandError,
    SwayConnectionError,
    SwayIpcAdapter,
    SwayMessageType,
    UnixSocketSwayIpcTransport,
)


class StubSwayTransport:
    def __init__(self, responses: dict[SwayMessageType, bytes]) -> None:
        self.responses = responses
        self.requests: list[tuple[SwayMessageType, bytes]] = []

    def request(self, message_type: SwayMessageType, payload: bytes = b"") -> bytes:
        self.requests.append((message_type, payload))
        return self.responses[message_type]


def test_switch_to_workspace_uses_run_command_ipc_message() -> None:
    transport = StubSwayTransport(
        responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": True}]).encode()}
    )
    sway = SwayIpcAdapter(transport=transport)

    sway.switch_to_workspace("p:demo")

    assert transport.requests == [
        (SwayMessageType.RUN_COMMAND, b'workspace "p:demo"'),
    ]


def test_switch_to_workspace_raises_when_sway_rejects_command() -> None:
    transport = StubSwayTransport(
        responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": False}]).encode()}
    )
    sway = SwayIpcAdapter(transport=transport)

    try:
        sway.switch_to_workspace("p:demo")
    except SwayCommandError as error:
        assert "p:demo" in str(error)
    else:
        raise AssertionError("Expected SwayCommandError for a rejected workspace command")


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
