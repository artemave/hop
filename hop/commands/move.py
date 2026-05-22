from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from hop.commands.session import SESSION_WORKSPACE_PREFIX
from hop.errors import HopError
from hop.sway import SwayWindow


class MoveSwayAdapter(Protocol):
    def list_session_workspaces(self, *, prefix: str = SESSION_WORKSPACE_PREFIX) -> Sequence[str]: ...

    def list_windows(self) -> Sequence[SwayWindow]: ...

    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None: ...

    def switch_to_workspace(self, workspace_name: str) -> None: ...


def move_focused_window(session_name: str, *, sway: MoveSwayAdapter) -> None:
    workspaces = sway.list_session_workspaces()
    matching = [w for w in workspaces if Path(w.removeprefix(SESSION_WORKSPACE_PREFIX)).name == session_name]
    if not matching:
        raise HopError(f"hop move: no session named {session_name!r}.")
    workspace_name = sorted(matching)[0]

    focused = next((w for w in sway.list_windows() if w.focused), None)
    if focused is None:
        raise HopError("hop move: no focused window.")

    sway.move_window_to_workspace(focused.id, workspace_name)
    sway.switch_to_workspace(workspace_name)
