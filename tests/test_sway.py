import os
import json

from hop.sway import (
    SwayCommandError,
    SwayConnectionError,
    SwayIpcAdapter,
    SwayMessageType,
    SwayWindow,
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


def test_run_command_raises_with_rejected_command_text() -> None:
    transport = StubSwayTransport(
        responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": False}]).encode()}
    )
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
                                    "marks": ["hop_browser:demo"],
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
            marks=("hop_browser:demo",),
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
    transport = StubSwayTransport(
        responses={SwayMessageType.RUN_COMMAND: json.dumps([{"success": True}]).encode()}
    )
    sway = SwayIpcAdapter(transport=transport)

    sway.focus_window(17)
    sway.move_window_to_workspace(17, "p:demo")
    sway.mark_window(17, "hop_browser:demo")

    assert transport.requests == [
        (SwayMessageType.RUN_COMMAND, b"[con_id=17] focus"),
        (SwayMessageType.RUN_COMMAND, b'[con_id=17] move container to workspace "p:demo"'),
        (SwayMessageType.RUN_COMMAND, b'[con_id=17] mark --add "hop_browser:demo"'),
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
