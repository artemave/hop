from pathlib import Path

from hop.editor import SharedNeovimEditorAdapter, _build_remote_open_command
from hop.session import ProjectSession


class StubKittyTransport:
    def __init__(self, responses: list[object], *, on_launch=None) -> None:
        self._responses = list(responses)
        self._on_launch = on_launch
        self.commands: list[tuple[str, dict[str, object] | None]] = []

    def send_command(self, command_name: str, payload: dict[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if command_name == "launch" and self._on_launch is not None and payload is not None:
            self._on_launch(payload)
        if not self._responses:
            return {"ok": True}
        return self._responses.pop(0)


class StubProcessRunner:
    def __init__(self) -> None:
        self.active_servers: set[str] = set()
        self.commands: list[list[str]] = []

    def activate(self, address: str) -> None:
        self.active_servers.add(address)

    def run(self, args: list[str]):
        from subprocess import CompletedProcess

        self.commands.append(args)

        if "--remote-expr" in args:
            address = args[args.index("--server") + 1]
            return CompletedProcess(args, 0 if address in self.active_servers else 1, "", "")

        if "--remote-send" in args:
            address = args[args.index("--server") + 1]
            return CompletedProcess(args, 0 if address in self.active_servers else 1, "", "")

        raise AssertionError(f"Unexpected process command: {args}")


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name="p:demo",
    )


def test_focus_reuses_existing_session_editor_window(tmp_path: Path) -> None:
    runner = StubProcessRunner()
    address = str((tmp_path / "hop" / "hop-demo.sock").resolve())
    runner.activate(address)
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
                                        "id": 23,
                                        "user_vars": {
                                            "hop_session": "demo",
                                            "hop_editor": "1",
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
    adapter = SharedNeovimEditorAdapter(
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=tmp_path / "hop",
    )

    adapter.focus(build_session())

    assert runner.commands == [
        ["nvim", "--server", address, "--remote-expr", "1"],
    ]
    assert transport.commands == [
        ("ls", {"output_format": "json"}),
        ("focus-window", {"match": "id:23"}),
    ]


def test_focus_recreates_editor_after_neovim_exits(tmp_path: Path) -> None:
    runner = StubProcessRunner()

    def activate_server(payload: dict[str, object]) -> None:
        args = payload["args"]
        assert isinstance(args, list)
        runner.activate(str(args[2]))

    transport = StubKittyTransport([{"ok": True}], on_launch=activate_server)
    runtime_dir = tmp_path / "hop"
    runtime_dir.mkdir()
    stale_socket = runtime_dir / "hop-demo.sock"
    stale_socket.write_text("stale")
    adapter = SharedNeovimEditorAdapter(
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=runtime_dir,
    )

    adapter.focus(build_session())

    assert not stale_socket.exists()
    assert runner.commands == [
        ["nvim", "--server", str(stale_socket), "--remote-expr", "1"],
        ["nvim", "--server", str(stale_socket), "--remote-expr", "1"],
    ]
    assert transport.commands == [
        (
            "launch",
            {
                "args": ["nvim", "--listen", str(stale_socket)],
                "cwd": str(build_session().project_root),
                "type": "os-window",
                "keep_focus": False,
                "allow_remote_control": True,
                "window_title": "demo:editor",
                "os_window_title": "demo:editor",
                "os_window_name": "hop:demo:editor",
                "env": [
                    "HOP_SESSION=demo",
                    f"HOP_PROJECT_ROOT={build_session().project_root}",
                    "HOP_EDITOR=1",
                ],
                "var": [
                    "hop_session=demo",
                    f"hop_project_root={build_session().project_root}",
                    "hop_editor=1",
                ],
            },
        ),
        ("ls", {"output_format": "json"}),
    ]


def test_open_target_focuses_editor_and_routes_path_with_line(tmp_path: Path) -> None:
    runner = StubProcessRunner()
    address = str((tmp_path / "hop" / "hop-demo.sock").resolve())
    runner.activate(address)
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
                                        "id": 31,
                                        "env": {
                                            "HOP_SESSION": "demo",
                                            "HOP_EDITOR": "1",
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
    adapter = SharedNeovimEditorAdapter(
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=tmp_path / "hop",
    )

    adapter.open_target(build_session(), target="app/models/user's file.rb:42")

    assert runner.commands == [
        ["nvim", "--server", address, "--remote-expr", "1"],
        [
            "nvim",
            "--server",
            address,
            "--remote-send",
            "<Cmd>execute 'drop ' . fnameescape('app/models/user''s file.rb')<CR><Cmd>42<CR>",
        ],
    ]
    assert transport.commands == [
        ("ls", {"output_format": "json"}),
        ("focus-window", {"match": "id:31"}),
    ]


def test_build_remote_open_command_preserves_plain_paths() -> None:
    assert (
        _build_remote_open_command("app/models/user.rb")
        == "<Cmd>execute 'drop ' . fnameescape('app/models/user.rb')<CR>"
    )
