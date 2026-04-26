from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Protocol, Sequence

from hop.errors import HopError
from hop.kitty import KittyWindow
from hop.session import ProjectSession, resolve_project_session

SESSION_WORKSPACE_PREFIX = "p:"
SHELL_TERMINAL_ROLE = "shell"
ADHOC_SHELL_ROLE_PREFIX = "shell-"


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
) -> tuple[str, ...]:
    workspace_names = sway.list_session_workspaces(prefix=prefix)
    session_paths = [Path(w.removeprefix(prefix)) for w in workspace_names if w.startswith(prefix)]
    basename_counts: Counter[str] = Counter(p.name for p in session_paths)
    return tuple(sorted(p.name if basename_counts[p.name] == 1 else str(p) for p in session_paths))
