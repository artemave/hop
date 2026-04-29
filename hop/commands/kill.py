from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, Sequence

from hop.backends import HostBackend, SessionBackend
from hop.browser import DEFAULT_BROWSER_MARK_PREFIX
from hop.editor import EDITOR_MARK_PREFIX
from hop.session import ProjectSession, resolve_project_session
from hop.state import forget_session
from hop.sway import SwayWindow


class KillSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def close_window(self, window_id: int) -> None: ...


def kill_session(
    cwd: Path | str,
    *,
    sway: KillSwayAdapter,
    session_backend_for: Callable[[ProjectSession], SessionBackend] = lambda _session: HostBackend(),
    forget: Callable[[str], None] = forget_session,
) -> ProjectSession:
    session = resolve_project_session(cwd)

    browser_mark = f"{DEFAULT_BROWSER_MARK_PREFIX}{session.session_name}"
    editor_mark = f"{EDITOR_MARK_PREFIX}{session.session_name}"

    # Close every window on the session workspace plus any session-marked
    # window that's drifted off it (browser via `hop browser`, editor when
    # its kitty was launched outside the session). Sway-driven closing means
    # we don't care which kitty instance owns each window — every process
    # gets SIGHUP from the window-close.
    for window in sway.list_windows():
        if (
            window.workspace_name == session.workspace_name
            or browser_mark in window.marks
            or editor_mark in window.marks
        ):
            sway.close_window(window.id)

    # Teardown after windows close so in-container shells exit via SIGHUP from
    # the kitty close, rather than being killed abruptly by e.g. `compose down`.
    session_backend_for(session).teardown(session)

    forget(session.session_name)

    return session
