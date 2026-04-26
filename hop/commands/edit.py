from __future__ import annotations

from pathlib import Path
from typing import Protocol

from hop.session import ProjectSession, resolve_project_session


class EditNeovimAdapter(Protocol):
    def focus(self, session: ProjectSession) -> None: ...

    def open_target(self, session: ProjectSession, *, target: str) -> None: ...


def edit_in_session(
    cwd: Path | str,
    *,
    neovim: EditNeovimAdapter,
    target: str | None = None,
) -> ProjectSession:
    session = resolve_project_session(cwd)

    if target is None:
        neovim.focus(session)
    else:
        neovim.open_target(session, target=target)

    return session
