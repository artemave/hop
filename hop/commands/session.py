from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from hop.session import ProjectSession, derive_workspace_name, resolve_project_session

SESSION_WORKSPACE_PREFIX = "p:"
SHELL_TERMINAL_ROLE = "shell"


class SessionSwayAdapter(Protocol):
    def switch_to_workspace(self, workspace_name: str) -> None: ...

    def list_session_workspaces(self, *, prefix: str = SESSION_WORKSPACE_PREFIX) -> Sequence[str]: ...


class SessionTerminalAdapter(Protocol):
    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None: ...


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


def switch_session(
    session_name: str,
    *,
    sway: SessionSwayAdapter,
) -> str:
    workspace_name = derive_workspace_name(session_name)
    sway.switch_to_workspace(workspace_name)
    return workspace_name


def list_sessions(
    *,
    sway: SessionSwayAdapter,
    prefix: str = SESSION_WORKSPACE_PREFIX,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                workspace_name.removeprefix(prefix)
                for workspace_name in sway.list_session_workspaces(prefix=prefix)
                if workspace_name.startswith(prefix)
            }
        )
    )
