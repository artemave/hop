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

    # Close Kitty-managed windows (role terminals + shared editor)
    for window in kitty.list_session_windows(session):
        kitty.close_window(window.id)

    # Close browser window via Sway mark (even if it drifted to another workspace)
    browser_mark = f"{DEFAULT_BROWSER_MARK_PREFIX}{session.session_name}"
    for window in sway.list_windows():
        if browser_mark in window.marks:
            sway.close_window(window.id)

    # Remove workspace if it still exists after teardown
    if session.workspace_name in sway.list_session_workspaces():
        sway.remove_workspace(session.workspace_name)

    return session
