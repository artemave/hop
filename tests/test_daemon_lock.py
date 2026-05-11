"""Tests for ``hop.daemon_lock`` — single-instance flock + status sidecar."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hop.daemon_lock import (
    HopdAlreadyRunning,
    acquire_lock,
    clear_status,
    read_status,
    signal_running_hopd_to_stop,
    status_path,
    write_status,
)


@pytest.fixture(autouse=True)
def isolate_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point XDG_RUNTIME_DIR at tmp_path so lock and status files don't
    collide with a real hopd or other test runs."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


def test_acquire_lock_succeeds_when_nothing_else_holds_it() -> None:
    fd = acquire_lock()
    try:
        # If we got here, the lock was acquired. Sanity: the lock file
        # exists on disk.
        assert os.path.exists(os.environ["XDG_RUNTIME_DIR"] + "/hop/hopd.lock")
    finally:
        os.close(fd)


def test_acquire_lock_raises_when_another_process_holds_it() -> None:
    """Take the lock from a real subprocess (so the OS sees a distinct
    holder), then try to acquire it from the test process. The second
    attempt must raise ``HopdAlreadyRunning``."""
    runtime = os.environ["XDG_RUNTIME_DIR"]
    holder = subprocess.Popen(
        [
            "python3",
            "-c",
            (
                "import fcntl, os, sys, time, pathlib;"
                "pathlib.Path(sys.argv[1]).mkdir(parents=True, exist_ok=True);"
                "fd = os.open(sys.argv[1] + '/hopd.lock', os.O_CREAT | os.O_RDWR);"
                "fcntl.flock(fd, fcntl.LOCK_EX);"
                "print('locked', flush=True);"
                "time.sleep(5)"
            ),
            runtime + "/hop",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait until the child has the lock before we probe.
        assert holder.stdout is not None
        line = holder.stdout.readline().strip()
        assert line == "locked"

        with pytest.raises(HopdAlreadyRunning):
            acquire_lock()
    finally:
        holder.terminate()
        holder.wait(timeout=2)


def test_acquire_lock_reports_holder_pid_when_status_file_present() -> None:
    """When the status file is intact, the raised exception carries the
    holder's pid so the CLI can surface a useful error."""
    write_status(pid=99999, version="9.9.9")

    runtime = os.environ["XDG_RUNTIME_DIR"]
    holder = subprocess.Popen(
        [
            "python3",
            "-c",
            (
                "import fcntl, os, sys, time, pathlib;"
                "pathlib.Path(sys.argv[1]).mkdir(parents=True, exist_ok=True);"
                "fd = os.open(sys.argv[1] + '/hopd.lock', os.O_CREAT | os.O_RDWR);"
                "fcntl.flock(fd, fcntl.LOCK_EX);"
                "print('locked', flush=True);"
                "time.sleep(5)"
            ),
            runtime + "/hop",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"

        with pytest.raises(HopdAlreadyRunning) as info:
            acquire_lock()
        assert info.value.holder_pid == 99999
    finally:
        holder.terminate()
        holder.wait(timeout=2)


def test_write_and_read_status_roundtrips() -> None:
    write_status(pid=12345, version="0.42.0")
    status = read_status()
    assert status is not None
    assert status.pid == 12345
    assert status.version == "0.42.0"


def test_read_status_returns_none_when_file_missing() -> None:
    assert read_status() is None


def test_read_status_returns_none_when_file_is_malformed() -> None:
    runtime_path = status_path()
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text("{ not valid json")
    assert read_status() is None


def test_read_status_returns_none_when_fields_have_wrong_types() -> None:
    runtime_path = status_path()
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(json.dumps({"pid": "not-an-int", "version": "0.1.0"}))
    assert read_status() is None


def test_clear_status_is_idempotent() -> None:
    # No file yet — clear_status shouldn't raise.
    clear_status()
    # Now write and clear; file should be gone.
    write_status(pid=1, version="0.0.0")
    clear_status()
    assert read_status() is None


def test_signal_running_hopd_to_stop_returns_false_when_no_status_file() -> None:
    assert signal_running_hopd_to_stop() is False


def test_signal_running_hopd_to_stop_returns_false_when_recorded_pid_is_dead() -> None:
    """The status file might survive a SIGKILL'd hopd. The signal helper
    must notice the pid is gone and return False rather than reporting
    "still running"."""
    # Use a pid that is virtually certain not to exist. Linux pids are
    # 32-bit; 2**31 - 1 is well above typical pid_max but valid as an
    # input to os.kill (which then ESRCHes).
    write_status(pid=2**31 - 1, version="0.0.0")
    assert signal_running_hopd_to_stop() is False


def test_signal_running_hopd_to_stop_signals_then_waits_for_release() -> None:
    """Real-process integration: start a child that holds the lock, then
    call signal_running_hopd_to_stop. The child must receive SIGTERM and
    exit; the helper must return True after the lock is free."""
    runtime = os.environ["XDG_RUNTIME_DIR"]
    holder = subprocess.Popen(
        [
            "python3",
            "-c",
            (
                "import fcntl, os, sys, time, pathlib;"
                "pathlib.Path(sys.argv[1]).mkdir(parents=True, exist_ok=True);"
                "fd = os.open(sys.argv[1] + '/hopd.lock', os.O_CREAT | os.O_RDWR);"
                "fcntl.flock(fd, fcntl.LOCK_EX);"
                "print('locked', flush=True);"
                "time.sleep(30)"
            ),
            runtime + "/hop",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"

        write_status(pid=holder.pid, version="0.0.0")

        assert signal_running_hopd_to_stop() is True
        # Child should have terminated by now.
        assert holder.wait(timeout=2) is not None
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=2)


def test_signal_running_hopd_to_stop_times_out_when_holder_ignores_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the existing hopd refuses to exit on SIGTERM (e.g. caught the
    signal and didn't unwind), the helper must raise rather than hanging
    forever."""

    runtime = os.environ["XDG_RUNTIME_DIR"]
    Path(runtime + "/hop").mkdir(parents=True, exist_ok=True)
    # Open the lock fd in *this* process so it stays held throughout the
    # test — but record the pid as someone else (our parent) so the
    # signal goes elsewhere and we control the timeout.
    import fcntl

    fd = os.open(runtime + "/hop/hopd.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        write_status(pid=os.getpid(), version="0.0.0")

        # Suppress real signal delivery (we don't actually want to
        # terminate ourselves) — replace os.kill with a no-op.
        def noop_kill(_pid: int, _sig: int) -> None:
            return None

        monkeypatch.setattr(os, "kill", noop_kill)

        # Stub sleep/clock to drive the timeout quickly.
        sleeps: list[float] = []
        now = [0.0]

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        def fake_clock() -> float:
            return now[0]

        with pytest.raises(HopdAlreadyRunning):
            signal_running_hopd_to_stop(sleep=fake_sleep, clock=fake_clock)

        # The wait loop should have slept multiple times before giving up.
        assert len(sleeps) >= 2
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_lock_releases_on_fd_close() -> None:
    """The flock is held for the lifetime of the fd. Closing it must
    release the lock so a subsequent acquire_lock works."""
    fd = acquire_lock()
    os.close(fd)
    # No HopdAlreadyRunning expected.
    fd2 = acquire_lock()
    os.close(fd2)


def test_installed_version_falls_back_when_metadata_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hop`` running from a checkout that isn't installed (rare edge,
    mostly during package metadata transitions) reports a literal
    ``"unknown"`` so callers don't crash on missing metadata."""
    import importlib.metadata

    from hop.daemon_lock import installed_version

    def raise_not_found(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("hop")

    monkeypatch.setattr(importlib.metadata, "version", raise_not_found)
    assert installed_version() == "unknown"


def test_acquire_lock_reraises_unexpected_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """``flock`` failures other than EWOULDBLOCK/EAGAIN aren't "already
    running" — they're real errors (EIO, ENOLCK, etc). Surface them
    instead of pretending another hopd is up."""
    import fcntl

    def fail_flock(_fd: int, _op: int) -> None:
        raise OSError(5, "I/O error")  # EIO

    monkeypatch.setattr(fcntl, "flock", fail_flock)

    with pytest.raises(OSError, match="I/O error"):
        acquire_lock()


def test_signal_running_hopd_to_stop_raises_when_holder_owned_by_another_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the recorded pid is owned by a different user, ``os.kill`` raises
    PermissionError. We can't terminate it; surface that as
    HopdAlreadyRunning so the CLI can hint at the situation."""
    write_status(pid=99999, version="0.0.0")

    def fail_kill(_pid: int, _sig: int) -> None:
        raise PermissionError("not your process")

    monkeypatch.setattr(os, "kill", fail_kill)

    with pytest.raises(HopdAlreadyRunning):
        signal_running_hopd_to_stop()
