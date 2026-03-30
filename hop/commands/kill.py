from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from hop.browser import DEFAULT_BROWSER_MARK_PREFIX
from hop.kitty import KittyWindow
from hop.session import ProjectSession, resolve_project_session
from hop.sway import SwayWindow


class KillSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def close_window(self, window_id: int) -> None: ...

    def list_session_workspaces(self, *, prefix: str = "p:") -> Sequence[str]: ...

    def remove_workspace(self, workspace_name: str) -> None: ...


class KillKittyAdapter(Protocol):
    def list_session_windows(self, session: ProjectSession) -> Sequence[KittyWindow]: ...

    def close_window(self, window_id: int) -> None: ...


def kill_session(
    cwd: Path | str,
    *,
    sway: KillSwayAdapter,
    kitty: KillKittyAdapter,
) -> ProjectSession:
    session = resolve_project_session(cwd)

    # Close browser window via Sway mark before closing Kitty windows.
    # Kitty windows include the terminal running hop kill — closing them sends SIGHUP
    # and kills this process before it can reach anything scheduled after.
    browser_mark = f"{DEFAULT_BROWSER_MARK_PREFIX}{session.session_name}"
    for window in sway.list_windows():
        if browser_mark in window.marks:
            sway.close_window(window.id)

    # Remove workspace before closing Kitty windows for the same reason.
    if session.workspace_name in sway.list_session_workspaces():
        sway.remove_workspace(session.workspace_name)

    # Close Kitty-managed windows last (role terminals + shared editor).
    # This may close the terminal running hop kill, ending this process.
    for window in kitty.list_session_windows(session):
        kitty.close_window(window.id)

    return session
