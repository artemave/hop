from pathlib import Path

from hop.commands.session import enter_project_session, list_sessions, switch_session


class StubSwayAdapter:
    def __init__(self, workspaces: tuple[str, ...] = ()) -> None:
        self.workspaces = workspaces
        self.switched_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return tuple(workspace for workspace in self.workspaces if workspace.startswith(prefix))


class StubTerminalAdapter:
    def __init__(self) -> None:
        self.ensured_terminals: list[tuple[str, str, Path]] = []

    def ensure_terminal(self, session, *, role: str) -> None:
        self.ensured_terminals.append((session.session_name, role, session.project_root))


def test_enter_project_session_switches_to_workspace_and_bootstraps_shell(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    (project_root / ".git").mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()

    session = enter_project_session(nested_directory, sway=sway, terminals=terminals)

    assert session.session_name == "demo"
    assert sway.switched_workspaces == ["p:demo"]
    assert terminals.ensured_terminals == [("demo", "shell", project_root)]


def test_switch_session_uses_workspace_name_derivation() -> None:
    sway = StubSwayAdapter()

    workspace_name = switch_session("demo", sway=sway)

    assert workspace_name == "p:demo"
    assert sway.switched_workspaces == ["p:demo"]


def test_list_sessions_returns_sorted_session_names() -> None:
    sway = StubSwayAdapter(workspaces=("p:zeta", "scratch", "p:alpha", "p:beta"))

    assert list_sessions(sway=sway) == ("alpha", "beta", "zeta")
