from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectSession:
    project_root: Path
    session_name: str
    workspace_name: str


def derive_project_root(
    start: Path | str,
) -> Path:
    return Path(start).expanduser().resolve()


def derive_session_name(project_root: Path | str) -> str:
    root = Path(project_root).expanduser().resolve()
    if not root.name:
        msg = f"Cannot derive a session name from {root!s}"
        raise ValueError(msg)
    return root.name


def derive_workspace_name(project_root: Path | str) -> str:
    root = Path(project_root).expanduser().resolve()
    return f"p:{root}"


def resolve_project_session(
    start: Path | str,
) -> ProjectSession:
    project_root = derive_project_root(start)
    session_name = derive_session_name(project_root)
    workspace_name = derive_workspace_name(project_root)
    return ProjectSession(
        project_root=project_root,
        session_name=session_name,
        workspace_name=workspace_name,
    )
