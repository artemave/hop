from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from hop.errors import HopError
from hop.kitty import KittyWindow
from hop.session import ProjectSession, resolve_project_session
from hop.state import SessionState, load_sessions

SESSION_WORKSPACE_PREFIX = "p:"
SHELL_TERMINAL_ROLE = "shell"
ADHOC_SHELL_ROLE_PREFIX = "shell-"


@dataclass(frozen=True, slots=True)
class SessionListing:
    name: str
    workspace: str
    project_root: Path | None


class SessionSwayAdapter(Protocol):
    def switch_to_workspace(self, workspace_name: str) -> None: ...

    def list_session_workspaces(self, *, prefix: str = SESSION_WORKSPACE_PREFIX) -> Sequence[str]: ...


class SessionTerminalAdapter(Protocol):
    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None: ...


class SpawnTerminalAdapter(SessionTerminalAdapter, Protocol):
    def list_session_windows(self, session: ProjectSession) -> Sequence[KittyWindow]: ...


def enter_project_session(
    cwd: Path | str,
    *,
    sway: SessionSwayAdapter,
    terminals: SessionTerminalAdapter,
) -> ProjectSession:
    session = resolve_project_session(cwd)
    sway.switch_to_workspace(session.workspace_name)
    terminals.ensure_terminal(session, role=SHELL_TERMINAL_ROLE)
    return session


def spawn_session_terminal(
    cwd: Path | str,
    *,
    terminals: SpawnTerminalAdapter,
) -> ProjectSession:
    session = resolve_project_session(cwd)
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
