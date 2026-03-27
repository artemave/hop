"""hop package scaffold."""

from hop.session import (
    PROJECT_ROOT_MARKERS,
    ProjectSession,
    derive_project_root,
    derive_session_name,
    derive_workspace_name,
    resolve_project_session,
)

__all__ = [
    "PROJECT_ROOT_MARKERS",
    "ProjectSession",
    "derive_project_root",
    "derive_session_name",
    "derive_workspace_name",
    "resolve_project_session",
]
