"""Subprocesses whose exit status survives running inside kitty.

The kitten dispatch and the hover-links watcher run inside the kitty process,
whose child monitor reaps every exited child with ``waitpid(-1)``. When it
reaps one of hop's subprocesses first, ``subprocess`` gets ``ECHILD`` and
reports exit status 0 — a failed backend command reads as a success. So the
command runs under a wrapper ``sh`` that writes ``$?`` into a pipe, and the
exit status is read from there instead of from ``waitpid``.
"""

from __future__ import annotations

import os
import subprocess
from typing import Sequence


def run_reporting_exit_status(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    input: str | None = None,
    stderr: int | None = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    read_fd, write_fd = os.pipe()
    # ``/dev/fd/N`` rather than ``>&N``: dash only redirects single-digit fds.
    script = f'"$@"; printf %s "$?" >/dev/fd/{write_fd}'
    try:
        result = subprocess.run(
            ["sh", "-c", script, "sh", *args],
            cwd=cwd,
            input=input,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            check=False,
            pass_fds=(write_fd,),
        )
    finally:
        os.close(write_fd)
    # Non-blocking: a background process the command left behind (e.g. an ssh
    # ControlMaster) inherits the pipe and would hold a blocking read open.
    os.set_blocking(read_fd, False)
    try:
        reported = os.read(read_fd, 16)
    except BlockingIOError:
        reported = b""
    finally:
        os.close(read_fd)
    # Nothing reported means the wrapper itself was killed; the wait status
    # (a negative signal number) is all there is.
    returncode = int(reported) if reported else result.returncode
    return subprocess.CompletedProcess(list(args), returncode, result.stdout, result.stderr)
