"""``hopd`` — long-lived Sway IPC subscriber that maintains the vicinae script set.

Wired into the user's sway config via ``exec hopd`` (not ``exec_always`` —
``exec`` runs once at sway startup, and the IPC subscription survives
reloads, so a single instance covers the whole sway session).
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path
from typing import Callable, Sequence

from hop import debug
from hop.app import SessionBackendRegistry
from hop.commands.session import SESSION_WORKSPACE_PREFIX, SessionListing, list_sessions
from hop.config import load_global_config
from hop.daemon_lock import (
    HopdAlreadyRunning,
    acquire_lock,
    clear_status,
    installed_version,
    signal_running_hopd_to_stop,
    write_status,
)
from hop.errors import HopError
from hop.state import SessionState, forget_session, load_sessions
from hop.sway import SwayIpcAdapter
from hop.vicinae import default_scripts_dir, regenerate, write_daemon_down_script


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hopd")
    parser.add_argument(
        "--restart",
        action="store_true",
        help=(
            "SIGTERM any running hopd and wait for it to exit before starting a new "
            "instance. Use after upgrading the hop package so the daemon picks up the "
            "new code."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    # Resolve scripts_dir up-front so the load-config error path can still
    # surface a "daemon stopped" entry — that's the most common crash cause
    # right after a config edit.
    scripts_dir = default_scripts_dir()

    if args.restart:
        try:
            signaled = signal_running_hopd_to_stop()
        except HopdAlreadyRunning as error:
            debug.log(f"hopd: --restart could not stop existing daemon: {error}")
            print(f"hopd: --restart could not stop existing daemon: {error}", file=sys.stderr)
            return 1
        if signaled:
            debug.log("hopd: --restart stopped previous daemon")

    try:
        lock_fd = acquire_lock()
    except HopdAlreadyRunning as error:
        debug.log(f"hopd: {error}; refusing to start a second instance")
        print(f"hopd: {error}; pass --restart to replace it", file=sys.stderr)
        return 1

    write_status(pid=os.getpid(), version=installed_version())

    try:
        return _run_main_loop(scripts_dir)
    finally:
        # Best-effort cleanup. The lock itself releases when the fd
        # closes (or the process exits), but the status file would
        # otherwise outlive us and falsely claim the daemon is alive.
        clear_status()
        os.close(lock_fd)


def _run_main_loop(scripts_dir: Path) -> int:
    try:
        debug.configure(load_global_config().debug_log)
    except HopError as error:
        # Bad global config aborts hopd startup; surface to stderr (which sway
        # may or may not be capturing) and to the debug log if a previous
        # configure() call had succeeded.
        debug.log(f"hopd: failed to load config: {error}")
        print(f"hopd: failed to load config: {error}", file=sys.stderr)
        _signal_daemon_down(scripts_dir, error)
        return 1

    debug.log("hopd: starting")
    sway = SwayIpcAdapter()
    registry = SessionBackendRegistry()

    def sessions_loader() -> Sequence[SessionListing]:
        return list_sessions(sway=sway)

    windows_for = registry.resolve_windows_for_entry

    try:
        sweep_stale_persisted_sessions(sway=sway)
        regenerate(
            sway=sway,
            sessions_loader=sessions_loader,
            scripts_dir=scripts_dir,
            windows_for=windows_for,
        )
        debug.log("hopd: subscribed to workspace events")
        for _event in sway.subscribe_to_workspace_events():
            sweep_stale_persisted_sessions(sway=sway)
            regenerate(
                sway=sway,
                sessions_loader=sessions_loader,
                scripts_dir=scripts_dir,
                windows_for=windows_for,
            )
    except HopError as error:
        debug.log(f"hopd: {error}")
        print(str(error), file=sys.stderr)
        _signal_daemon_down(scripts_dir, error)
        return 1
    except Exception as error:
        # Unhandled exceptions otherwise vanish into sway's stderr (often
        # /dev/null), leaving the daemon dead with no trace. Mirror the
        # traceback to the debug log when configured so the next `tail
        # $XDG_RUNTIME_DIR/hop/debug.log` shows what happened.
        debug.log(f"hopd: unhandled exception\n{traceback.format_exc()}")
        traceback.print_exc()
        _signal_daemon_down(scripts_dir, error)
        return 1

    debug.log("hopd: Sway IPC subscription ended")
    print("hopd: Sway IPC subscription ended", file=sys.stderr)
    _signal_daemon_down(scripts_dir, RuntimeError("Sway IPC subscription ended"))
    return 1


def _signal_daemon_down(scripts_dir: Path, error: BaseException) -> None:
    """Best-effort: surface the crash through the vicinae script set.

    Replaces every ``hop-*`` entry with a single "daemon stopped — restart"
    entry. Any failure in the rewrite (missing dir, permission error, disk
    full) is logged but suppressed — the outer exception is the real signal
    and we don't want to mask it by raising on top.
    """

    try:
        write_daemon_down_script(scripts_dir, error=error)
    except OSError as write_err:
        debug.log(f"hopd: failed to write daemon-down entry: {write_err}")


def sweep_stale_persisted_sessions(
    *,
    sway: SwayIpcAdapter,
    sessions_loader: Callable[[], dict[str, SessionState]] = load_sessions,
    forget: Callable[[str], None] = forget_session,
) -> None:
    """Drop persisted state files whose `p:<name>` workspace is no longer alive.

    The CLI's first-entry gate keys on kitty socket liveness, so stale state
    is a tidiness concern rather than a correctness bug. Run on every
    workspace event so that a session's state file disappears within one
    event of the workspace being destroyed.
    """
    live_workspaces = set(sway.list_session_workspaces(prefix=SESSION_WORKSPACE_PREFIX))
    for name in sessions_loader():
        workspace = f"{SESSION_WORKSPACE_PREFIX}{name}"
        if workspace not in live_workspaces:
            forget(name)
