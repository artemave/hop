from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

PROJECT_ROOT_MARKERS: tuple[str, ...] = (".git", ".dust", "pyproject.toml")


@dataclass(frozen=True, slots=True)
class ProjectSession:
    project_root: Path
    session_name: str
    workspace_name: str


def derive_project_root(
    start: Path | str,
    *,
    markers: Sequence[str] = PROJECT_ROOT_MARKERS,
) -> Path:
    candidate = Path(start).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent

    for directory in (candidate, *candidate.parents):
        if any((directory / marker).exists() for marker in markers):
            return directory

    return candidate


def derive_session_name(project_root: Path | str) -> str:
    root = Path(project_root).expanduser().resolve()
    if not root.name:
        msg = f"Cannot derive a session name from {root!s}"
        raise ValueError(msg)
    return root.name


def derive_workspace_name(session_name: str) -> str:
    return f"p:{session_name}"


def resolve_project_session(
    start: Path | str,
    *,
    markers: Sequence[str] = PROJECT_ROOT_MARKERS,
) -> ProjectSession:
    project_root = derive_project_root(start, markers=markers)
    session_name = derive_session_name(project_root)
    workspace_name = derive_workspace_name(session_name)
    return ProjectSession(
        project_root=project_root,
        session_name=session_name,
        workspace_name=workspace_name,
    )
