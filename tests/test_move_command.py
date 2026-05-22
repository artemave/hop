import pytest

from hop.commands.move import move_focused_window
from hop.errors import HopError
from hop.sway import SwayWindow


class StubSwayAdapter:
    def __init__(
        self,
        *,
        workspaces: tuple[str, ...] = (),
        windows: tuple[SwayWindow, ...] = (),
    ) -> None:
        self._workspaces = workspaces
        self._windows = windows
        self.moved_windows: list[tuple[int, str]] = []
        self.switched_workspaces: list[str] = []

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return tuple(w for w in self._workspaces if w.startswith(prefix))

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return self._windows

    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None:
        self.moved_windows.append((window_id, workspace_name))

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)


def _focused(window_id: int, workspace_name: str) -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name=workspace_name,
        app_id="bitwarden",
        window_class=None,
        focused=True,
    )


def _unfocused(window_id: int, workspace_name: str) -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name=workspace_name,
        app_id="kitty",
        window_class=None,
        focused=False,
    )


def test_move_relocates_focused_window_and_follows_to_destination() -> None:
    sway = StubSwayAdapter(
        workspaces=("p:/home/user/projects/demo", "p:/home/user/projects/other"),
        windows=(_focused(42, "2"), _unfocused(7, "p:/home/user/projects/demo")),
    )

    move_focused_window("demo", sway=sway)

    assert sway.moved_windows == [(42, "p:/home/user/projects/demo")]
    assert sway.switched_workspaces == ["p:/home/user/projects/demo"]


def test_move_raises_when_session_does_not_exist() -> None:
    sway = StubSwayAdapter(
        workspaces=("p:/home/user/projects/demo",),
        windows=(_focused(42, "2"),),
    )

    with pytest.raises(HopError, match="no session named 'ghost'"):
        move_focused_window("ghost", sway=sway)

    assert sway.moved_windows == []
    assert sway.switched_workspaces == []


def test_move_raises_when_no_window_is_focused() -> None:
    sway = StubSwayAdapter(
        workspaces=("p:/home/user/projects/demo",),
        windows=(_unfocused(7, "2"),),
    )

    with pytest.raises(HopError, match="no focused window"):
        move_focused_window("demo", sway=sway)

    assert sway.moved_windows == []
    assert sway.switched_workspaces == []


def test_self_move_still_issues_the_sway_calls() -> None:
    """Sway no-ops both `move container to workspace <same>` and `workspace
    <focused>`, so there's no need to special-case self-move in the executor."""
    sway = StubSwayAdapter(
        workspaces=("p:/home/user/projects/demo",),
        windows=(_focused(42, "p:/home/user/projects/demo"),),
    )

    move_focused_window("demo", sway=sway)

    assert sway.moved_windows == [(42, "p:/home/user/projects/demo")]
    assert sway.switched_workspaces == ["p:/home/user/projects/demo"]
