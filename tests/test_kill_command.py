from pathlib import Path

from hop.backends import HostBackend
from hop.commands.kill import kill_session
from hop.session import ProjectSession
from hop.sway import SwayWindow


class StubSwayAdapter:
    def __init__(self, windows: tuple[SwayWindow, ...] = ()) -> None:
        self.windows = windows
        self.closed_windows: list[int] = []

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return self.windows

    def close_window(self, window_id: int) -> None:
        self.closed_windows.append(window_id)


def _session_window(*, id: int, workspace: str, marks: tuple[str, ...] = ()) -> SwayWindow:
    return SwayWindow(
        id=id,
        workspace_name=workspace,
        app_id="kitty",
        window_class=None,
        marks=marks,
    )


def test_kill_session_closes_every_window_on_session_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    workspace_name = f"p:{project_root.name}"

    sway = StubSwayAdapter(windows=(
        _session_window(id=1, workspace=workspace_name),
        _session_window(id=2, workspace=workspace_name),
        _session_window(id=3, workspace=workspace_name),
    ))

    kill_session(project_root, sway=sway)

    assert sway.closed_windows == [1, 2, 3]


def test_kill_session_closes_browser_that_drifted_to_another_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    drifted_browser = SwayWindow(
        id=99,
        workspace_name="p:other",
        app_id="firefox",
        window_class=None,
        marks=("_hop_browser:demo",),
    )
    sway = StubSwayAdapter(windows=(drifted_browser,))

    kill_session(project_root, sway=sway)

    assert sway.closed_windows == [99]


def test_kill_session_closes_editor_marked_window_outside_session_workspace(tmp_path: Path) -> None:
    # Editor's kitty is sometimes the parent kitty (different workspace) when
    # the editor was launched from the kitty boss with a stale KITTY_LISTEN_ON.
    # The session's editor mark catches it regardless of which workspace it
    # ended up on.
    project_root = tmp_path / "demo"
    project_root.mkdir()

    drifted_editor = SwayWindow(
        id=42,
        workspace_name="p:other",
        app_id="hop:editor",
        window_class=None,
        marks=("_hop_editor:demo",),
    )
    sway = StubSwayAdapter(windows=(drifted_editor,))

    kill_session(project_root, sway=sway)

    assert sway.closed_windows == [42]


def test_kill_session_does_not_close_windows_on_other_workspaces(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    other_window = SwayWindow(
        id=77,
        workspace_name="p:other",
        app_id="kitty",
        window_class=None,
    )
    sway = StubSwayAdapter(windows=(other_window,))

    kill_session(project_root, sway=sway)

    assert sway.closed_windows == []


def test_kill_session_returns_resolved_session(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested = project_root / "src"
    nested.mkdir(parents=True)

    sway = StubSwayAdapter()

    session = kill_session(nested, sway=sway)

    assert session.session_name == "src"
    assert session.workspace_name == f"p:{nested.name}"


def test_kill_session_forgets_persisted_session_state(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    forgotten: list[str] = []

    kill_session(project_root, sway=sway, forget=forgotten.append)

    assert forgotten == ["demo"]


def test_kill_session_runs_teardown_after_window_close(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    workspace_name = f"p:{project_root.name}"

    events: list[str] = []

    class TrackingSway(StubSwayAdapter):
        def close_window(self, window_id: int) -> None:
            events.append(f"close-{window_id}")
            super().close_window(window_id)

    class TrackingBackend:
        def teardown(self, _session: ProjectSession) -> None:
            events.append("teardown")

    def track_forget(name: str) -> None:
        events.append(f"forget-{name}")

    sway = TrackingSway(windows=(
        _session_window(id=7, workspace=workspace_name),
        _session_window(id=8, workspace=workspace_name),
    ))

    kill_session(
        project_root,
        sway=sway,
        session_backend_for=lambda _session: TrackingBackend(),  # type: ignore[arg-type]
        forget=track_forget,
    )

    assert events == ["close-7", "close-8", "teardown", "forget-demo"]


def test_kill_session_uses_host_backend_by_default(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    kill_session(
        project_root,
        sway=StubSwayAdapter(),
        session_backend_for=lambda _session: HostBackend(),
    )
