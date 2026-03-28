from pathlib import Path

from hop.commands.session import enter_project_session, list_sessions, switch_session
from hop.errors import HopError
from hop.session import ProjectSession


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

    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
        self.ensured_terminals.append((session.session_name, role, session.project_root))


def test_enter_project_session_switches_to_workspace_and_bootstraps_shell(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()

    session = enter_project_session(nested_directory, sway=sway, terminals=terminals)

    assert session.session_name == "src"
    assert sway.switched_workspaces == [f"p:{nested_directory}"]
    assert terminals.ensured_terminals == [("src", "shell", nested_directory)]


def test_enter_project_session_reuses_the_same_directory_session_on_repeat_invocation(tmp_path: Path) -> None:
    session_root = tmp_path / "demo" / "src"
    session_root.mkdir(parents=True)

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()

    first_session = enter_project_session(session_root, sway=sway, terminals=terminals)
    second_session = enter_project_session(session_root, sway=sway, terminals=terminals)

    assert first_session == second_session
    assert sway.switched_workspaces == [f"p:{session_root}", f"p:{session_root}"]
    assert terminals.ensured_terminals == [
        ("src", "shell", session_root),
        ("src", "shell", session_root),
    ]


def test_enter_project_session_creates_distinct_sessions_for_same_basename_dirs(tmp_path: Path) -> None:
    dir_a = tmp_path / "project_a" / "myapp"
    dir_b = tmp_path / "project_b" / "myapp"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()

    session_a = enter_project_session(dir_a, sway=sway, terminals=terminals)
    session_b = enter_project_session(dir_b, sway=sway, terminals=terminals)

    assert session_a.session_name == session_b.session_name == "myapp"
    assert session_a.workspace_name != session_b.workspace_name
    assert sway.switched_workspaces[0] != sway.switched_workspaces[1]


def test_switch_session_finds_workspace_by_session_name() -> None:
    sway = StubSwayAdapter(workspaces=("p:/some/path/demo",))

    workspace_name = switch_session("demo", sway=sway)

    assert workspace_name == "p:/some/path/demo"
    assert sway.switched_workspaces == ["p:/some/path/demo"]


def test_switch_session_picks_first_alphabetically_when_multiple_sessions_share_name() -> None:
    sway = StubSwayAdapter(workspaces=("p:/path/b/myapp", "p:/path/a/myapp"))

    workspace_name = switch_session("myapp", sway=sway)

    assert workspace_name == "p:/path/a/myapp"
    assert sway.switched_workspaces == ["p:/path/a/myapp"]


def test_switch_session_raises_when_no_matching_session_exists() -> None:
    sway = StubSwayAdapter(workspaces=())

    raised = False
    try:
        switch_session("demo", sway=sway)
    except HopError:
        raised = True
    assert raised


def test_list_sessions_returns_sorted_session_names() -> None:
    sway = StubSwayAdapter(workspaces=("p:/sessions/zeta", "scratch", "p:/sessions/alpha", "p:/sessions/beta"))

    assert list_sessions(sway=sway) == ("alpha", "beta", "zeta")


def test_list_sessions_shows_full_paths_for_duplicate_session_names() -> None:
    sway = StubSwayAdapter(workspaces=("p:/path/a/myapp", "p:/path/b/myapp"))

    assert list_sessions(sway=sway) == ("/path/a/myapp", "/path/b/myapp")
