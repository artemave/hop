import io
from contextlib import redirect_stdout
from pathlib import Path

from hop.app import HopServices, execute_command
from hop.commands import (
    BrowserCommand,
    EditCommand,
    EnterSessionCommand,
    KillCommand,
    ListSessionsCommand,
    RunCommand,
    SwitchSessionCommand,
    TermCommand,
)
from hop.sway import SwayWindow


class StubSwayAdapter:
    def __init__(self, workspaces: tuple[str, ...] = ()) -> None:
        self.workspaces = workspaces
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


class StubKittyAdapter:
    def __init__(self) -> None:
        self.ensured_roles: list[tuple[str, str]] = []
        self.runs: list[tuple[str, str, str]] = []
        self.closed_windows: list[int] = []

    def ensure_terminal(self, session, *, role: str) -> None:
        self.ensured_roles.append((session.session_name, role))

    def run_in_terminal(self, session, *, role: str, command: str) -> None:
        self.runs.append((session.session_name, role, command))

    def list_session_windows(self, session) -> list[object]:
        return []

    def close_window(self, window_id: int) -> None:
        self.closed_windows.append(window_id)


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.focused_sessions: list[str] = []
        self.opened_targets: list[tuple[str, str]] = []

    def focus(self, session) -> None:
        self.focused_sessions.append(session.session_name)

    def open_target(self, session, *, target: str) -> None:
        self.opened_targets.append((session.session_name, target))


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, str | None]] = []

    def ensure_browser(self, session, *, url: str | None) -> None:
        self.calls.append((session.session_name, session.project_root, url))


def build_services(*, workspaces: tuple[str, ...] = ()) -> HopServices:
    return HopServices(
        sway=StubSwayAdapter(workspaces=workspaces),
        kitty=StubKittyAdapter(),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
    )


def test_execute_command_enters_project_session_and_bootstraps_shell(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert execute_command(EnterSessionCommand(), cwd=nested_directory, services=services) == 0
    assert services.sway.switched_workspaces == [f"p:{nested_directory}"]
    assert services.kitty.ensured_roles == [("src", "shell")]


def test_execute_command_switches_to_named_session() -> None:
    services = build_services(workspaces=(f"p:/some/path/demo",))

    assert execute_command(SwitchSessionCommand(session_name="demo"), cwd=Path("/tmp"), services=services) == 0
    assert services.sway.switched_workspaces == ["p:/some/path/demo"]


def test_execute_command_lists_sorted_session_names() -> None:
    services = build_services(workspaces=("p:/sessions/zeta", "workspace", "p:/sessions/alpha"))
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert execute_command(ListSessionsCommand(), cwd=Path("/tmp"), services=services) == 0

    assert stdout.getvalue() == "alpha\nzeta\n"


def test_execute_command_focuses_terminal_role_in_current_session(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert execute_command(TermCommand(role="test"), cwd=nested_directory, services=services) == 0
    assert services.sway.switched_workspaces == [f"p:{nested_directory}"]
    assert services.kitty.ensured_roles == [("src", "test")]


def test_execute_command_routes_run_commands_to_role_terminal(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert (
        execute_command(
            RunCommand(role="server", command_text="bin/dev"),
            cwd=nested_directory,
            services=services,
        )
        == 0
    )
    assert services.sway.switched_workspaces == [f"p:{nested_directory}"]
    assert services.kitty.runs == [("src", "server", "bin/dev")]


def test_execute_command_focuses_shared_editor_in_current_session(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert execute_command(EditCommand(), cwd=nested_directory, services=services) == 0
    assert services.sway.switched_workspaces == [f"p:{nested_directory}"]
    assert services.neovim.focused_sessions == ["src"]


def test_execute_command_routes_edit_targets_to_shared_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert (
        execute_command(
            EditCommand(target="app/models/user.rb:42"),
            cwd=nested_directory,
            services=services,
        )
        == 0
    )
    assert services.sway.switched_workspaces == [f"p:{nested_directory}"]
    assert services.neovim.opened_targets == [("src", "app/models/user.rb:42")]


def test_execute_command_uses_invocation_directory_for_browser_sessions(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert (
        execute_command(
            BrowserCommand(url="https://example.com"),
            cwd=nested_directory,
            services=services,
        )
        == 0
    )
    assert services.sway.switched_workspaces == [f"p:{nested_directory}"]
    assert services.browser.calls == [("src", nested_directory.resolve(), "https://example.com")]


def test_execute_command_creates_distinct_sessions_for_same_basename_directories(tmp_path: Path) -> None:
    dir_a = tmp_path / "project_a" / "myapp"
    dir_b = tmp_path / "project_b" / "myapp"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    services_a = build_services()
    services_b = build_services()

    assert execute_command(EnterSessionCommand(), cwd=dir_a, services=services_a) == 0
    assert execute_command(EnterSessionCommand(), cwd=dir_b, services=services_b) == 0

    assert services_a.sway.switched_workspaces != services_b.sway.switched_workspaces


def test_execute_command_kills_managed_windows_and_removes_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    workspace_name = f"p:{project_root.resolve()}"

    services = build_services(workspaces=(workspace_name,))

    assert execute_command(KillCommand(), cwd=project_root, services=services) == 0
    assert services.sway.removed_workspaces == [workspace_name]
