import io
from contextlib import redirect_stdout
from pathlib import Path

from hop.app import HopServices, execute_command
from hop.commands import EnterSessionCommand, ListSessionsCommand, SwitchSessionCommand


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

    def ensure_terminal(self, session, *, role: str) -> None:
        self.ensured_roles.append((session.session_name, role))

    def run_in_terminal(self, session, *, role: str, command: str) -> None:
        raise AssertionError("run_in_terminal should not be called in these tests")


class StubNeovimAdapter:
    def focus(self, session) -> None:
        raise AssertionError("Neovim should not be called in these tests")

    def open_target(self, session, *, target: str) -> None:
        raise AssertionError("Neovim should not be called in these tests")


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
