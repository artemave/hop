"""hop package scaffold."""

from hop.session import (
    ProjectSession,
    derive_project_root,
    derive_session_name,
    derive_workspace_name,
    resolve_project_session,
)

__all__ = [
    "ProjectSession",
    "derive_project_root",
    "derive_session_name",
    "derive_workspace_name",
    "resolve_project_session",
]
