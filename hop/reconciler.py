"""Reconcile session-window marks against current Sway workspace placement.

`hopd` runs this on every Sway `window` event so that raw Sway moves of the
session editor or browser off `p:<session>` clear the corresponding mark —
the window becomes a regular Sway window with no hop affiliation, and the
next `hop open` / `hop browser` launches a fresh one rather than yanking
the moved window back.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from hop.browser import DEFAULT_BROWSER_MARK_PREFIX
from hop.commands.session import SESSION_WORKSPACE_PREFIX
from hop.sway import SwayWindow

_SESSION_MARK_PREFIXES = (DEFAULT_BROWSER_MARK_PREFIX,)


class ReconcilerSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def unmark_window(self, window_id: int, mark: str) -> None: ...


def reconcile_marks(sway: ReconcilerSwayAdapter) -> None:
    for window in sway.list_windows():
        for mark in window.marks:
            session_name = _session_name_for_mark(mark)
            if session_name is None:
                continue
            expected_workspace = f"{SESSION_WORKSPACE_PREFIX}{session_name}"
            if window.workspace_name != expected_workspace:
                sway.unmark_window(window.id, mark)


def _session_name_for_mark(mark: str) -> str | None:
    for prefix in _SESSION_MARK_PREFIXES:
        if mark.startswith(prefix):
            return mark[len(prefix) :]
    return None
