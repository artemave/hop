from pathlib import Path

from hop.backends import HostBackend
from hop.commands.kill import kill_session
from hop.kitty import KittyWindow
from hop.session import ProjectSession
from hop.sway import SwayWindow


class StubSwayAdapter:
    def __init__(
        self,
        workspaces: tuple[str, ...] = (),
        windows: tuple[SwayWindow, ...] = (),
    ) -> None:
        self.workspaces = workspaces
        self.windows = windows
        self.closed_windows: list[int] = []
        self.removed_workspaces: list[str] = []

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return self.windows

    def close_window(self, window_id: int) -> None:
        self.closed_windows.append(window_id)

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return tuple(w for w in self.workspaces if w.startswith(prefix))

    def remove_workspace(self, workspace_name: str) -> None:
        self.removed_workspaces.append(workspace_name)


class StubKittyAdapter:
    def __init__(self, window_ids: tuple[int, ...] = ()) -> None:
        self._window_ids = window_ids
        self.closed_windows: list[int] = []

    def list_session_windows(self, session: ProjectSession) -> list[KittyWindow]:
        return [KittyWindow(id=wid, role=None) for wid in self._window_ids]

    def close_window(self, session_name: str, window_id: int) -> None:
        self.closed_windows.append(window_id)


def test_kill_session_closes_all_kitty_managed_windows(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    kitty = StubKittyAdapter(window_ids=(1, 2, 3))

    kill_session(project_root, sway=sway, kitty=kitty)

    assert kitty.closed_windows == [1, 2, 3]


def test_kill_session_closes_browser_window_by_sway_mark(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    browser_window = SwayWindow(
        id=42,
        workspace_name="p:/other/workspace",
        app_id="brave-browser",
        window_class=None,
        marks=("_hop_browser:demo",),
    )
    sway = StubSwayAdapter(windows=(browser_window,))
    kitty = StubKittyAdapter()

    kill_session(project_root, sway=sway, kitty=kitty)

    assert sway.closed_windows == [42]


def test_kill_session_closes_browser_that_drifted_to_another_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    workspace_name = f"p:{project_root.name}"

    browser_window = SwayWindow(
        id=99,
        workspace_name="p:/some/other/project",
        app_id="firefox",
        window_class=None,
        marks=("_hop_browser:demo",),
    )
    sway = StubSwayAdapter(workspaces=(workspace_name,), windows=(browser_window,))
    kitty = StubKittyAdapter()

    kill_session(project_root, sway=sway, kitty=kitty)

    assert 99 in sway.closed_windows


def test_kill_session_does_not_close_unrelated_sway_windows(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    unrelated_window = SwayWindow(
        id=55,
        workspace_name=f"p:{project_root.name}",
        app_id="kitty",
        window_class=None,
        marks=(),
    )
    sway = StubSwayAdapter(windows=(unrelated_window,))
    kitty = StubKittyAdapter()

    kill_session(project_root, sway=sway, kitty=kitty)

    assert sway.closed_windows == []


def test_kill_session_removes_workspace_if_still_exists(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    workspace_name = f"p:{project_root.name}"

    sway = StubSwayAdapter(workspaces=(workspace_name,))
    kitty = StubKittyAdapter()

    kill_session(project_root, sway=sway, kitty=kitty)

    assert sway.removed_workspaces == [workspace_name]


def test_kill_session_skips_workspace_removal_when_already_gone(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter(workspaces=())
    kitty = StubKittyAdapter()

    kill_session(project_root, sway=sway, kitty=kitty)

    assert sway.removed_workspaces == []


def test_kill_session_returns_resolved_session(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested = project_root / "src"
    nested.mkdir(parents=True)

    sway = StubSwayAdapter()
    kitty = StubKittyAdapter()

    session = kill_session(nested, sway=sway, kitty=kitty)

    assert session.session_name == "src"
    assert session.workspace_name == f"p:{nested.name}"


def test_kill_session_forgets_persisted_session_state(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    sway = StubSwayAdapter()
    kitty = StubKittyAdapter()
    forgotten: list[str] = []

    kill_session(project_root, sway=sway, kitty=kitty, forget=forgotten.append)

    assert forgotten == ["demo"]


def test_kill_session_calls_base_teardown_after_window_cleanup(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    events: list[str] = []

    class TrackingKitty(StubKittyAdapter):
        def close_window(self, session_name: str, window_id: int) -> None:
            events.append(f"close-{window_id}")
            super().close_window(session_name, window_id)

    class TrackingBackend:
        def teardown(self, _session: ProjectSession) -> None:
            events.append("teardown")

    forgotten: list[str] = []

    def track_forget(name: str) -> None:
        events.append("forget")
        forgotten.append(name)

    sway = StubSwayAdapter()
    kitty = TrackingKitty(window_ids=(7, 8))

    kill_session(
        project_root,
        sway=sway,
        kitty=kitty,
        session_backend_for=lambda _session: TrackingBackend(),  # type: ignore[arg-type]
        forget=track_forget,
    )

    assert events == ["close-7", "close-8", "teardown", "forget"]


def test_kill_session_uses_host_base_by_default(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    # Default backend must be a no-op; this just confirms kill_session doesn't blow up.
    kill_session(
        project_root,
        sway=StubSwayAdapter(),
        kitty=StubKittyAdapter(),
        session_backend_for=lambda _session: HostBackend(),
    )
