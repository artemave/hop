from __future__ import annotations

from pathlib import Path
from typing import Protocol

from hop.session import ProjectSession, resolve_project_session


class TermKittyAdapter(Protocol):
    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None: ...


def focus_terminal(
    cwd: Path | str,
    *,
    terminals: TermKittyAdapter,
    role: str,
) -> ProjectSession:
    session = resolve_project_session(cwd)
    terminals.ensure_terminal(session, role=role)
    return session
