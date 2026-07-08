from typing import Sequence

from hop.reconciler import reconcile_marks
from hop.sway import SwayWindow


class StubSwayAdapter:
    def __init__(self, windows: Sequence[SwayWindow]) -> None:
        self._windows = tuple(windows)
        self.unmarked: list[tuple[int, str]] = []

    def list_windows(self) -> Sequence[SwayWindow]:
        return self._windows

    def unmark_window(self, window_id: int, mark: str) -> None:
        self.unmarked.append((window_id, mark))


def _window(
    window_id: int,
    *,
    workspace_name: str,
    marks: tuple[str, ...] = (),
    app_id: str | None = "kitty",
) -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name=workspace_name,
        app_id=app_id,
        window_class=None,
        marks=marks,
    )


def test_reconciler_clears_browser_mark_when_window_left_session_workspace() -> None:
    sway = StubSwayAdapter([_window(99, workspace_name="2", marks=("_hop_browser:demo",))])

    reconcile_marks(sway)

    assert sway.unmarked == [(99, "_hop_browser:demo")]


def test_reconciler_is_a_noop_when_marks_match_their_session_workspace() -> None:
    sway = StubSwayAdapter(
        [
            _window(1, workspace_name="p:demo", marks=("_hop_browser:demo",)),
            _window(2, workspace_name="p:other", marks=("_hop_browser:other",)),
        ]
    )

    reconcile_marks(sway)

    assert sway.unmarked == []


def test_reconciler_handles_multiple_sessions_independently() -> None:
    sway = StubSwayAdapter(
        [
            _window(1, workspace_name="p:demo", marks=("_hop_browser:demo",)),  # placed correctly
            _window(2, workspace_name="p:other", marks=("_hop_browser:demo",)),  # drifted
            _window(3, workspace_name="p:other", marks=("_hop_browser:other",)),  # placed correctly
            _window(4, workspace_name="p:demo", marks=("_hop_browser:other",)),  # drifted
        ]
    )

    reconcile_marks(sway)

    assert sorted(sway.unmarked) == [(2, "_hop_browser:demo"), (4, "_hop_browser:other")]


def test_reconciler_leaves_unrelated_marks_alone() -> None:
    sway = StubSwayAdapter(
        [
            _window(1, workspace_name="2", marks=("user-favorite",)),
            _window(2, workspace_name="p:other", marks=("scratchpad",)),
        ]
    )

    reconcile_marks(sway)

    assert sway.unmarked == []


def test_reconciler_clears_only_session_marks_on_a_mixed_window() -> None:
    """A window carrying a stray session mark plus an unrelated user mark
    loses only the session mark."""
    sway = StubSwayAdapter(
        [
            _window(
                42,
                workspace_name="p:other",
                marks=("_hop_browser:demo", "user-favorite"),
            )
        ]
    )

    reconcile_marks(sway)

    assert sway.unmarked == [(42, "_hop_browser:demo")]
