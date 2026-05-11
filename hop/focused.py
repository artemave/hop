"""Focused-session helpers for path existence and (future) related queries.

The open-selection kitten asks hop "do these path-shaped tokens exist?" via
``paths_exist``. Inside, hop resolves the currently focused hop session,
asks kitty for the focused window's in-shell cwd, reconstructs the session's
backend, and consults ``backend.paths_exist``. The kitten never touches sway,
kitty IPC, or backend internals.

When no hop session is focused (e.g. the kitten fired from a non-hop kitty
window) or the focused session's kitty isn't reachable, hop falls back to a
local check against the current process's cwd so the kitten remains useful
outside hop sessions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from hop.backends import SessionBackend
from hop.kitty import get_focused_window_cwd
from hop.session import resolve_project_session
from hop.state import SessionState, load_sessions
from hop.sway import SwayIpcAdapter
from hop.targets import ResolvedFileTarget, resolve_visible_output_target

WORKSPACE_PREFIX = "p:"


def paths_exist(
    candidates: Iterable[str],
    *,
    focused_workspace: Callable[[], str] | None = None,
    sessions_loader: Callable[[], dict[str, SessionState]] | None = None,
    cwd_loader: Callable[[str], Path | None] | None = None,
    backend_loader: Callable[[SessionState], SessionBackend | None] | None = None,
) -> set[str]:
    """Return the subset of ``candidates`` that exist for the focused session.

    Relative candidates are resolved against the focused window's in-shell
    cwd (via kitty's per-session socket). Absolute candidates are checked
    as-is. The backend's ``paths_exist`` does the actual filesystem check.

    Returns the original input strings (not resolved Paths), so callers
    matching by string identity (e.g. the kitten's regex marks) line up
    cleanly.

    Falls back to local ``Path.exists()`` against ``Path.cwd()`` when no hop
    session is focused or the focused session's kitty isn't reachable.

    The ``*_loader`` kwargs are injection points for tests; production
    callers pass nothing and get the default sway/kitty/state wiring.
    """

    candidate_list = list(candidates)
    if not candidate_list:
        return set()

    workspace_fn = focused_workspace or _default_focused_workspace
    sessions_fn = sessions_loader or load_sessions
    cwd_fn = cwd_loader or get_focused_window_cwd
    backend_fn = backend_loader or _default_backend_loader

    try:
        workspace_name = workspace_fn()
    except Exception:
        return _local_fallback(candidate_list, base_cwd=Path.cwd())

    session_name = _session_name_from_workspace(workspace_name)
    if session_name is None:
        return _local_fallback(candidate_list, base_cwd=Path.cwd())

    state = sessions_fn().get(session_name)
    if state is None:
        return _local_fallback(candidate_list, base_cwd=Path.cwd())

    backend = backend_fn(state)
    if backend is None:
        return _local_fallback(candidate_list, base_cwd=Path.cwd())

    in_shell_cwd = cwd_fn(session_name)
    base_cwd = in_shell_cwd if in_shell_cwd is not None else state.project_root

    session = resolve_project_session(state.project_root)

    # Resolve each candidate string via the standard target parser so rails
    # refs and file:line shapes turn into real paths. URL targets are not
    # this function's concern — callers (the kitten) handle them separately.
    resolved_by_candidate: dict[str, Path] = {}
    for candidate in candidate_list:
        target = resolve_visible_output_target(candidate, terminal_cwd=base_cwd)
        if isinstance(target, ResolvedFileTarget):
            resolved_by_candidate.setdefault(candidate, target.path)

    if not resolved_by_candidate:
        return set()

    existing_paths = backend.paths_exist(session, tuple(set(resolved_by_candidate.values())))
    return {candidate for candidate, path in resolved_by_candidate.items() if path in existing_paths}


def _default_focused_workspace() -> str:
    return SwayIpcAdapter().get_focused_workspace()


def _default_backend_loader(state: SessionState) -> SessionBackend | None:
    # Imported lazily to break a potential import cycle: hop.app pulls in
    # adapters that don't need to know about hop.focused.
    from hop.app import backend_from_record

    return backend_from_record(state.backend)


def _session_name_from_workspace(workspace_name: str) -> str | None:
    if not workspace_name.startswith(WORKSPACE_PREFIX):
        return None
    suffix = workspace_name[len(WORKSPACE_PREFIX) :]
    return suffix or None


def _local_fallback(candidates: list[str], *, base_cwd: Path) -> set[str]:
    surviving: set[str] = set()
    for candidate in candidates:
        target = resolve_visible_output_target(candidate, terminal_cwd=base_cwd)
        if isinstance(target, ResolvedFileTarget) and target.path.exists():
            surviving.add(candidate)
    return surviving


__all__ = ["paths_exist"]
