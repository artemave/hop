from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Protocol

from hop.backends import HostBackend, SessionBackend
from hop.kitty import session_name_from_listen_on
from hop.session import ProjectSession, resolve_project_session
from hop.state import SessionState, load_sessions
from hop.targets import ResolvedUrlTarget, resolve_visible_output_target

logger = logging.getLogger("hop.open_selection")


class OpenSelectionNeovimAdapter(Protocol):
    def open_target(self, session: ProjectSession, *, target: str) -> None: ...


class OpenSelectionBrowserAdapter(Protocol):
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None: ...


def open_selection_in_window(
    selection: str,
    *,
    source_cwd: Path | str | None,
    listen_on: str | None,
    neovim: OpenSelectionNeovimAdapter,
    browser: OpenSelectionBrowserAdapter,
    sessions_loader: Callable[[], dict[str, SessionState]] = load_sessions,
    session_backend_for: Callable[[ProjectSession], SessionBackend] = lambda _session: HostBackend(),
) -> ProjectSession | None:
    session_name = session_name_from_listen_on(listen_on) if listen_on else None
    if session_name is None:
        logger.info("listen_on=%r is not a hop session socket; selection=%r", listen_on, selection)
        return None

    state = sessions_loader().get(session_name)
    if state is None:
        logger.warning("no recorded session state for %r; selection=%r", session_name, selection)
        return None

    if source_cwd is None:
        logger.warning("source window has no cwd; selection=%r", selection)
        return None

    session = resolve_project_session(state.project_root)
    backend = session_backend_for(session)
    translated_cwd = backend.translate_terminal_cwd(session, Path(source_cwd))

    resolved_target = resolve_visible_output_target(
        selection,
        terminal_cwd=translated_cwd,
        project_root=session.project_root,
    )
    if resolved_target is None:
        logger.info(
            "could not resolve %r against terminal_cwd=%s project_root=%s",
            selection,
            translated_cwd,
            session.project_root,
        )
        return None

    if isinstance(resolved_target, ResolvedUrlTarget):
        logger.info("dispatching url %r to session %r", resolved_target.url, session_name)
        browser.ensure_browser(session, url=resolved_target.url)
    else:
        logger.info(
            "dispatching file %r to session %r",
            resolved_target.editor_target,
            session_name,
        )
        neovim.open_target(session, target=resolved_target.editor_target)

    return session
