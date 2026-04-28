from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Protocol

from hop.session import ProjectSession, resolve_project_session
from hop.state import SessionState, load_sessions
from hop.targets import ResolvedUrlTarget, resolve_visible_output_target

SESSION_SOCKET_PREFIX = "unix:@hop-"

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
) -> ProjectSession | None:
    if not listen_on or not listen_on.startswith(SESSION_SOCKET_PREFIX):
        logger.info("listen_on=%r is not a hop session socket; selection=%r", listen_on, selection)
        return None
    session_name = listen_on.removeprefix(SESSION_SOCKET_PREFIX)

    state = sessions_loader().get(session_name)
    if state is None:
        logger.warning("no recorded session state for %r; selection=%r", session_name, selection)
        return None

    if source_cwd is None:
        logger.warning("source window has no cwd; selection=%r", selection)
        return None

    session = resolve_project_session(state.project_root)
    resolved_target = resolve_visible_output_target(
        selection,
        terminal_cwd=source_cwd,
        project_root=session.project_root,
    )
    if resolved_target is None:
        logger.info(
            "could not resolve %r against terminal_cwd=%s project_root=%s",
            selection,
            source_cwd,
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
