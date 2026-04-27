import io
import json
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pytest
from hop.app import HopServices, execute_command
from hop.commands import (
    BrowserCommand,
    EditCommand,
    EnterSessionCommand,
    KillCommand,
    ListSessionsCommand,
    RunCommand,
    SwitchSessionCommand,
    TailCommand,
    TermCommand,
)
from hop.kitty import KittyRemoteControlAdapter, KittyWindow, KittyWindowContext, KittyWindowState
from hop.session import ProjectSession
from hop.sway import SwayWindow


class StubSwayAdapter:
    def __init__(self, workspaces: tuple[str, ...] = (), *, focused_workspace: str = "") -> None:
        self.workspaces = workspaces
        self.focused_workspace = focused_workspace
        self.switched_workspaces: list[str] = []
        self.closed_windows: list[int] = []
        self.removed_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return tuple(workspace for workspace in self.workspaces if workspace.startswith(prefix))

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return ()

    def close_window(self, window_id: int) -> None:
        self.closed_windows.append(window_id)

    def remove_workspace(self, workspace_name: str) -> None:
        self.removed_workspaces.append(workspace_name)

    def get_focused_workspace(self) -> str:
        return self.focused_workspace


class StubKittyAdapter:
    def __init__(self, *, last_cmd_output: str = "") -> None:
        self.ensured_roles: list[tuple[str, str, Path]] = []
        self.runs: list[tuple[str, str, str, Path]] = []
        self.closed_windows: list[int] = []
        self._last_cmd_output = last_cmd_output
        self._state_calls = 0

    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
        self.ensured_roles.append((session.session_name, role, session.project_root))

    def run_in_terminal(self, session: ProjectSession, *, role: str, command: str) -> int:
        self.runs.append((session.session_name, role, command, session.project_root))
        return 0

    def inspect_window(self, window_id: int) -> KittyWindowContext | None:
        return None

    def list_session_windows(self, session: ProjectSession) -> list[KittyWindow]:
        return []

    def close_window(self, window_id: int) -> None:
        self.closed_windows.append(window_id)

    def get_window_state(self, window_id: int) -> KittyWindowState:
        self._state_calls += 1
        return KittyWindowState(at_prompt=self._state_calls > 1, last_cmd_exit_status=0)

    def get_last_cmd_output(self, window_id: int) -> str:
        return self._last_cmd_output


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.focused_sessions: list[tuple[str, Path]] = []
        self.opened_targets: list[tuple[str, str, Path]] = []

    def focus(self, session: ProjectSession) -> None:
        self.focused_sessions.append((session.session_name, session.project_root))

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        self.opened_targets.append((session.session_name, target, session.project_root))


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, str | None]] = []

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        self.calls.append((session.session_name, session.project_root, url))


@dataclass
class StubHopServices:
    sway: StubSwayAdapter
    kitty: StubKittyAdapter
    neovim: StubNeovimAdapter
    browser: StubBrowserAdapter

    def as_services(self) -> HopServices:
        return HopServices(sway=self.sway, kitty=self.kitty, neovim=self.neovim, browser=self.browser)


def build_services(
    *,
    workspaces: tuple[str, ...] = (),
    focused_workspace: str = "",
    last_cmd_output: str = "",
) -> StubHopServices:
    return StubHopServices(
        sway=StubSwayAdapter(workspaces=workspaces, focused_workspace=focused_workspace),
        kitty=StubKittyAdapter(last_cmd_output=last_cmd_output),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
    )


class CapturingKittyTransport:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.commands: list[tuple[str, Mapping[str, object] | None]] = []

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if not self._responses:
            return {"ok": True}
        return self._responses.pop(0)


def test_hop_enter_session_passes_invocation_directory_as_kitty_launch_cwd(
    tmp_path: Path,
) -> None:
    """End-to-end: cli `hop` from a directory must produce a kitty launch payload
    whose cwd is that exact directory."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    transport = CapturingKittyTransport([{"ok": True, "data": []}, {"ok": True}])
    services = HopServices(
        sway=StubSwayAdapter(),
        kitty=KittyRemoteControlAdapter(transport=transport),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
    )

    assert execute_command(EnterSessionCommand(), cwd=project_root, services=services) == 0

    launches = [payload for name, payload in transport.commands if name == "launch"]
    assert len(launches) == 1
    payload = launches[0]
    assert payload is not None
    assert payload["cwd"] == str(project_root.resolve())


def test_execute_command_enters_project_session_and_bootstraps_shell(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert execute_command(EnterSessionCommand(), cwd=nested_directory, services=services.as_services()) == 0
    assert services.sway.switched_workspaces == [f"p:{nested_directory.name}"]
    assert services.kitty.ensured_roles == [("src", "shell", nested_directory.resolve())]


def test_execute_command_spawns_extra_shell_when_focused_on_session_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    # Sway reports we're already focused on this session's workspace, so bare
    # `hop` should spawn another shell rather than re-enter.
    services = build_services(focused_workspace="p:demo")

    assert (
        execute_command(
            EnterSessionCommand(),
            cwd=project_root,
            services=services.as_services(),
        )
        == 0
    )
    assert services.sway.switched_workspaces == []
    assert services.kitty.ensured_roles == [("demo", "shell-2", project_root.resolve())]


def test_execute_command_enters_session_when_focused_on_a_different_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    services = build_services(focused_workspace="p:other")

    assert (
        execute_command(
            EnterSessionCommand(),
            cwd=project_root,
            services=services.as_services(),
        )
        == 0
    )
    assert services.sway.switched_workspaces == ["p:demo"]
    assert services.kitty.ensured_roles == [("demo", "shell", project_root.resolve())]


def test_execute_command_switches_to_named_session() -> None:
    services = build_services(workspaces=("p:demo",))

    result = execute_command(
        SwitchSessionCommand(session_name="demo"), cwd=Path("/tmp"), services=services.as_services()
    )
    assert result == 0
    assert services.sway.switched_workspaces == ["p:demo"]


def test_execute_command_lists_sorted_session_names() -> None:
    services = build_services(workspaces=("p:zeta", "workspace", "p:alpha"))
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert execute_command(ListSessionsCommand(), cwd=Path("/tmp"), services=services.as_services()) == 0

    assert stdout.getvalue() == "alpha\nzeta\n"


def test_execute_command_focuses_terminal_role_in_current_session(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert execute_command(TermCommand(role="test"), cwd=nested_directory, services=services.as_services()) == 0
    assert services.sway.switched_workspaces == []
    assert services.kitty.ensured_roles == [("src", "test", nested_directory.resolve())]


def test_execute_command_routes_run_commands_to_role_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    monkeypatch.setenv("HOP_RUNS_DIR", str(tmp_path / "runs"))

    services = build_services()
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert (
            execute_command(
                RunCommand(role="server", command_text="bin/dev"),
                cwd=nested_directory,
                services=services.as_services(),
            )
            == 0
        )
    assert services.sway.switched_workspaces == []
    assert services.kitty.runs == [("src", "server", "bin/dev", nested_directory.resolve())]
    run_id = stdout.getvalue().strip()
    assert run_id
    assert (tmp_path / "runs" / f"{run_id}.json").is_file()


def test_execute_command_tails_run_output_to_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "abc.json").write_text(
        json.dumps({"window_id": 1, "session": "demo", "role": "test", "dispatched_at": 0.0})
    )
    monkeypatch.setenv("HOP_RUNS_DIR", str(runs_dir))

    services = build_services(last_cmd_output="hello\n")
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert (
            execute_command(
                TailCommand(run_id="abc"),
                cwd=tmp_path,
                services=services.as_services(),
            )
            == 0
        )

    assert stdout.getvalue() == "hello\n"


def test_execute_command_focuses_shared_editor_in_current_session(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert execute_command(EditCommand(), cwd=nested_directory, services=services.as_services()) == 0
    assert services.sway.switched_workspaces == []
    assert services.neovim.focused_sessions == [("src", nested_directory.resolve())]


def test_execute_command_routes_edit_targets_to_shared_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert (
        execute_command(
            EditCommand(target="app/models/user.rb:42"),
            cwd=nested_directory,
            services=services.as_services(),
        )
        == 0
    )
    assert services.sway.switched_workspaces == []
    assert services.neovim.opened_targets == [("src", "app/models/user.rb:42", nested_directory.resolve())]


def test_execute_command_uses_invocation_directory_for_browser_sessions(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert (
        execute_command(
            BrowserCommand(url="https://example.com"),
            cwd=nested_directory,
            services=services.as_services(),
        )
        == 0
    )
    assert services.sway.switched_workspaces == []
    assert services.browser.calls == [("src", nested_directory.resolve(), "https://example.com")]


def test_execute_command_kills_managed_windows_and_removes_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    workspace_name = f"p:{project_root.name}"

    services = build_services(workspaces=(workspace_name,))

    assert execute_command(KillCommand(), cwd=project_root, services=services.as_services()) == 0
    assert services.sway.removed_workspaces == [workspace_name]
