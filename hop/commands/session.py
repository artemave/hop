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
    # The ssh target when the session runs on a remote machine, ``None`` locally.
    # Carried so hopd can rebuild a remote ``ProjectSession`` (e.g. to enumerate
    # its windows for vicinae) and drive probes over the transport, not locally
    # against a ``project_root`` that only exists on the remote.
    host: str | None = None


class SessionSwayAdapter(Protocol):
    def switch_to_workspace(self, workspace_name: str) -> None: ...

    def set_workspace_layout(self, workspace_name: str, layout: str) -> None: ...

    def list_session_workspaces(self, *, prefix: str = SESSION_WORKSPACE_PREFIX) -> Sequence[str]: ...

    def list_windows(self) -> Sequence[SwayWindow]: ...

    def focus_window(self, window_id: int) -> None: ...

    def get_focused_workspace(self) -> str: ...


class SessionTerminalAdapter(Protocol):
    def ensure_terminal(self, session: ProjectSession, *, role: str, already_prepared: bool = False) -> None: ...


class SpawnTerminalAdapter(SessionTerminalAdapter, Protocol):
    def list_session_windows(self, session: ProjectSession) -> Sequence[KittyWindow]: ...


class SessionEditorAdapter(Protocol):
    def ensure(self, session: ProjectSession, *, keep_focus: bool = True) -> None: ...


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
    session: ProjectSession | None = None,
) -> ProjectSession:
    # ``session`` lets the caller supply an already-resolved session — required
    # for a remote session, whose identity comes from the shim's (host, cwd),
    # not from ``cwd`` (which is the *local* home of the dispatching subprocess
    # and would otherwise re-resolve to the wrong, local session). Local callers
    # omit it and identity is derived from ``cwd`` as before.
    if session is None:
        session = resolve_project_session(cwd)
    # Switch only when we aren't already on the session's workspace. Two
    # reasons: (a) sway's `workspace_auto_back_and_forth yes` flips off the
    # focused workspace when re-targeted, which would yank the user away;
    # (b) the headless first-entry path may have switched eagerly already
    # (so the prepare popup lands on `p:<session>`), in which case we'd be
    # re-issuing the same switch. The check also re-affirms the target when
    # the user navigated away during a slow prepare popup — kitty windows
    # then bootstrap on the session's workspace rather than wherever the
    # user currently is.
    if sway.get_focused_workspace() != session.workspace_name:
        sway.switch_to_workspace(session.workspace_name)
    # Terminal must come first: on first entry the per-session kitty isn't
    # running yet, and only ensure_terminal knows how to bootstrap it (catch
    # KittyConnectionError → spawn kitty listening on the session socket).
    # The editor adapter talks to that socket directly with no fallback, so
    # ensuring the editor first would fail with "Could not talk to Kitty".
    # `editor` is only passed on first entry to a session — re-entry from
    # another workspace must not resurrect a deliberately-closed editor.
    # ``already_prepared=True`` short-circuits the redundant ``backend.prepare``
    # call inside the bootstrap path: the caller (resolve_for_entry inline, or
    # popup.run_prepare for headless) has already brought the backend up. A
    # repeat ``compose up -d`` on an up container can stall 20+ seconds doing
    # nothing useful.
    terminals.ensure_terminal(session, role=SHELL_TERMINAL_ROLE, already_prepared=True)
    if editor is None:
        # Re-entry path: caller signals "shell only" by omitting the editor
        # adapter. The activation sweep is gated on the same signal.
        return session
    if not windows:
        # No resolved windows (legacy callers/tests): fall back to bringing
        # up the editor alongside the shell, matching the pre-resolver
        # bootstrap behavior.
        editor.ensure(session, keep_focus=False)
    else:
        for window in windows:
            if window.role == SHELL_ROLE:
                continue
            if not window.active:
                continue
            if window.role == EDITOR_ROLE:
                editor.ensure(session, keep_focus=False)
            elif window.role == BROWSER_ROLE:
                if browser is not None:
                    browser.ensure_browser(session, url=None)
            else:
                terminals.ensure_terminal(session, role=window.role, already_prepared=True)
    # Each kitty `launch` IPC steals focus by default, so after the
    # activation sweep the focused window is whichever role landed last
    # (typically the editor or a layout window). Refocus the shell so the
    # session lands on a sensible starting point — and in a tabbed
    # workspace, makes the shell the visible tab.
    focused_on_session = _focus_shell_if_present(session, sway=sway)
    if workspace_layout is not None and focused_on_session:
        # Apply layout *after* the activation sweep, not before: sway reaps
        # empty named workspaces when focus leaves them, and a slow
        # ``prepare`` (devcontainer up, popup) can leave ``p:<session>`` empty
        # for the gap between the popup closing and the role window
        # registering. If the user has wandered to another workspace by then,
        # ``p:<session>`` gets destroyed and re-created at sway's default
        # layout when ``_adopt_role_window_to_workspace`` moves the role
        # window back. Setting layout here — after ``_focus_shell_if_present``
        # has refocused us onto ``p:<session>``, which now has the shell —
        # guarantees the layout sticks. The one-frame reflow as the shell
        # transitions from default to tabbed/stacking is imperceptible.
        sway.set_workspace_layout(session.workspace_name, workspace_layout)
    return session


def _focus_shell_if_present(session: ProjectSession, *, sway: SessionSwayAdapter) -> bool:
    shell_app_id = f"hop:{SHELL_ROLE}"
    candidates = [
        window
        for window in sway.list_windows()
        if (window.app_id == shell_app_id or window.window_class == shell_app_id)
        and window.workspace_name == session.workspace_name
    ]
    if not candidates:
        return False
    sway.focus_window(min(candidates, key=lambda window: window.id).id)
    return True


def spawn_session_terminal(
    cwd: Path | str,
    *,
    terminals: SpawnTerminalAdapter,
    session: ProjectSession | None = None,
) -> ProjectSession:
    # ``session`` lets the caller pass an already-resolved (possibly remote)
    # session; a remote one can't be re-derived from ``cwd`` (the local home of
    # the dispatching subprocess). Local callers omit it.
    if session is None:
        session = resolve_project_session(cwd)
    # `hop` from inside a session with a live kitty spawns the next free
    # `shell-<N>`. The dead-kitty case never reaches here — bare `hop` in
    # `EnterSessionCommand` routes that through the full first-entry
    # bootstrap instead, so prepare runs and every configured window comes
    # up. Re-creating a closed editor is the user's explicit choice via
    # `hop term --role editor` (or the vicinae `Hop editor` entry, which
    # calls the same command).
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
                host=recorded.backend.transport_host if recorded is not None else None,
            )
        )
    return tuple(sorted(listings, key=lambda listing: listing.name))
