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
from hop.state import SessionState


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


class StubEditorAdapter:
    def __init__(self, *, editor_was_closed: bool = False) -> None:
        self.ensured: list[str] = []
        # Whether ensure() should report "I just launched a new editor".
        # spawn_session_terminal short-circuits when this is True so the
        # user gets exactly one window back — the editor — instead of
        # editor + a new shell.
        self._editor_was_closed = editor_was_closed

    def ensure(self, session: ProjectSession) -> bool:
        self.ensured.append(session.session_name)
        return self._editor_was_closed


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


def test_enter_project_session_ensures_editor_when_one_is_supplied(tmp_path: Path) -> None:
    """Bootstrap path: callers (app.py on first entry) pass an editor
    adapter so the new session comes up with both shell and editor."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    enter_project_session(project_root, sway=sway, terminals=terminals, editor=editor)

    assert editor.ensured == ["demo"]
    assert terminals.ensured_terminals == [("demo", "shell", project_root)]


def test_enter_project_session_launches_terminal_before_editor(tmp_path: Path) -> None:
    """Order matters: the per-session kitty isn't running on first entry,
    and only ensure_terminal knows how to bootstrap it. The editor adapter
    talks to the kitty socket directly with no fallback — if it runs first,
    the call fails with `Could not talk to Kitty over unix:.../...sock`."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    call_log: list[str] = []

    class OrderedTerminalAdapter(StubTerminalAdapter):
        def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
            call_log.append("terminal")
            super().ensure_terminal(session, role=role)

    class OrderedEditorAdapter(StubEditorAdapter):
        def ensure(self, session: ProjectSession) -> bool:
            call_log.append("editor")
            return super().ensure(session)

    enter_project_session(
        project_root,
        sway=StubSwayAdapter(),
        terminals=OrderedTerminalAdapter(),
        editor=OrderedEditorAdapter(),
    )

    assert call_log == ["terminal", "editor"]


def test_enter_project_session_does_not_touch_editor_on_re_entry(tmp_path: Path) -> None:
    """Re-entry from another workspace must not resurrect an editor the
    user deliberately closed — callers signal that by passing editor=None."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    enter_project_session(project_root, sway=sway, terminals=terminals, editor=None)

    assert editor.ensured == []


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


def _make_window(*, role: str) -> KittyWindow:
    return KittyWindow(id=0, role=role)


def test_spawn_session_terminal_picks_first_unused_shell_role(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    terminals = StubTerminalAdapter(existing_windows=(_make_window(role="shell"),))
    editor = StubEditorAdapter()

    session = spawn_session_terminal(project_root, terminals=terminals, editor=editor)

    assert session.session_name == "demo"
    assert terminals.ensured_terminals == [("demo", "shell-2", project_root)]


def test_spawn_session_terminal_skips_used_numbered_shells(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    terminals = StubTerminalAdapter(
        existing_windows=(
            _make_window(role="shell"),
            _make_window(role="shell-2"),
            _make_window(role="shell-3"),
        ),
    )
    editor = StubEditorAdapter()

    spawn_session_terminal(project_root, terminals=terminals, editor=editor)

    assert terminals.ensured_terminals == [("demo", "shell-4", project_root)]


def test_spawn_session_terminal_does_not_switch_workspace(tmp_path: Path) -> None:
    """Spawning a new terminal from inside a session does not switch the workspace —
    the caller is already on p:<session>."""
    project_root = tmp_path / "demo"
    project_root.mkdir()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    spawn_session_terminal(project_root, terminals=terminals, editor=editor)

    assert terminals.ensured_terminals == [("demo", "shell-2", project_root)]


def test_spawn_session_terminal_resurrects_a_closed_editor_without_extra_shell(
    tmp_path: Path,
) -> None:
    """When the editor was closed, `hop` should bring back exactly one
    window — the editor — not editor + another shell. Spawning both at
    once would clutter the workspace whenever the user's intent was just
    to recover the editor."""
    project_root = tmp_path / "demo"
    project_root.mkdir()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter(editor_was_closed=True)

    spawn_session_terminal(project_root, terminals=terminals, editor=editor)

    assert editor.ensured == ["demo"]
    assert terminals.ensured_terminals == []


def test_spawn_session_terminal_spawns_shell_when_editor_already_open(tmp_path: Path) -> None:
    """When the editor is already up, `hop` falls through to spawning a
    numbered shell — the user's other reason for invoking `hop` from
    inside an existing session."""
    project_root = tmp_path / "demo"
    project_root.mkdir()
    terminals = StubTerminalAdapter(existing_windows=(_make_window(role="shell"),))
    editor = StubEditorAdapter(editor_was_closed=False)

    spawn_session_terminal(project_root, terminals=terminals, editor=editor)

    assert editor.ensured == ["demo"]
    assert terminals.ensured_terminals == [("demo", "shell-2", project_root)]


def test_list_sessions_returns_sorted_listings_with_workspace_and_known_project_roots() -> None:
    sway = StubSwayAdapter(workspaces=("p:zeta", "scratch", "p:alpha", "p:beta"))

    listings = list_sessions(
        sway=sway,
        sessions_loader=lambda: {
            "alpha": SessionState(name="alpha", project_root=Path("/projects/alpha")),
            "beta": SessionState(name="beta", project_root=Path("/projects/beta")),
        },
    )

    assert [listing.name for listing in listings] == ["alpha", "beta", "zeta"]
    assert [listing.workspace for listing in listings] == ["p:alpha", "p:beta", "p:zeta"]
    assert [listing.project_root for listing in listings] == [
        Path("/projects/alpha"),
        Path("/projects/beta"),
        None,
    ]
