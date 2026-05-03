from pathlib import Path

from hop.commands.session import (
    enter_project_session,
    list_sessions,
    spawn_session_terminal,
    switch_session,
)
from hop.errors import HopError
from hop.kitty import KittyWindow
from hop.layouts import WindowSpec
from hop.session import ProjectSession
from hop.state import SessionState
from hop.sway import SwayWindow


class StubSwayAdapter:
    def __init__(
        self,
        workspaces: tuple[str, ...] = (),
        windows: tuple[SwayWindow, ...] = (),
    ) -> None:
        self.workspaces = workspaces
        self.windows = windows
        self.switched_workspaces: list[str] = []
        self.layout_calls: list[tuple[str, str]] = []
        self.focused_window_ids: list[int] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)

    def set_workspace_layout(self, workspace_name: str, layout: str) -> None:
        self.layout_calls.append((workspace_name, layout))

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return tuple(workspace for workspace in self.workspaces if workspace.startswith(prefix))

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return self.windows

    def focus_window(self, window_id: int) -> None:
        self.focused_window_ids.append(window_id)


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


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        self.calls.append((session.session_name, url))


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


def test_enter_project_session_autostarts_active_windows_in_declaration_order(tmp_path: Path) -> None:
    """First entry with a resolved windows tuple: shell + editor + server (active)
    + console (inactive) ensures shell, editor, and server but not console."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()
    browser = StubBrowserAdapter()

    enter_project_session(
        project_root,
        sway=sway,
        terminals=terminals,
        editor=editor,
        browser=browser,
        windows=(
            WindowSpec(role="shell", command="zsh", autostart_active=True),
            WindowSpec(role="editor", command="nvim", autostart_active=True),
            WindowSpec(role="server", command="bin/dev", autostart_active=True),
            WindowSpec(role="console", command="bin/rails console", autostart_active=False),
        ),
    )

    assert editor.ensured == ["demo"]
    assert terminals.ensured_terminals == [
        ("demo", "shell", project_root),
        ("demo", "server", project_root),
    ]
    assert browser.calls == []


def test_enter_project_session_skips_autostart_sweep_on_re_entry(tmp_path: Path) -> None:
    """Re-entry (editor=None) ensures only the shell window — autostart-active
    server / browser entries are NOT launched."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    browser = StubBrowserAdapter()

    enter_project_session(
        project_root,
        sway=sway,
        terminals=terminals,
        editor=None,
        browser=browser,
        windows=(
            WindowSpec(role="shell", command="zsh", autostart_active=True),
            WindowSpec(role="server", command="bin/dev", autostart_active=True),
            WindowSpec(role="browser", command="firefox", autostart_active=True),
        ),
    )

    assert terminals.ensured_terminals == [("demo", "shell", project_root)]
    assert browser.calls == []


def test_enter_project_session_dispatches_browser_role_to_browser_adapter(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()
    browser = StubBrowserAdapter()

    enter_project_session(
        project_root,
        sway=sway,
        terminals=terminals,
        editor=editor,
        browser=browser,
        windows=(
            WindowSpec(role="shell", command="zsh", autostart_active=True),
            WindowSpec(role="browser", command="firefox", autostart_active=True),
        ),
    )

    assert browser.calls == [("demo", None)]
    # Browser doesn't go through the kitty terminal adapter — only the shell
    # was ensured there.
    assert terminals.ensured_terminals == [("demo", "shell", project_root)]


def test_enter_project_session_skips_browser_role_when_no_browser_adapter(tmp_path: Path) -> None:
    """If the resolver yields a browser window but the caller didn't pass a
    browser adapter, skip the browser role instead of crashing."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    enter_project_session(
        project_root,
        sway=sway,
        terminals=terminals,
        editor=editor,
        browser=None,
        windows=(
            WindowSpec(role="shell", command="zsh", autostart_active=True),
            WindowSpec(role="browser", command="firefox", autostart_active=True),
        ),
    )

    # Shell ensured by the unconditional bootstrap step; browser is skipped
    # because no adapter was provided.
    assert terminals.ensured_terminals == [("demo", "shell", project_root)]


def test_enter_project_session_skips_inactive_window(tmp_path: Path) -> None:
    """A window with autostart_active=False (resolver output for a layout
    whose probe failed, or an explicit autostart="false") is skipped from
    the autostart sweep — declared but not auto-launched."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()
    browser = StubBrowserAdapter()

    enter_project_session(
        project_root,
        sway=sway,
        terminals=terminals,
        editor=editor,
        browser=browser,
        windows=(
            WindowSpec(role="shell", command="zsh", autostart_active=True),
            WindowSpec(role="server", command="bin/dev", autostart_active=False),
        ),
    )

    # Shell was ensured; server window was skipped because autostart_active is False.
    assert terminals.ensured_terminals == [("demo", "shell", project_root)]


def test_enter_project_session_sets_workspace_layout_before_launching_windows(tmp_path: Path) -> None:
    """When `workspace_layout` is configured, hop sends it to sway *before*
    ensuring any windows so the first window lands in the configured
    arrangement instead of getting reflowed afterwards."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    enter_project_session(
        project_root,
        sway=sway,
        terminals=terminals,
        editor=editor,
        workspace_layout="tabbed",
    )

    assert sway.layout_calls == [("p:demo", "tabbed")]
    assert sway.switched_workspaces == ["p:demo"]


def test_enter_project_session_focuses_shell_window_after_sweep(tmp_path: Path) -> None:
    """Each kitty launch steals focus, so after the autostart sweep the
    last-launched window is focused. enter_project_session refocuses the
    shell so the session lands on a sensible starting point — and in a
    tabbed workspace, makes the shell the visible tab."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    shell_window = SwayWindow(id=42, workspace_name="p:demo", app_id="hop:shell", window_class=None)
    editor_window = SwayWindow(id=43, workspace_name="p:demo", app_id="hop:editor", window_class=None)
    sway = StubSwayAdapter(windows=(shell_window, editor_window))
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    enter_project_session(project_root, sway=sway, terminals=terminals, editor=editor)

    assert sway.focused_window_ids == [42]


def test_enter_project_session_skips_focus_when_no_shell_window_in_sway(tmp_path: Path) -> None:
    """If sway hasn't registered the shell window yet (or it ended up on
    another workspace somehow), skip the refocus instead of erroring."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()  # no windows
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    enter_project_session(project_root, sway=sway, terminals=terminals, editor=editor)

    assert sway.focused_window_ids == []


def test_enter_project_session_focuses_lowest_id_shell_on_session_workspace(tmp_path: Path) -> None:
    """A stale shell window from another workspace must not be picked.
    Match by app_id AND workspace name, then take the lowest sway id."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    foreign_shell = SwayWindow(id=10, workspace_name="p:other", app_id="hop:shell", window_class=None)
    session_shell = SwayWindow(id=20, workspace_name="p:demo", app_id="hop:shell", window_class=None)
    sway = StubSwayAdapter(windows=(foreign_shell, session_shell))
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    enter_project_session(project_root, sway=sway, terminals=terminals, editor=editor)

    assert sway.focused_window_ids == [20]


def test_enter_project_session_skips_layout_call_when_unset(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    enter_project_session(project_root, sway=sway, terminals=terminals, editor=editor)

    assert sway.layout_calls == []


def test_enter_project_session_falls_back_to_legacy_behavior_without_windows(tmp_path: Path) -> None:
    """Callers that don't pass a resolved windows tuple (legacy tests, etc.)
    still get the pre-resolver behavior: shell + editor."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    enter_project_session(project_root, sway=sway, terminals=terminals, editor=editor)

    assert editor.ensured == ["demo"]
    assert terminals.ensured_terminals == [("demo", "shell", project_root)]


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
