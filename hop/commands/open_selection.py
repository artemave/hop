from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from hop.backends import CommandBackend, SessionBackend
from hop.commands.open import OpenBrowserAdapter, OpenTargetNeovimAdapter, dispatch_resolved_target
from hop.kitty import session_name_from_listen_on
from hop.session import ProjectSession, resolve_project_session
from hop.state import SessionState, load_sessions
from hop.targets import (
    ResolvedFileTarget,
    ResolvedUrlTarget,
    SyntacticRailsRefTarget,
    parse_visible_output_target,
    resolve_target,
)

logger = logging.getLogger("hop.open_selection")

_BUILTIN_HOST_BACKEND = CommandBackend(name="host", interactive_prefix="", noninteractive_prefix="")


def open_selection_in_window(
    selection: str,
    *,
    source_cwd: Path | str | None,
    listen_on: str | None,
    neovim: OpenTargetNeovimAdapter,
    browser: OpenBrowserAdapter,
    sessions_loader: Callable[[], dict[str, SessionState]] = load_sessions,
    session_backend_for: Callable[[ProjectSession], SessionBackend] = lambda _session: _BUILTIN_HOST_BACKEND,
) -> ProjectSession | None:
    session_name = session_name_from_listen_on(listen_on) if listen_on else None
    if session_name is None:
        logger.info("listen_on=%r is not a hop session socket; selection=%r", listen_on, selection)
        return None

    state = sessions_loader().get(session_name)
    if state is None:
        logger.warning("no recorded session state for %r; selection=%r", session_name, selection)
        return None

    session = resolve_project_session(state.project_root)
    backend = session_backend_for(session)

    # Pick the cwd against which relative candidates resolve. ``source_cwd``
    # comes from kitty's ``window.cwd_of_child`` which is the foreground
    # process's /proc cwd — for container/ssh backends that's the host
    # launch directory, not the in-backend path, so resolving against it
    # gives paths the backend can't see. Prefer ``backend.workspace_path``
    # (probed via ``<noninteractive_prefix> pwd`` at bootstrap) whenever
    # the backend has one; for the host backend fall back to ``source_cwd``
    # (which is meaningful there) or the project root as a last resort.
    backend_workspace = getattr(state.backend, "workspace_path", None)
    if backend_workspace is not None:
        base_cwd: Path = Path(backend_workspace)
    elif source_cwd is not None:
        base_cwd = Path(source_cwd)
    else:
        base_cwd = state.project_root

    syntactic = parse_visible_output_target(selection)
    if syntactic is None:
        logger.info("could not parse %r against terminal_cwd=%s", selection, base_cwd)
        return None

    resolved_target = resolve_target(syntactic, session=session, backend=backend, terminal_cwd=base_cwd)
    if resolved_target is None:
        # Rails refs whose file is missing or whose action isn't defined
        # filter to None inside resolve_target. The kitten's own highlight
        # check already filtered most of these out; this is the dispatch-
        # time double check (the editor's state may have changed since the
        # hint was painted).
        logger.info("could not resolve %r against terminal_cwd=%s", selection, base_cwd)
        return None

    # Existence check for plain file refs only — Rails refs were verified
    # inside resolve_target (it had to read the file to find the def line).
    # Highlighting a stale path-shaped token from terminal output would be
    # actively misleading; dispatching one from the CLI is the user's call.
    if (
        isinstance(resolved_target, ResolvedFileTarget)
        and not isinstance(syntactic, SyntacticRailsRefTarget)
        and not backend.paths_exist(session, (resolved_target.path,))
    ):
        logger.info(
            "candidate %r does not exist in backend for session %r",
            str(resolved_target.path),
            session_name,
        )
        return None

    dispatched = dispatch_resolved_target(
        resolved_target,
        session=session,
        backend=backend,
        neovim=neovim,
        browser=browser,
    )
    if isinstance(dispatched, ResolvedUrlTarget):
        logger.info("dispatching url %r to session %r", dispatched.url, session_name)
    else:
        logger.info("dispatching file %r to session %r", dispatched.editor_target, session_name)
    return session
