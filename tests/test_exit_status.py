from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from hop.exit_status import run_reporting_exit_status


def test_reports_exit_status_output_and_stdin(tmp_path: Path) -> None:
    result = run_reporting_exit_status(
        ["sh", "-c", 'cat; pwd; echo oops >&2; exit "$1"', "sh", "7"],
        cwd=str(tmp_path),
        input="piped\n",
    )

    assert result.args == ["sh", "-c", 'cat; pwd; echo oops >&2; exit "$1"', "sh", "7"]
    assert (result.returncode, result.stdout, result.stderr) == (7, f"piped\n{tmp_path}\n", "oops\n")


@pytest.fixture
def kitty_like_reaper() -> Iterator[list[int]]:
    """Reaps every exited child of this process the way kitty's child monitor
    does (``waitpid(-1)``), racing ``subprocess`` for each exit status."""

    reaped: list[int] = []
    stop = threading.Event()

    def reap() -> None:
        while not stop.is_set():
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                pid = 0
            if pid:
                reaped.append(pid)
            else:
                time.sleep(0.0005)

    thread = threading.Thread(target=reap)
    thread.start()
    yield reaped
    stop.set()
    thread.join()


# The background sleep holds stdout open after the command exits, so
# ``subprocess`` is still reading output while the reaper collects the exit.
EXITS_BEFORE_STDOUT_CLOSES = ["sh", "-c", "sleep 0.05 & exit 42"]


def test_exit_status_survives_another_thread_reaping_the_child(kitty_like_reaper: list[int]) -> None:
    unwrapped = subprocess.run(EXITS_BEFORE_STDOUT_CLOSES, stdout=subprocess.PIPE, check=False)
    assert unwrapped.returncode == 0

    assert run_reporting_exit_status(EXITS_BEFORE_STDOUT_CLOSES).returncode == 42
    assert len(kitty_like_reaper) == 2


def test_works_when_the_pipe_lands_on_a_multi_digit_fd() -> None:
    held = [os.open(os.devnull, os.O_RDONLY) for _ in range(12)]
    try:
        assert run_reporting_exit_status(["sh", "-c", "exit 5"]).returncode == 5
    finally:
        for fd in held:
            os.close(fd)


def test_a_background_process_holding_the_pipe_does_not_block() -> None:
    started = time.monotonic()

    result = run_reporting_exit_status(["sh", "-c", "sleep 5 >/dev/null 2>&1 & exit 3"])

    assert result.returncode == 3
    assert time.monotonic() - started < 2


def test_falls_back_to_the_wait_status_when_the_wrapper_is_killed() -> None:
    # The background sleep keeps the pipe open, so the read finds it empty
    # rather than at end-of-file.
    result = run_reporting_exit_status(
        ["sh", "-c", "sleep 1 >/dev/null 2>&1 & kill -9 $PPID"], stderr=subprocess.DEVNULL
    )

    assert result.returncode == -9
