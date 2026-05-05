"""``hopd`` — long-lived Sway IPC subscriber that maintains the vicinae script set.

Wired into the user's sway config via ``exec hopd`` (not ``exec_always`` —
``exec`` runs once at sway startup, and the IPC subscription survives
reloads, so a single instance covers the whole sway session).
"""

from __future__ import annotations

import sys
from typing import Sequence

from hop.app import SessionBackendRegistry
from hop.commands.session import SessionListing, list_sessions
from hop.errors import HopError
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
        regenerate(
            sway=sway,
            sessions_loader=sessions_loader,
            scripts_dir=scripts_dir,
            windows_for=windows_for,
        )
        for _event in sway.subscribe_to_workspace_events():
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
