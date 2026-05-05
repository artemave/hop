from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from hop.session import ProjectSession, resolve_project_session
from hop.sway import SwayWindow


class TermKittyAdapter(Protocol):
    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None: ...


class TermSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def focus_window(self, window_id: int) -> None: ...


def focus_terminal(
    cwd: Path | str,
    *,
    terminals: TermKittyAdapter,
    sway: TermSwayAdapter,
    role: str,
) -> ProjectSession:
    session = resolve_project_session(cwd)
    terminals.ensure_terminal(session, role=role)
    # Kitty's `focus-window` IPC asks Sway for focus via the
    # `xdg-activation` protocol, which Sway can refuse when the
    # activation token has gone stale — typically because vicinae's UI
    # close races with the kitty server's activation request when
    # scripts fire back-to-back. Sway IPC focus is unconditional, so we
    # look up the role's OS window by app_id and direct sway to focus
    # it. Editor + browser already use this same pattern (see
    # editor.py's `focus()` for the why).
    target = _find_role_window(session, role=role, sway=sway)
    if target is not None:
        sway.focus_window(target.id)
    return session


def _find_role_window(
    session: ProjectSession,
    *,
    role: str,
    sway: TermSwayAdapter,
) -> SwayWindow | None:
    app_id = f"hop:{role}"
    candidates = [
        window
        for window in sway.list_windows()
        if (window.app_id == app_id or window.window_class == app_id)
        and window.workspace_name == session.workspace_name
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda window: window.id)
