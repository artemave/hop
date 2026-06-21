from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectSession:
    session_root: Path
    session_name: str
    workspace_name: str
    # The ssh target when this session runs on a remote machine; ``None`` for a
    # local session. When set, ``session_root`` is a path on that remote host
    # (never touched as a local filesystem path) and hopd drives the backend
    # through an ``SshTransport`` to ``host``.
    host: str | None = None


def derive_session_root(
    start: Path | str,
) -> Path:
    return Path(start).expanduser().resolve()


def derive_session_name(session_root: Path | str) -> str:
    root = Path(session_root).expanduser().resolve()
    if not root.name:
        msg = f"Cannot derive a session name from {root!s}"
        raise ValueError(msg)
    return root.name


def derive_workspace_name(session_root: Path | str) -> str:
    root = Path(session_root).expanduser().resolve()
    return f"p:{root.name}"


def resolve_project_session(
    start: Path | str,
) -> ProjectSession:
    session_root = derive_session_root(start)
    session_name = derive_session_name(session_root)
    workspace_name = derive_workspace_name(session_root)
    return ProjectSession(
        session_root=session_root,
        session_name=session_name,
        workspace_name=workspace_name,
    )


def remote_session_from_env() -> ProjectSession | None:
    """Build a remote ``ProjectSession`` from ``HOP_REMOTE_HOST`` / ``HOP_REMOTE_CWD``.

    Set by the bridge when it dispatches a command for a remote session (the
    ``hop ssh`` enter, and any in-session ``hop open`` / ``hop run`` from inside
    the container): there's no local directory to root the subprocess in, so
    identity rides in the environment. Returns ``None`` for an ordinary local
    invocation, where the caller falls back to ``resolve_project_session(cwd)``.
    The remote ``cwd`` is used verbatim — it is a path on ``host``, never
    resolved against the local filesystem.
    """

    host = os.environ.get("HOP_REMOTE_HOST")
    cwd = os.environ.get("HOP_REMOTE_CWD")
    if not host or not cwd:
        return None
    root = Path(cwd)
    return ProjectSession(
        session_root=root,
        session_name=root.name,
        workspace_name=f"p:{root.name}",
        host=host,
    )
