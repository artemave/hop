from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from hop.config import BROWSER_ROLE, EDITOR_ROLE, SHELL_ROLE
from hop.errors import HopError
from hop.kitty import KittyWindow
from hop.layouts import WindowSpec
from hop.session import ProjectSession, resolve_project_session
from hop.state import SessionState, load_sessions
from hop.sway import SwayWindow

SESSION_WORKSPACE_PREFIX = "p:"
SHELL_TERMINAL_ROLE = SHELL_ROLE
ADHOC_SHELL_ROLE_PREFIX = "shell-"


@dataclass(frozen=True, slots=True)
class SessionListing:
    name: str
    workspace: str
    project_root: Path | None


class SessionSwayAdapter(Protocol):
    def switch_to_workspace(self, workspace_name: str) -> None: ...

    def set_workspace_layout(self, workspace_name: str, layout: str) -> None: ...

    def list_session_workspaces(self, *, prefix: str = SESSION_WORKSPACE_PREFIX) -> Sequence[str]: ...

    def list_windows(self) -> Sequence[SwayWindow]: ...

    def focus_window(self, window_id: int) -> None: ...


class SessionTerminalAdapter(Protocol):
    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None: ...


class SpawnTerminalAdapter(SessionTerminalAdapter, Protocol):
    def list_session_windows(self, session: ProjectSession) -> Sequence[KittyWindow]: ...


class SessionEditorAdapter(Protocol):
    def ensure(self, session: ProjectSession) -> bool: ...


class SessionBrowserAutostartAdapter(Protocol):
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None: ...


def enter_project_session(
    cwd: Path | str,
    *,
    sway: SessionSwayAdapter,
    terminals: SessionTerminalAdapter,
    editor: SessionEditorAdapter | None = None,
    browser: SessionBrowserAutostartAdapter | None = None,
    windows: Sequence[WindowSpec] = (),
    workspace_layout: str | None = None,
) -> ProjectSession:
    session = resolve_project_session(cwd)
    sway.switch_to_workspace(session.workspace_name)
    if workspace_layout is not None:
        # Apply before launching any windows so the first one lands in the
        # configured arrangement (tabbed, stacking, splith, splitv) instead
        # of getting placed under sway's default layout and then reflowed.
        sway.set_workspace_layout(session.workspace_name, workspace_layout)
    # Terminal must come first: on first entry the per-session kitty isn't
    # running yet, and only ensure_terminal knows how to bootstrap it (catch
    # KittyConnectionError → spawn kitty listening on the session socket).
    # The editor adapter talks to that socket directly with no fallback, so
    # ensuring the editor first would fail with "Could not talk to Kitty".
    # `editor` is only passed on first entry to a session — re-entry from
    # another workspace must not resurrect a deliberately-closed editor.
    terminals.ensure_terminal(session, role=SHELL_TERMINAL_ROLE)
    if editor is None:
        # Re-entry path: caller signals "shell only" by omitting the editor
        # adapter. The autostart sweep is gated on the same signal.
        return session
    if not windows:
        # No resolved windows (legacy callers/tests): fall back to bringing
        # up the editor alongside the shell, matching the pre-resolver
        # bootstrap behavior.
        editor.ensure(session)
    else:
        for window in windows:
            if window.role == SHELL_ROLE:
                continue
            if not window.autostart_active:
                continue
            if window.role == EDITOR_ROLE:
                editor.ensure(session)
            elif window.role == BROWSER_ROLE:
                if browser is not None:
                    browser.ensure_browser(session, url=None)
            else:
                terminals.ensure_terminal(session, role=window.role)
    # Each kitty `launch` IPC steals focus by default, so after the
    # autostart sweep the focused window is whichever role landed last
    # (typically the editor or a layout window). Refocus the shell so the
    # session lands on a sensible starting point — and in a tabbed
    # workspace, makes the shell the visible tab.
    _focus_shell_if_present(session, sway=sway)
    return session


def _focus_shell_if_present(session: ProjectSession, *, sway: SessionSwayAdapter) -> None:
    shell_app_id = f"hop:{SHELL_ROLE}"
    candidates = [
        window
        for window in sway.list_windows()
        if (window.app_id == shell_app_id or window.window_class == shell_app_id)
        and window.workspace_name == session.workspace_name
    ]
    if not candidates:
        return
    sway.focus_window(min(candidates, key=lambda window: window.id).id)


def spawn_session_terminal(
    cwd: Path | str,
    *,
    terminals: SpawnTerminalAdapter,
    editor: SessionEditorAdapter,
) -> ProjectSession:
    session = resolve_project_session(cwd)
    # `hop` from inside a session does at most one thing: if the editor
    # has been closed, resurrect it; otherwise spawn a numbered shell. The
    # user expectation is "one keystroke, one new window" — bringing back
    # the editor and *also* opening another shell would clutter the
    # workspace whenever the user's intent was just to recover the editor.
    if editor.ensure(session):
        return session
    existing_roles = {window.role for window in terminals.list_session_windows(session) if window.role}
    role = _next_adhoc_shell_role(existing_roles)
    terminals.ensure_terminal(session, role=role)
    return session


def _next_adhoc_shell_role(existing_roles: set[str]) -> str:
    n = 2
    while f"{ADHOC_SHELL_ROLE_PREFIX}{n}" in existing_roles:
        n += 1
    return f"{ADHOC_SHELL_ROLE_PREFIX}{n}"


def switch_session(
    session_name: str,
    *,
    sway: SessionSwayAdapter,
) -> str:
    workspaces = sway.list_session_workspaces()
    matching = [w for w in workspaces if Path(w.removeprefix(SESSION_WORKSPACE_PREFIX)).name == session_name]
    if not matching:
        msg = f"No active session named {session_name!r}."
        raise HopError(msg)
    workspace_name = sorted(matching)[0]
    sway.switch_to_workspace(workspace_name)
    return workspace_name


def list_sessions(
    *,
    sway: SessionSwayAdapter,
    prefix: str = SESSION_WORKSPACE_PREFIX,
    sessions_loader: Callable[[], dict[str, SessionState]] = load_sessions,
) -> tuple[SessionListing, ...]:
    workspace_names = sway.list_session_workspaces(prefix=prefix)
    state = sessions_loader()
    listings: list[SessionListing] = []
    for workspace_name in workspace_names:
        name = workspace_name.removeprefix(prefix)
        recorded = state.get(name)
        listings.append(
            SessionListing(
                name=name,
                workspace=workspace_name,
                project_root=recorded.project_root if recorded is not None else None,
            )
        )
    return tuple(sorted(listings, key=lambda listing: listing.name))
