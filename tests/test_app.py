import io
from contextlib import redirect_stdout
from pathlib import Path

from hop.app import HopServices, execute_command
from hop.commands import (
    EditCommand,
    EnterSessionCommand,
    ListSessionsCommand,
    RunCommand,
    SwitchSessionCommand,
    TermCommand,
)


class StubSwayAdapter:
    def __init__(self, workspaces: tuple[str, ...] = ()) -> None:
        self.workspaces = workspaces
        self.switched_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return tuple(workspace for workspace in self.workspaces if workspace.startswith(prefix))


class StubKittyAdapter:
    def __init__(self) -> None:
        self.ensured_roles: list[tuple[str, str]] = []
        self.runs: list[tuple[str, str, str]] = []

    def ensure_terminal(self, session, *, role: str) -> None:
        self.ensured_roles.append((session.session_name, role))

    def run_in_terminal(self, session, *, role: str, command: str) -> None:
        self.runs.append((session.session_name, role, command))


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.focused_sessions: list[str] = []
        self.opened_targets: list[tuple[str, str]] = []

    def focus(self, session) -> None:
        self.focused_sessions.append(session.session_name)

    def open_target(self, session, *, target: str) -> None:
        self.opened_targets.append((session.session_name, target))


class StubBrowserAdapter:
    def ensure_browser(self, session, *, url: str | None) -> None:
        raise AssertionError("Browser should not be called in these tests")


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
    (project_root / ".dust").mkdir()

    services = build_services()

    assert execute_command(EnterSessionCommand(), cwd=nested_directory, services=services) == 0
    assert services.sway.switched_workspaces == ["p:demo"]
    assert services.kitty.ensured_roles == [("demo", "shell")]


def test_execute_command_switches_to_named_session() -> None:
    services = build_services()

    assert execute_command(SwitchSessionCommand(session_name="demo"), cwd=Path("/tmp"), services=services) == 0
    assert services.sway.switched_workspaces == ["p:demo"]


def test_execute_command_lists_sorted_session_names() -> None:
    services = build_services(workspaces=("p:zeta", "workspace", "p:alpha"))
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert execute_command(ListSessionsCommand(), cwd=Path("/tmp"), services=services) == 0

    assert stdout.getvalue() == "alpha\nzeta\n"


def test_execute_command_focuses_terminal_role_in_current_session(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    (project_root / ".git").mkdir()

    services = build_services()

    assert execute_command(TermCommand(role="test"), cwd=nested_directory, services=services) == 0
    assert services.sway.switched_workspaces == ["p:demo"]
    assert services.kitty.ensured_roles == [("demo", "test")]


def test_execute_command_routes_run_commands_to_role_terminal(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    (project_root / ".git").mkdir()

    services = build_services()

    assert (
        execute_command(
            RunCommand(role="server", command_text="bin/dev"),
            cwd=nested_directory,
            services=services,
        )
        == 0
    )
    assert services.sway.switched_workspaces == ["p:demo"]
    assert services.kitty.runs == [("demo", "server", "bin/dev")]


def test_execute_command_focuses_shared_editor_in_current_session(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    (project_root / ".git").mkdir()

    services = build_services()

    assert execute_command(EditCommand(), cwd=nested_directory, services=services) == 0
    assert services.sway.switched_workspaces == ["p:demo"]
    assert services.neovim.focused_sessions == ["demo"]


def test_execute_command_routes_edit_targets_to_shared_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    (project_root / ".dust").mkdir()

    services = build_services()

    assert (
        execute_command(
            EditCommand(target="app/models/user.rb:42"),
            cwd=nested_directory,
            services=services,
        )
        == 0
    )
    assert services.sway.switched_workspaces == ["p:demo"]
    assert services.neovim.opened_targets == [("demo", "app/models/user.rb:42")]
