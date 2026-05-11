from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Protocol, Sequence

from hop.backends import CommandBackend, SessionBackend
from hop.browser import DEFAULT_BROWSER_MARK_PREFIX
from hop.editor import EDITOR_MARK_PREFIX
from hop.session import ProjectSession, resolve_project_session
from hop.state import forget_session
from hop.sway import SwayWindow

WINDOW_CLOSE_TIMEOUT_SECONDS = 5.0
WINDOW_CLOSE_POLL_INTERVAL_SECONDS = 0.05

_BUILTIN_HOST_BACKEND = CommandBackend(name="host", interactive_prefix="", noninteractive_prefix="")


class KillSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def close_window(self, window_id: int) -> None: ...


def kill_session(
    cwd: Path | str,
    *,
    sway: KillSwayAdapter,
    session_backend_for: Callable[[ProjectSession], SessionBackend] = lambda _session: _BUILTIN_HOST_BACKEND,
    forget: Callable[[str], None] = forget_session,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ProjectSession:
    session = resolve_project_session(cwd)

    # Capture the backend reference now, before windows close. The daemon
    # (hopd) sweeps stale persisted state on every workspace event; closing
    # the session's last window destroys its workspace, fires that event,
    # and races our window-close wait loop. By the time we'd otherwise look
    # up the backend, the state file could already be gone — for_session
    # would fall back to the built-in host backend whose teardown is a no-op,
    # silently skipping `compose down` (or whatever the user configured).
    backend = session_backend_for(session)

    browser_mark = f"{DEFAULT_BROWSER_MARK_PREFIX}{session.session_name}"
    editor_mark = f"{EDITOR_MARK_PREFIX}{session.session_name}"

    def belongs_to_session(window: SwayWindow) -> bool:
        return (
            window.workspace_name == session.workspace_name
            or browser_mark in window.marks
            or editor_mark in window.marks
        )

    # Close every window on the session workspace plus any session-marked
    # window that's drifted off it (browser via `hop browser`, editor when
    # its kitty was launched outside the session). Sway-driven closing means
    # we don't care which kitty instance owns each window — every process
    # gets SIGHUP from the window-close.
    closed_ids: set[int] = set()
    for window in sway.list_windows():
        if belongs_to_session(window):
            sway.close_window(window.id)
            closed_ids.add(window.id)

    # Wait for sway to actually destroy the windows before tearing down the
    # backend. close_window is async — by the time it returns, the kitty
    # processes (and any shells they wrap, e.g. `podman-compose exec`) may
    # still be alive. Running teardown while exec sessions are attached can
    # leave a container in an in-between state where the next `prepare`
    # refuses to start it.
    deadline = clock() + WINDOW_CLOSE_TIMEOUT_SECONDS
    while closed_ids and clock() < deadline:
        live_ids = {window.id for window in sway.list_windows()}
        closed_ids &= live_ids
        if not closed_ids:
            break
        sleep(WINDOW_CLOSE_POLL_INTERVAL_SECONDS)

    backend.teardown(session)

    forget(session.session_name)

    return session
