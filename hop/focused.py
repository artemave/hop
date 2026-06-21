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
from hop.state import SessionState, load_sessions, session_from_state
from hop.sway import SwayIpcAdapter
from hop.targets import (
    ResolvedFileTarget,
    SyntacticFileTarget,
    SyntacticRailsRefTarget,
    parse_visible_output_target,
    resolve_file_candidate,
    resolve_target,
)

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

    # Pick the base cwd against which relative candidates resolve. OSC 7
    # from the in-shell shell is the ground truth (cd-aware). If the shell
    # isn't emitting it, fall back to the backend's cached ``workspace_path``
    # (its default cwd, captured at bootstrap via ``<noninteractive_prefix>
    # pwd``) — works for the at-default-cwd case without any in-shell setup.
    # Last resort is the host-side project root, which is the wrong namespace
    # for non-host backends but is preserved here as a fallback so the host
    # backend (no workspace_path) still resolves relatives.
    in_shell_cwd = cwd_fn(session_name)
    if in_shell_cwd is not None:
        base_cwd = in_shell_cwd
    elif state.backend.workspace_path is not None:
        base_cwd = Path(state.backend.workspace_path)
    else:
        base_cwd = state.session_root

    session = session_from_state(state)

    # Two checks happen here. Plain file refs flow through ``backend.paths_exist``
    # in one batched call. Rails refs run through ``resolve_target``, which
    # reads the controller file via ``backend.read_file`` and scans for
    # ``def <action>`` — so the highlight only fires when the action really
    # exists, not just because the controller file does. Rails refs that
    # survive that check are added to the result directly (the file read
    # already proved existence).
    plain_files_to_check: dict[str, Path] = {}
    verified: set[str] = set()
    for candidate in candidate_list:
        syntactic = parse_visible_output_target(candidate)
        if isinstance(syntactic, SyntacticFileTarget):
            path = resolve_file_candidate(syntactic.path_text, terminal_cwd=base_cwd)
            plain_files_to_check.setdefault(candidate, path)
        elif isinstance(syntactic, SyntacticRailsRefTarget):
            resolved = resolve_target(syntactic, session=session, backend=backend, terminal_cwd=base_cwd)
            if isinstance(resolved, ResolvedFileTarget):
                verified.add(candidate)

    if plain_files_to_check:
        existing_paths = backend.paths_exist(session, tuple(set(plain_files_to_check.values())))
        for candidate, path in plain_files_to_check.items():
            if path in existing_paths:
                verified.add(candidate)
    return verified


def _default_focused_workspace() -> str:
    return SwayIpcAdapter().get_focused_workspace()


def _default_backend_loader(state: SessionState) -> SessionBackend | None:
    # Imported lazily to break a potential import cycle: hop.app pulls in
    # adapters that don't need to know about hop.focused.
    from hop.app import backend_from_record

    return backend_from_record(state.backend, session_root=state.session_root)


def _session_name_from_workspace(workspace_name: str) -> str | None:
    if not workspace_name.startswith(WORKSPACE_PREFIX):
        return None
    suffix = workspace_name[len(WORKSPACE_PREFIX) :]
    return suffix or None


def _local_fallback(candidates: list[str], *, base_cwd: Path) -> set[str]:
    # No session, no backend — Rails refs can't run their def-line lookup
    # here. Only plain file shapes are checked; URLs and Rails refs are
    # dropped. Rails-ref highlighting outside hop sessions is rare enough
    # that the loss isn't worth a host-only def grep.
    surviving: set[str] = set()
    for candidate in candidates:
        syntactic = parse_visible_output_target(candidate)
        if not isinstance(syntactic, SyntacticFileTarget):
            continue
        path = resolve_file_candidate(syntactic.path_text, terminal_cwd=base_cwd)
        if path.exists():
            surviving.add(candidate)
    return surviving


__all__ = ["paths_exist"]
