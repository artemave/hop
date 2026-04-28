import hashlib
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from hop.editor import SharedNeovimEditorAdapter, build_remote_open_command
from hop.session import ProjectSession


class StubKittyTransport:
    def __init__(self, responses: list[object], *, on_launch: object = None) -> None:
        self._responses = list(responses)
        self._on_launch = on_launch
        self.commands: list[tuple[str, Mapping[str, object] | None]] = []

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if command_name == "launch" and callable(self._on_launch) and payload is not None:
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

    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args_list = list(args)
        self.commands.append(args_list)

        if "--remote-expr" in args_list:
            address = args_list[args_list.index("--server") + 1]
            return subprocess.CompletedProcess(args_list, 0 if address in self.active_servers else 1, "", "")

        if "--remote-send" in args_list:
            address = args_list[args_list.index("--server") + 1]
            return subprocess.CompletedProcess(args_list, 0 if address in self.active_servers else 1, "", "")

        raise AssertionError(f"Unexpected process command: {args}")


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name=f"p:{project_root}",
    )


def _session_socket_name(project_root: Path) -> str:
    root_hash = hashlib.sha256(str(project_root).encode()).hexdigest()[:16]
    return f"hop-{root_hash}.sock"


def test_focus_reuses_existing_session_editor_window(tmp_path: Path) -> None:
    runner = StubProcessRunner()
    project_root = build_session().project_root
    socket_name = _session_socket_name(project_root)
    address = str((tmp_path / "hop" / socket_name).resolve())
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
                                        "user_vars": {"hop_editor": "1"},
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
        runner.activate(str(cast(list[Any], args)[2]))

    transport = StubKittyTransport([{"ok": True}], on_launch=activate_server)
    runtime_dir = tmp_path / "hop"
    runtime_dir.mkdir()
    project_root = build_session().project_root
    socket_name = _session_socket_name(project_root)
    stale_socket = runtime_dir / socket_name
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
                "window_title": "editor",
                "os_window_title": "editor",
                "os_window_name": "hop:demo:editor",
                "var": ["hop_editor=1"],
            },
        ),
        ("ls", {"output_format": "json"}),
    ]


def test_open_target_focuses_editor_and_routes_path_with_line(tmp_path: Path) -> None:
    runner = StubProcessRunner()
    project_root = build_session().project_root
    socket_name = _session_socket_name(project_root)
    address = str((tmp_path / "hop" / socket_name).resolve())
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
                                        "user_vars": {"hop_editor": "1"},
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


def testbuild_remote_open_command_preserves_plain_paths() -> None:
    assert (
        build_remote_open_command("app/models/user.rb")
        == "<Cmd>execute 'drop ' . fnameescape('app/models/user.rb')<CR>"
    )


def test_editor_uses_distinct_sockets_for_same_basename_directories(tmp_path: Path) -> None:
    project_root_a = Path("/tmp/project_a/myapp").resolve()
    project_root_b = Path("/tmp/project_b/myapp").resolve()

    socket_a = _session_socket_name(project_root_a)
    socket_b = _session_socket_name(project_root_b)

    assert socket_a != socket_b
