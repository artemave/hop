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
        focused_workspace: str = "",
    ) -> None:
        self.workspaces = workspaces
        self.windows = windows
        self.focused_workspace = focused_workspace
        self.switched_workspaces: list[str] = []
        self.layout_calls: list[tuple[str, str]] = []
        self.focused_window_ids: list[int] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)
        # Reflect the switch so subsequent `get_focused_workspace` checks see
        # the new workspace — `enter_project_session` reads it back to decide
        # whether to re-issue the switch.
        self.focused_workspace = workspace_name

    def set_workspace_layout(self, workspace_name: str, layout: str) -> None:
        self.layout_calls.append((workspace_name, layout))

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return tuple(workspace for workspace in self.workspaces if workspace.startswith(prefix))

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return self.windows

    def focus_window(self, window_id: int) -> None:
        self.focused_window_ids.append(window_id)

    def get_focused_workspace(self) -> str:
        return self.focused_workspace


class StubTerminalAdapter:
    def __init__(
        self,
        *,
        existing_windows: tuple[KittyWindow, ...] = (),
    ) -> None:
        self.ensured_terminals: list[tuple[str, str, Path]] = []
        self.already_prepared_flags: list[bool] = []
        self._existing_windows = existing_windows

    def ensure_terminal(self, session: ProjectSession, *, role: str, already_prepared: bool = False) -> None:
        self.ensured_terminals.append((session.session_name, role, session.project_root))
        self.already_prepared_flags.append(already_prepared)

    def list_session_windows(self, session: ProjectSession) -> tuple[KittyWindow, ...]:
        return self._existing_windows


class StubEditorAdapter:
    def __init__(self) -> None:
        self.ensured: list[str] = []
        self.keep_focus_calls: list[bool] = []

    def ensure(self, session: ProjectSession, *, keep_focus: bool = True) -> None:
        self.ensured.append(session.session_name)
        self.keep_focus_calls.append(keep_focus)


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
    # Caller has already run prepare (resolve_for_entry inline or the headless
    # popup) — the bootstrap path must not re-run it. Re-running ``compose up
    # -d`` on an up container can stall 20+ seconds.
    assert terminals.already_prepared_flags == [True]


def test_enter_project_session_uses_supplied_session_over_cwd(tmp_path: Path) -> None:
    """A remote session's identity comes from the shim's (host, cwd), not the
    local cwd of the dispatching subprocess. A supplied session must override
    what ``cwd`` would resolve to — the bug where windows came up for the local
    home session ("artem") instead of the remote one."""
    sway = StubSwayAdapter()
    terminals = StubTerminalAdapter()
    remote = ProjectSession(
        project_root=Path("/home/admin/projects/thonon-les-pains"),
        session_name="thonon-les-pains",
        workspace_name="p:thonon-les-pains",
        host="devbox",
    )

    # ``tmp_path`` stands in for the local home a remote-enter subprocess passes;
    # it must be ignored in favour of the supplied remote session.
    returned = enter_project_session(tmp_path, sway=sway, terminals=terminals, session=remote)

    assert returned is remote
    assert sway.switched_workspaces == ["p:thonon-les-pains"]
    assert terminals.ensured_terminals == [
        ("thonon-les-pains", "shell", Path("/home/admin/projects/thonon-les-pains")),
    ]


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


def test_enter_project_session_passes_keep_focus_false_to_editor_during_bootstrap(tmp_path: Path) -> None:
    """In sway tabbed mode, new windows are inserted right after the
    focused tab. The bootstrap activation sweep launches editor first,
    then layout terminals. If the editor doesn't take focus, every
    subsequent terminal slots in between shell and editor and the editor
    walks to the end of the tab strip. Passing ``keep_focus=False``
    makes the editor the focused tab, so terminals tab in *after* it,
    yielding the desired shell → editor → terminals order."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    editor = StubEditorAdapter()

    enter_project_session(
        project_root,
        sway=StubSwayAdapter(),
        terminals=StubTerminalAdapter(),
        editor=editor,
        windows=(
            WindowSpec(role="shell", command="", active=True),
            WindowSpec(role="editor", command="nvim", active=True),
            WindowSpec(role="server", command="bin/dev", active=True),
        ),
    )

    assert editor.keep_focus_calls == [False]


def test_enter_project_session_passes_keep_focus_false_to_editor_in_legacy_no_windows_path(tmp_path: Path) -> None:
    """Same keep_focus contract for the legacy path (callers that pass no
    ``windows`` tuple) — the editor still owns slot 2 in the tab strip."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    editor = StubEditorAdapter()

    enter_project_session(
        project_root,
        sway=StubSwayAdapter(),
        terminals=StubTerminalAdapter(),
        editor=editor,
    )

    assert editor.keep_focus_calls == [False]


def test_enter_project_session_launches_terminal_before_editor(tmp_path: Path) -> None:
    """Order matters: the per-session kitty isn't running on first entry,
    and only ensure_terminal knows how to bootstrap it. The editor adapter
    talks to the kitty socket directly with no fallback — if it runs first,
    the call fails with `Could not talk to Kitty over unix:.../...sock`."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    call_log: list[str] = []

    class OrderedTerminalAdapter(StubTerminalAdapter):
        def ensure_terminal(self, session: ProjectSession, *, role: str, already_prepared: bool = False) -> None:
            call_log.append("terminal")
            super().ensure_terminal(session, role=role, already_prepared=already_prepared)

    class OrderedEditorAdapter(StubEditorAdapter):
        def ensure(self, session: ProjectSession, *, keep_focus: bool = True) -> None:
            call_log.append("editor")
            super().ensure(session, keep_focus=keep_focus)

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


def test_enter_project_session_activates_windows_in_declaration_order(tmp_path: Path) -> None:
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
            WindowSpec(role="shell", command="zsh", active=True),
            WindowSpec(role="editor", command="nvim", active=True),
            WindowSpec(role="server", command="bin/dev", active=True),
            WindowSpec(role="console", command="bin/rails console", active=False),
        ),
    )

    assert editor.ensured == ["demo"]
    assert terminals.ensured_terminals == [
        ("demo", "shell", project_root),
        ("demo", "server", project_root),
    ]
    assert browser.calls == []


def test_enter_project_session_skips_activation_sweep_on_re_entry(tmp_path: Path) -> None:
    """Re-entry (editor=None) ensures only the shell window — active
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
            WindowSpec(role="shell", command="zsh", active=True),
            WindowSpec(role="server", command="bin/dev", active=True),
            WindowSpec(role="browser", command="firefox", active=True),
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
            WindowSpec(role="shell", command="zsh", active=True),
            WindowSpec(role="browser", command="firefox", active=True),
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
            WindowSpec(role="shell", command="zsh", active=True),
            WindowSpec(role="browser", command="firefox", active=True),
        ),
    )

    # Shell ensured by the unconditional bootstrap step; browser is skipped
    # because no adapter was provided.
    assert terminals.ensured_terminals == [("demo", "shell", project_root)]


def test_enter_project_session_skips_inactive_window(tmp_path: Path) -> None:
    """A window with active=False (resolver output for a layout
    whose probe failed, or an explicit activate="false") is skipped from
    the activation sweep — declared but not auto-launched."""
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
            WindowSpec(role="shell", command="zsh", active=True),
            WindowSpec(role="server", command="bin/dev", active=False),
        ),
    )

    # Shell was ensured; server window was skipped because active is False.
    assert terminals.ensured_terminals == [("demo", "shell", project_root)]


def test_enter_project_session_applies_workspace_layout_after_refocusing_shell(tmp_path: Path) -> None:
    """``workspace_layout`` is applied *after* ``_focus_shell_if_present`` has
    refocused the session workspace, not before window launches. Reason: sway
    reaps empty named workspaces — a slow ``prepare`` can leave ``p:<session>``
    empty long enough to be destroyed, and the recreated workspace loses any
    earlier layout. Refocusing the shell first brings us back to a populated
    ``p:<session>`` so the ``layout <mode>`` command sticks."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    shell_window = SwayWindow(id=42, workspace_name="p:demo", app_id="hop:shell", window_class=None)
    sway = StubSwayAdapter(windows=(shell_window,))
    terminals = StubTerminalAdapter()
    editor = StubEditorAdapter()

    enter_project_session(
        project_root,
        sway=sway,
        terminals=terminals,
        editor=editor,
        workspace_layout="tabbed",
    )

    assert sway.focused_window_ids == [42]
    assert sway.layout_calls == [("p:demo", "tabbed")]
    assert sway.switched_workspaces == ["p:demo"]


def test_enter_project_session_skips_workspace_layout_when_no_shell_on_session_workspace(tmp_path: Path) -> None:
    """If no shell window has registered on ``p:<session>`` by the end of the
    activation sweep, ``_focus_shell_if_present`` can't refocus — and applying
    ``layout`` against whatever workspace the user is currently on would
    silently corrupt that workspace's layout. Skip in that case."""
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

    assert sway.layout_calls == []
    assert sway.focused_window_ids == []


def test_enter_project_session_focuses_shell_window_after_sweep(tmp_path: Path) -> None:
    """Each kitty launch steals focus, so after the activation sweep the
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
    # The first call switches workspace; the second is a no-op for the switch
    # since the user is already on `p:src` (skipping the IPC avoids tripping
    # sway's `workspace_auto_back_and_forth`). Terminal ensure is still
    # idempotent and fires both times — that's the user-visible "give me
    # another shell" behavior.
    assert sway.switched_workspaces == [f"p:{session_root.name}"]
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

    session = spawn_session_terminal(project_root, terminals=terminals)

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
