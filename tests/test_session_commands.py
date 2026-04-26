from pathlib import Path

from hop.commands.session import (
    enter_project_session,
    list_sessions,
    spawn_session_terminal,
    switch_session,
)
from hop.errors import HopError
from hop.kitty import KittyWindow
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
    def __init__(self, *, existing_windows: tuple[KittyWindow, ...] = ()) -> None:
        self.ensured_terminals: list[tuple[str, str, Path]] = []
        self._existing_windows = existing_windows

    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
        self.ensured_terminals.append((session.session_name, role, session.project_root))

    def list_session_windows(self, session: ProjectSession) -> tuple[KittyWindow, ...]:
        return self._existing_windows


def test_enter_project_session_switches_to_workspace_and_bootstraps_shell(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()

    session = enter_project_session(nested_directory, sway=sway, terminals=terminals)

    assert session.session_name == "src"
    assert sway.switched_workspaces == [f"p:{nested_directory.name}"]
    assert terminals.ensured_terminals == [("src", "shell", nested_directory)]


def test_enter_project_session_reuses_the_same_directory_session_on_repeat_invocation(tmp_path: Path) -> None:
    session_root = tmp_path / "demo" / "src"
    session_root.mkdir(parents=True)

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()

    first_session = enter_project_session(session_root, sway=sway, terminals=terminals)
    second_session = enter_project_session(session_root, sway=sway, terminals=terminals)

    assert first_session == second_session
    assert sway.switched_workspaces == [f"p:{session_root.name}", f"p:{session_root.name}"]
    assert terminals.ensured_terminals == [
        ("src", "shell", session_root),
        ("src", "shell", session_root),
    ]


def test_switch_session_finds_workspace_by_session_name() -> None:
    sway = StubSwayAdapter(workspaces=("p:demo",))

    workspace_name = switch_session("demo", sway=sway)

    assert workspace_name == "p:demo"
    assert sway.switched_workspaces == ["p:demo"]


def test_switch_session_raises_when_no_matching_session_exists() -> None:
    sway = StubSwayAdapter(workspaces=())

    raised = False
    try:
        switch_session("demo", sway=sway)
    except HopError:
        raised = True
    assert raised


def _make_window(*, role: str, project_root: Path) -> KittyWindow:
    return KittyWindow(id=0, session_name=project_root.name, role=role, project_root=project_root)


def test_spawn_session_terminal_picks_first_unused_shell_role(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    terminals = StubTerminalAdapter(
        existing_windows=(_make_window(role="shell", project_root=project_root),),
    )

    session = spawn_session_terminal(project_root, terminals=terminals)

    assert session.session_name == "demo"
    assert terminals.ensured_terminals == [("demo", "shell-2", project_root)]


def test_spawn_session_terminal_skips_used_numbered_shells(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    terminals = StubTerminalAdapter(
        existing_windows=(
            _make_window(role="shell", project_root=project_root),
            _make_window(role="shell-2", project_root=project_root),
            _make_window(role="shell-3", project_root=project_root),
        ),
    )

    spawn_session_terminal(project_root, terminals=terminals)

    assert terminals.ensured_terminals == [("demo", "shell-4", project_root)]


def test_spawn_session_terminal_does_not_switch_workspace(tmp_path: Path) -> None:
    """Spawning a new terminal from inside a session does not switch the workspace —
    the caller is already on p:<session>."""
    project_root = tmp_path / "demo"
    project_root.mkdir()
    terminals = StubTerminalAdapter()

    spawn_session_terminal(project_root, terminals=terminals)

    assert terminals.ensured_terminals == [("demo", "shell-2", project_root)]


def test_list_sessions_returns_sorted_session_names() -> None:
    sway = StubSwayAdapter(workspaces=("p:zeta", "scratch", "p:alpha", "p:beta"))

    assert list_sessions(sway=sway) == ("alpha", "beta", "zeta")
