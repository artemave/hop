"""``hopd`` — long-lived Sway IPC subscriber that maintains the vicinae script set.

Wired into the user's sway config via ``exec hopd`` (not ``exec_always`` —
``exec`` runs once at sway startup, and the IPC subscription survives
reloads, so a single instance covers the whole sway session).
"""

from __future__ import annotations

import sys
from typing import Callable, Sequence

from hop.app import SessionBackendRegistry
from hop.commands.session import SESSION_WORKSPACE_PREFIX, SessionListing, list_sessions
from hop.errors import HopError
from hop.state import SessionState, forget_session, load_sessions
from hop.sway import SwayIpcAdapter
from hop.vicinae import default_scripts_dir, regenerate


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    sway = SwayIpcAdapter()
    registry = SessionBackendRegistry()
    scripts_dir = default_scripts_dir()

    def sessions_loader() -> Sequence[SessionListing]:
        return list_sessions(sway=sway)

    windows_for = registry.resolve_windows_for_entry

    try:
        _sweep_stale_persisted_sessions(sway=sway)
        regenerate(
            sway=sway,
            sessions_loader=sessions_loader,
            scripts_dir=scripts_dir,
            windows_for=windows_for,
        )
        for _event in sway.subscribe_to_workspace_events():
            _sweep_stale_persisted_sessions(sway=sway)
            regenerate(
                sway=sway,
                sessions_loader=sessions_loader,
                scripts_dir=scripts_dir,
                windows_for=windows_for,
            )
    except HopError as error:
        print(str(error), file=sys.stderr)
        return 1

    print("hopd: Sway IPC subscription ended", file=sys.stderr)
    return 1


def _sweep_stale_persisted_sessions(
    *,
    sway: SwayIpcAdapter,
    sessions_loader: Callable[[], dict[str, SessionState]] = load_sessions,
    forget: Callable[[str], None] = forget_session,
) -> None:
    """Drop persisted state files whose `p:<name>` workspace is no longer alive.

    The CLI's first-entry gate keys on kitty socket liveness, so stale state
    is a tidiness concern rather than a correctness bug. Run on every
    workspace event so that a session's state file disappears within one
    event of the workspace being destroyed.
    """
    live_workspaces = set(sway.list_session_workspaces(prefix=SESSION_WORKSPACE_PREFIX))
    for name in sessions_loader():
        workspace = f"{SESSION_WORKSPACE_PREFIX}{name}"
        if workspace not in live_workspaces:
            forget(name)
