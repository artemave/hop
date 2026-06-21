from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from hop.config import EDITOR_ROLE
from hop.session import ProjectSession, remote_session_from_env, resolve_project_session
from hop.sway import SwayWindow


class TermKittyAdapter(Protocol):
    def ensure_terminal(self, session: ProjectSession, *, role: str, already_prepared: bool = False) -> None: ...


class TermSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def focus_window(self, window_id: int) -> None: ...


class TermNeovimAdapter(Protocol):
    def focus(self, session: ProjectSession) -> None: ...


def focus_terminal(
    cwd: Path | str,
    *,
    terminals: TermKittyAdapter,
    sway: TermSwayAdapter,
    neovim: TermNeovimAdapter,
    role: str,
) -> ProjectSession:
    session = remote_session_from_env() or resolve_project_session(cwd)
    if role == EDITOR_ROLE:
        # The editor isn't a plain kitty role terminal — it has its own
        # launch path (composed `<editor>; <shell>`, deterministic listen
        # socket, `_hop_editor:<session>` sway mark). Delegating to the
        # neovim adapter's `focus` reuses the existing launch-if-missing /
        # focus-if-present / recreate-if-quit lifecycle, and skips this
        # function's trailing sway focus escalation because the editor
        # adapter does its own through `_sway.focus_window`.
        neovim.focus(session)
        return session
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
