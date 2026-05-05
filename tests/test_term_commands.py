from pathlib import Path

from hop.commands.term import focus_terminal
from hop.session import ProjectSession
from hop.sway import SwayWindow


class StubKittyAdapter:
    def __init__(self) -> None:
        self.ensured: list[tuple[str, str, Path]] = []

    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
        self.ensured.append((session.session_name, role, session.project_root))


class StubSwayAdapter:
    def __init__(self, windows: tuple[SwayWindow, ...] = ()) -> None:
        self.windows = windows
        self.focused_window_ids: list[int] = []

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return self.windows

    def focus_window(self, window_id: int) -> None:
        self.focused_window_ids.append(window_id)


def test_focus_terminal_routes_by_role(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    kitty = StubKittyAdapter()
    sway = StubSwayAdapter()

    session = focus_terminal(
        nested_directory,
        terminals=kitty,
        sway=sway,
        role="test",
    )

    assert session.session_name == "src"
    assert kitty.ensured == [("src", "test", nested_directory)]


def test_focus_terminal_escalates_via_sway_ipc_when_role_window_exists(tmp_path: Path) -> None:
    """Why this matters: kitty's `focus-window` relies on xdg-activation,
    which sway can refuse when vicinae's UI close steals the token. The
    sway IPC focus call is unconditional and survives that race."""

    project_root = tmp_path / "rails"
    project_root.mkdir(parents=True)

    role_window = SwayWindow(
        id=42,
        workspace_name="p:rails",
        app_id="hop:console",
        window_class=None,
    )
    other_window = SwayWindow(
        id=99,
        workspace_name="p:rails",
        app_id="hop:server",
        window_class=None,
    )

    kitty = StubKittyAdapter()
    sway = StubSwayAdapter(windows=(other_window, role_window))

    focus_terminal(
        project_root,
        terminals=kitty,
        sway=sway,
        role="console",
    )

    assert sway.focused_window_ids == [42]


def test_focus_terminal_matches_role_via_x11_window_class_fallback(tmp_path: Path) -> None:
    project_root = tmp_path / "rails"
    project_root.mkdir(parents=True)

    # On X11, kitty sets WM_CLASS rather than the Wayland app_id; sway
    # surfaces it as `window_class`. Both should match.
    role_window = SwayWindow(
        id=7,
        workspace_name="p:rails",
        app_id=None,
        window_class="hop:console",
    )
    sway = StubSwayAdapter(windows=(role_window,))

    focus_terminal(
        project_root,
        terminals=StubKittyAdapter(),
        sway=sway,
        role="console",
    )

    assert sway.focused_window_ids == [7]


def test_focus_terminal_skips_sway_focus_when_no_role_window_visible_yet(tmp_path: Path) -> None:
    """Right after a fresh launch the kitty window may not have surfaced
    in sway's tree yet. Kitty's launch already focused the new window,
    so dropping the sway escalation is harmless — the focus is correct
    regardless."""

    project_root = tmp_path / "rails"
    project_root.mkdir(parents=True)

    kitty = StubKittyAdapter()
    sway = StubSwayAdapter(windows=())

    focus_terminal(
        project_root,
        terminals=kitty,
        sway=sway,
        role="console",
    )

    assert kitty.ensured == [("rails", "console", project_root)]
    assert sway.focused_window_ids == []


def test_focus_terminal_ignores_role_windows_on_other_workspaces(tmp_path: Path) -> None:
    """A drifted-or-stale window on a different workspace should not
    pull focus into the wrong project."""

    project_root = tmp_path / "rails"
    project_root.mkdir(parents=True)

    drifted = SwayWindow(
        id=1,
        workspace_name="p:other",
        app_id="hop:console",
        window_class=None,
    )
    sway = StubSwayAdapter(windows=(drifted,))

    focus_terminal(
        project_root,
        terminals=StubKittyAdapter(),
        sway=sway,
        role="console",
    )

    assert sway.focused_window_ids == []
