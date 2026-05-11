"""Single-instance enforcement + version stamp for ``hopd``.

A second ``hopd`` started concurrently would race the script-set rewriter
against the first, double-subscribe to sway events, and generally make a
mess. The lock here ensures exactly one daemon at a time, and the
sidecar status file lets the ``hop`` CLI notice when the running daemon
is an old version after the user upgrades the package.

Mechanism: an exclusive flock on ``${XDG_RUNTIME_DIR}/hop/hopd.lock``.
The fd is held for the lifetime of the hopd process; kernel releases the
flock when the fd is closed or the process exits (clean or crash), so no
stale-lock surgery is needed.

The status file at ``${XDG_RUNTIME_DIR}/hop/hopd.status`` is a small JSON
document ``{"pid": int, "version": str}`` written right after the lock
is acquired. ``hopd --restart`` reads it to find which process to SIGTERM;
the CLI reads it to detect a version mismatch.
"""

from __future__ import annotations

import errno
import fcntl
import importlib.metadata
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from tempfile import gettempdir
from typing import Callable

LOCK_FILENAME = "hopd.lock"
STATUS_FILENAME = "hopd.status"
RESTART_WAIT_TIMEOUT_SECONDS = 2.0
RESTART_WAIT_POLL_INTERVAL_SECONDS = 0.05


class HopdAlreadyRunning(Exception):
    """Raised when ``acquire_lock`` cannot take the lock because another
    ``hopd`` already holds it. Carries the holder's PID (or ``None`` if
    the status file is unreadable)."""

    def __init__(self, holder_pid: int | None) -> None:
        self.holder_pid = holder_pid
        msg = (
            f"another hopd is already running (pid {holder_pid})"
            if holder_pid is not None
            else "another hopd is already running"
        )
        super().__init__(msg)


@dataclass(frozen=True, slots=True)
class HopdStatus:
    pid: int
    version: str


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or gettempdir()
    return Path(base).expanduser() / "hop"


def lock_path() -> Path:
    return runtime_dir() / LOCK_FILENAME


def status_path() -> Path:
    return runtime_dir() / STATUS_FILENAME


def installed_version() -> str:
    """Version string for the currently-installed ``hop`` package.

    Falls back to ``"unknown"`` when running from a source tree that
    isn't pip-installed (rare; mostly during development against an
    editable install before metadata refreshes)."""
    try:
        return importlib.metadata.version("hop")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def read_status() -> HopdStatus | None:
    """Read the current hopd status file, or ``None`` if not present /
    unreadable / malformed. Never raises — callers treat ``None`` as
    "no daemon known to be running"."""
    try:
        payload = json.loads(status_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    pid = payload.get("pid")
    version = payload.get("version")
    if not isinstance(pid, int) or not isinstance(version, str):
        return None
    return HopdStatus(pid=pid, version=version)


def acquire_lock() -> int:
    """Take the hopd flock. Returns the file descriptor; caller keeps it
    alive for the daemon's lifetime (the kernel auto-releases on close /
    process exit).

    Raises ``HopdAlreadyRunning`` if another process holds the lock.
    """
    dir_path = runtime_dir()
    dir_path.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            existing = read_status()
            holder = existing.pid if existing is not None else None
            raise HopdAlreadyRunning(holder) from exc
        raise
    return fd


def write_status(*, pid: int, version: str) -> None:
    """Atomically write the status file. Called right after the lock is
    acquired so ``read_status`` returns the new process's identity to any
    concurrent reader."""
    runtime_dir().mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": pid, "version": version})
    target = status_path()
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(str(tmp), str(target))


def clear_status() -> None:
    """Best-effort removal of the status file. Called when hopd exits
    cleanly so a stale ``status`` file doesn't claim the daemon is still
    running. The lock fd handles the actual mutex; this is purely a
    cosmetic cleanup for ``read_status``."""
    try:
        status_path().unlink()
    except FileNotFoundError:
        pass


def signal_running_hopd_to_stop(
    *,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> bool:
    """Send SIGTERM to the running hopd (if any) and wait for it to
    release the lock.

    Returns ``True`` when a previous hopd was signaled and exited within
    the wait window, ``False`` when none was running. Raises
    ``HopdAlreadyRunning`` if the holder is still alive after the wait —
    caller decides whether to retry or surface the error.
    """
    import time

    sleep_fn = sleep or time.sleep
    clock_fn = clock or time.monotonic

    existing = read_status()
    if existing is None:
        return False
    try:
        os.kill(existing.pid, signal.SIGTERM)
    except ProcessLookupError:
        # The recorded pid is dead but didn't clean up its status file
        # (kill -9, crash). The lock is already released; nothing to do.
        return False
    except PermissionError:
        # Different user owns the pid — extraordinarily rare, but bail
        # rather than silently fail.
        raise HopdAlreadyRunning(existing.pid) from None

    deadline = clock_fn() + RESTART_WAIT_TIMEOUT_SECONDS
    while clock_fn() < deadline:
        if _is_lock_free():
            return True
        sleep_fn(RESTART_WAIT_POLL_INTERVAL_SECONDS)
    raise HopdAlreadyRunning(existing.pid)


def _is_lock_free() -> bool:
    """Probe whether the lock can currently be acquired. Used by the
    restart wait loop. Releases the probe lock immediately so the caller
    can race for the real one."""
    fd = os.open(str(lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True
