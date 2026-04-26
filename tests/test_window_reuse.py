"""
End-to-end tests verifying window reuse guarantees across the hop command surface.

These tests use stateful stub adapters to assert that:
- Repeated commands reuse existing windows instead of creating duplicates
- Missing components (editor, terminals, browser) are recreated automatically after teardown
- Session and role routing boundaries prevent cross-session window sharing
"""

from pathlib import Path

from hop.commands.browser import focus_browser
from hop.commands.edit import edit_in_session
from hop.commands.run import run_command
from hop.commands.term import focus_terminal
from hop.session import ProjectSession


class IdempotentKittyAdapter:
    """
    Kitty stub that simulates the reuse contract: both ensure_terminal and
    run_in_terminal create a new OS window only when the (session, role) pair
    does not already exist.
    """

    def __init__(self) -> None:
        self._windows: set[tuple[str, str]] = set()
        self.created_windows: list[tuple[str, str]] = []
        self.focused_windows: list[tuple[str, str]] = []
        self.runs: list[tuple[str, str, str]] = []

    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
        key = (session.session_name, role)
        if key not in self._windows:
            self._windows.add(key)
            self.created_windows.append(key)
        else:
            self.focused_windows.append(key)

    def run_in_terminal(self, session: ProjectSession, *, role: str, command: str) -> int:
        key = (session.session_name, role)
        if key not in self._windows:
            self._windows.add(key)
            self.created_windows.append(key)
        self.runs.append((session.session_name, role, command))
        return abs(hash(key)) % 10_000

    def close_window(self, session_name: str, role: str) -> None:
        """Simulate the user closing a terminal OS window."""
        self._windows.discard((session_name, role))


class IdempotentNeovimAdapter:
    """
    Neovim stub that simulates the editor lifecycle: focus and open_target launch
    a new server only when the session has no live server; quit_editor tears it down.
    """

    def __init__(self) -> None:
        self._servers: set[str] = set()
        self.launched: list[str] = []
        self.focused: list[str] = []
        self.opened_targets: list[tuple[str, str]] = []

    def focus(self, session: ProjectSession) -> None:
        if session.session_name not in self._servers:
            self._servers.add(session.session_name)
            self.launched.append(session.session_name)
        else:
            self.focused.append(session.session_name)

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        if session.session_name not in self._servers:
            self._servers.add(session.session_name)
            self.launched.append(session.session_name)
        self.opened_targets.append((session.session_name, target))

    def quit_editor(self, session_name: str) -> None:
        """Simulate the user exiting Neovim with :qa."""
        self._servers.discard(session_name)


class IdempotentBrowserAdapter:
    """
    Browser stub that simulates the session browser lifecycle: ensure_browser
    launches a new window only when the session has no marked browser yet.
    """

    def __init__(self) -> None:
        self._session_browsers: set[str] = set()
        self.launched: list[str] = []
        self.reused: list[str] = []
        self.urls_navigated: list[tuple[str, str | None]] = []

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        if session.session_name not in self._session_browsers:
            self._session_browsers.add(session.session_name)
            self.launched.append(session.session_name)
        else:
            self.reused.append(session.session_name)
        if url is not None:
            self.urls_navigated.append((session.session_name, url))

    def close_browser(self, session_name: str) -> None:
        """Simulate the user closing the session browser window."""
        self._session_browsers.discard(session_name)


# ─── Terminal reuse: hop term ──────────────────────────────────────────────────


def test_repeated_hop_term_reuses_existing_window(tmp_path: Path) -> None:
    """Calling hop term twice for the same role focuses the existing window, not a new one."""
    session_root = tmp_path / "myproject"
    session_root.mkdir()

    kitty = IdempotentKittyAdapter()

    focus_terminal(session_root, terminals=kitty, role="shell")
    focus_terminal(session_root, terminals=kitty, role="shell")

    assert kitty.created_windows == [("myproject", "shell")]
    assert kitty.focused_windows == [("myproject", "shell")]


def test_hop_term_different_roles_create_distinct_windows(tmp_path: Path) -> None:
    """Each distinct role gets its own terminal window within the same session."""
    session_root = tmp_path / "myproject"
    session_root.mkdir()

    kitty = IdempotentKittyAdapter()

    focus_terminal(session_root, terminals=kitty, role="shell")
    focus_terminal(session_root, terminals=kitty, role="test")
    focus_terminal(session_root, terminals=kitty, role="server")

    assert set(kitty.created_windows) == {
        ("myproject", "shell"),
        ("myproject", "test"),
        ("myproject", "server"),
    }
    assert kitty.focused_windows == []


def test_hop_term_recreates_window_after_manual_close(tmp_path: Path) -> None:
    """After a terminal window is closed, the next hop term creates a fresh one."""
    session_root = tmp_path / "myproject"
    session_root.mkdir()

    kitty = IdempotentKittyAdapter()

    focus_terminal(session_root, terminals=kitty, role="test")
    kitty.close_window("myproject", "test")
    focus_terminal(session_root, terminals=kitty, role="test")

    assert kitty.created_windows == [("myproject", "test"), ("myproject", "test")]
    assert kitty.focused_windows == []


# ─── Run command provisions missing role window: hop run ──────────────────────


def test_hop_run_provisions_missing_role_window(tmp_path: Path) -> None:
    """hop run creates the role terminal when it does not exist, then injects the command."""
    session_root = tmp_path / "myproject"
    session_root.mkdir()

    kitty = IdempotentKittyAdapter()

    run_command(
        session_root,
        terminals=kitty,
        role="server",
        command="bin/dev",
        runs_dir=tmp_path / "runs",
    )

    assert ("myproject", "server") in kitty.created_windows
    assert kitty.runs == [("myproject", "server", "bin/dev")]


def test_hop_run_reuses_existing_role_window(tmp_path: Path) -> None:
    """Sending a second command to the same role does not create a duplicate window."""
    session_root = tmp_path / "myproject"
    session_root.mkdir()

    kitty = IdempotentKittyAdapter()
    runs_dir = tmp_path / "runs"

    run_command(session_root, terminals=kitty, role="shell", command="ls", runs_dir=runs_dir)
    run_command(session_root, terminals=kitty, role="shell", command="echo hello", runs_dir=runs_dir)

    assert kitty.created_windows == [("myproject", "shell")]
    assert kitty.runs == [
        ("myproject", "shell", "ls"),
        ("myproject", "shell", "echo hello"),
    ]


# ─── Editor reuse and recreation: hop edit ────────────────────────────────────


def test_repeated_hop_edit_reuses_existing_editor(tmp_path: Path) -> None:
    """Calling hop edit twice focuses the existing Neovim instance without launching a second."""
    session_root = tmp_path / "myproject"
    session_root.mkdir()

    neovim = IdempotentNeovimAdapter()

    edit_in_session(session_root, neovim=neovim)
    edit_in_session(session_root, neovim=neovim)

    assert neovim.launched == ["myproject"]
    assert neovim.focused == ["myproject"]


def test_hop_edit_recreates_editor_after_quit(tmp_path: Path) -> None:
    """After :qa, the next hop edit launches a fresh editor instead of accumulating windows."""
    session_root = tmp_path / "myproject"
    session_root.mkdir()

    neovim = IdempotentNeovimAdapter()

    edit_in_session(session_root, neovim=neovim)
    neovim.quit_editor("myproject")
    edit_in_session(session_root, neovim=neovim)

    assert neovim.launched == ["myproject", "myproject"]
    assert neovim.focused == []


def test_hop_edit_routes_target_to_existing_editor_without_relaunch(tmp_path: Path) -> None:
    """Opening a file target into a live editor does not relaunch the editor."""
    session_root = tmp_path / "myproject"
    session_root.mkdir()

    neovim = IdempotentNeovimAdapter()

    edit_in_session(session_root, neovim=neovim)
    edit_in_session(session_root, neovim=neovim, target="app/models/user.rb:42")

    assert neovim.launched == ["myproject"]
    assert neovim.opened_targets == [("myproject", "app/models/user.rb:42")]


# ─── Browser session scope: hop browser ───────────────────────────────────────


def test_hop_browser_reuses_session_browser_across_repeated_calls(tmp_path: Path) -> None:
    """Repeated hop browser calls focus the existing session browser window."""
    session_root = tmp_path / "myproject"
    session_root.mkdir()

    browser = IdempotentBrowserAdapter()

    focus_browser(session_root, browser=browser, url="https://example.com")
    focus_browser(session_root, browser=browser, url="https://other.example.com")

    assert browser.launched == ["myproject"]
    assert browser.reused == ["myproject"]


def test_hop_browser_is_isolated_between_sessions(tmp_path: Path) -> None:
    """Each session gets its own browser window; they do not share one."""
    session_a_root = tmp_path / "project-a"
    session_b_root = tmp_path / "project-b"
    session_a_root.mkdir()
    session_b_root.mkdir()

    browser = IdempotentBrowserAdapter()

    focus_browser(session_a_root, browser=browser, url="https://docs.a.com")
    focus_browser(session_b_root, browser=browser, url="https://docs.b.com")

    assert set(browser.launched) == {"project-a", "project-b"}
    assert browser.reused == []


def test_hop_browser_recreates_window_after_close(tmp_path: Path) -> None:
    """After the session browser is closed, the next hop browser launches a fresh one."""
    session_root = tmp_path / "myproject"
    session_root.mkdir()

    browser = IdempotentBrowserAdapter()

    focus_browser(session_root, browser=browser, url="https://example.com")
    browser.close_browser("myproject")
    focus_browser(session_root, browser=browser, url="https://example.com")

    assert browser.launched == ["myproject", "myproject"]
    assert browser.reused == []


# ─── Session switching: no cross-session window duplication ───────────────────


def test_same_role_in_different_sessions_creates_separate_windows(tmp_path: Path) -> None:
    """The shell role in project-a and project-b are distinct OS windows, not the same one."""
    session_a_root = tmp_path / "project-a"
    session_b_root = tmp_path / "project-b"
    session_a_root.mkdir()
    session_b_root.mkdir()

    kitty = IdempotentKittyAdapter()

    focus_terminal(session_a_root, terminals=kitty, role="shell")
    focus_terminal(session_b_root, terminals=kitty, role="shell")

    assert set(kitty.created_windows) == {("project-a", "shell"), ("project-b", "shell")}
    assert kitty.focused_windows == []


def test_switching_back_to_session_reuses_its_existing_windows(tmp_path: Path) -> None:
    """
    Switching to project-a, then project-b, then back to project-a focuses the
    original project-a windows rather than creating duplicates.
    """
    session_a_root = tmp_path / "project-a"
    session_b_root = tmp_path / "project-b"
    session_a_root.mkdir()
    session_b_root.mkdir()

    kitty = IdempotentKittyAdapter()

    focus_terminal(session_a_root, terminals=kitty, role="shell")
    focus_terminal(session_b_root, terminals=kitty, role="shell")
    focus_terminal(session_a_root, terminals=kitty, role="shell")

    assert kitty.created_windows == [("project-a", "shell"), ("project-b", "shell")]
    assert kitty.focused_windows == [("project-a", "shell")]


def test_session_switch_does_not_mix_editor_instances(tmp_path: Path) -> None:
    """Each session's Neovim instance is independent; switching sessions never merges them."""
    session_a_root = tmp_path / "project-a"
    session_b_root = tmp_path / "project-b"
    session_a_root.mkdir()
    session_b_root.mkdir()

    neovim = IdempotentNeovimAdapter()

    edit_in_session(session_a_root, neovim=neovim)
    edit_in_session(session_b_root, neovim=neovim)
    edit_in_session(session_a_root, neovim=neovim)

    assert neovim.launched == ["project-a", "project-b"]
    assert neovim.focused == ["project-a"]
