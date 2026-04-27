from __future__ import annotations

import os
from typing import Callable, Protocol

from hop.kitty import KITTY_LISTEN_ON_ENV_VAR, KittyWindowContext
from hop.session import ProjectSession, resolve_project_session
from hop.state import SessionState, load_sessions
from hop.targets import ResolvedUrlTarget, resolve_visible_output_target

SESSION_SOCKET_PREFIX = "unix:@hop-"


class OpenSelectionKittyAdapter(Protocol):
    def inspect_window(self, window_id: int) -> KittyWindowContext | None: ...


class OpenSelectionNeovimAdapter(Protocol):
    def open_target(self, session: ProjectSession, *, target: str) -> None: ...


class OpenSelectionBrowserAdapter(Protocol):
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None: ...


def open_selection_in_window(
    selection: str,
    *,
    source_window_id: int,
    kitty: OpenSelectionKittyAdapter,
    neovim: OpenSelectionNeovimAdapter,
    browser: OpenSelectionBrowserAdapter,
    sessions_loader: Callable[[], dict[str, SessionState]] = load_sessions,
    listen_on_env: str | None = None,
) -> ProjectSession | None:
    source_window = kitty.inspect_window(source_window_id)
    if source_window is None or source_window.cwd is None:
        return None

    listen_on = listen_on_env if listen_on_env is not None else os.environ.get(KITTY_LISTEN_ON_ENV_VAR, "")
    if not listen_on.startswith(SESSION_SOCKET_PREFIX):
        return None
    session_name = listen_on.removeprefix(SESSION_SOCKET_PREFIX)

    state = sessions_loader().get(session_name)
    if state is None:
        return None

    session = resolve_project_session(state.project_root)
    resolved_target = resolve_visible_output_target(
        selection,
        terminal_cwd=source_window.cwd,
        project_root=session.project_root,
    )
    if resolved_target is None:
        return None

    if isinstance(resolved_target, ResolvedUrlTarget):
        browser.ensure_browser(session, url=resolved_target.url)
    else:
        neovim.open_target(session, target=resolved_target.editor_target)

    return session
