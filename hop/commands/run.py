from __future__ import annotations

from pathlib import Path
from typing import Protocol

from hop.session import ProjectSession, resolve_project_session

DEFAULT_RUN_ROLE = "shell"


class RunSwayAdapter(Protocol):
    def switch_to_workspace(self, workspace_name: str) -> None: ...


class RunKittyAdapter(Protocol):
    def run_in_terminal(
        self,
        session: ProjectSession,
        *,
        role: str,
        command: str,
    ) -> None: ...


def run_command(
    cwd: Path | str,
    *,
    sway: RunSwayAdapter,
    terminals: RunKittyAdapter,
    command: str,
    role: str = DEFAULT_RUN_ROLE,
) -> ProjectSession:
    session = resolve_project_session(cwd)
    sway.switch_to_workspace(session.workspace_name)
    terminals.run_in_terminal(session, role=role, command=command)
    return session
