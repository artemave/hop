from pathlib import Path
from typing import Mapping

from hop.kitty import KittyRemoteControlAdapter
from hop.session import ProjectSession


class StubKittyTransport:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.commands: list[tuple[str, Mapping[str, object] | None]] = []

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if not self._responses:
            return {"ok": True}
        return self._responses.pop(0)


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name=f"p:{project_root}",
    )


def test_ensure_terminal_focuses_existing_role_window() -> None:
    transport = StubKittyTransport(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 17,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "test",
                                            "hop_project_root": str(build_session().project_root),
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
            {"ok": True},
        ]
    )
    adapter = KittyRemoteControlAdapter(transport=transport)

    adapter.ensure_terminal(build_session(), role="test")

    assert transport.commands == [
        ("ls", {"output_format": "json"}),
        ("focus-window", {"match": "id:17"}),
    ]


def test_ensure_terminal_launches_os_window_when_role_is_missing() -> None:
    transport = StubKittyTransport(
        [
            {"ok": True, "data": []},
            {"ok": True},
        ]
    )
    adapter = KittyRemoteControlAdapter(transport=transport)

    adapter.ensure_terminal(build_session(), role="server")

    assert transport.commands == [
        ("ls", {"output_format": "json"}),
        (
            "launch",
            {
                "args": [],
                "cwd": str(build_session().project_root),
                "type": "os-window",
                "keep_focus": False,
                "allow_remote_control": True,
                "window_title": "demo:server",
                "os_window_title": "demo:server",
                "os_window_name": "hop:demo:server",
                "env": [
                    "HOP_SESSION=demo",
                    "HOP_ROLE=server",
                    f"HOP_PROJECT_ROOT={build_session().project_root}",
                ],
                "var": [
                    "hop_session=demo",
                    "hop_role=server",
                    f"hop_project_root={build_session().project_root}",
                ],
            },
        ),
    ]


def test_run_in_terminal_reuses_existing_role_window() -> None:
    transport = StubKittyTransport(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 9,
                                        "env": {
                                            "HOP_SESSION": "demo",
                                            "HOP_ROLE": "shell",
                                            "HOP_PROJECT_ROOT": str(build_session().project_root),
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
            {"ok": True},
        ]
    )
    adapter = KittyRemoteControlAdapter(transport=transport)

    adapter.run_in_terminal(build_session(), role="shell", command="pytest -q")

    assert transport.commands == [
        ("ls", {"output_format": "json"}),
        ("send-text", {"match": "id:9", "data": "text:pytest -q\n"}),
    ]


def test_run_in_terminal_creates_missing_role_window_and_routes_command() -> None:
    transport = StubKittyTransport(
        [
            {"ok": True, "data": []},
            {"ok": True},
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 13,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "test",
                                            "hop_project_root": str(build_session().project_root),
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
            {"ok": True},
        ]
    )
    adapter = KittyRemoteControlAdapter(transport=transport)

    adapter.run_in_terminal(build_session(), role="test", command="bin/test")

    assert transport.commands == [
        ("ls", {"output_format": "json"}),
        (
            "launch",
            {
                "args": [],
                "cwd": str(build_session().project_root),
                "type": "os-window",
                "keep_focus": True,
                "allow_remote_control": True,
                "window_title": "demo:test",
                "os_window_title": "demo:test",
                "os_window_name": "hop:demo:test",
                "env": [
                    "HOP_SESSION=demo",
                    "HOP_ROLE=test",
                    f"HOP_PROJECT_ROOT={build_session().project_root}",
                ],
                "var": [
                    "hop_session=demo",
                    "hop_role=test",
                    f"hop_project_root={build_session().project_root}",
                ],
            },
        ),
        ("ls", {"output_format": "json"}),
        ("send-text", {"match": "id:13", "data": "text:bin/test\n"}),
    ]


def test_inspect_window_reads_project_root_and_cwd_from_kitty_metadata() -> None:
    project_root = (Path("/tmp/demo")).resolve()
    terminal_cwd = (project_root / "src").resolve()
    transport = StubKittyTransport(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 17,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "shell",
                                            "hop_project_root": str(project_root),
                                        },
                                        "foreground_processes": [
                                            {
                                                "cwd": str(terminal_cwd),
                                            }
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
    )
    adapter = KittyRemoteControlAdapter(transport=transport)

    window = adapter.inspect_window(17)

    assert transport.commands == [
        ("ls", {"match": "id:17", "output_format": "json", "all_env_vars": True}),
    ]
    assert window is not None
    assert window.session_name == "demo"
    assert window.role == "shell"
    assert window.project_root == project_root
    assert window.cwd == terminal_cwd


def test_list_session_windows_returns_all_windows_for_session_project_root() -> None:
    other_root = Path("/tmp/other/demo").resolve()
    transport = StubKittyTransport(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 1,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "shell",
                                            "hop_project_root": str(build_session().project_root),
                                        },
                                    },
                                    {
                                        "id": 2,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "test",
                                            "hop_project_root": str(build_session().project_root),
                                        },
                                    },
                                    {
                                        "id": 3,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "shell",
                                            "hop_project_root": str(other_root),
                                        },
                                    },
                                ]
                            }
                        ]
                    }
                ],
            }
        ]
    )
    adapter = KittyRemoteControlAdapter(transport=transport)

    windows = adapter.list_session_windows(build_session())

    assert {w.id for w in windows} == {1, 2}


def test_close_window_sends_close_window_command() -> None:
    transport = StubKittyTransport([{"ok": True}])
    adapter = KittyRemoteControlAdapter(transport=transport)

    adapter.close_window(17)

    assert transport.commands == [("close-window", {"match": "id:17"})]


def test_ensure_terminal_does_not_reuse_window_from_different_directory_with_same_basename() -> None:
    other_project_root = Path("/tmp/other/demo").resolve()
    transport = StubKittyTransport(
        [
            {
                "ok": True,
                "data": [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "id": 42,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_role": "shell",
                                            "hop_project_root": str(other_project_root),
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
            {"ok": True},
        ]
    )
    adapter = KittyRemoteControlAdapter(transport=transport)

    adapter.ensure_terminal(build_session(), role="shell")

    assert transport.commands[1][0] == "launch"
