from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, TextIO

from hop.commands.run import default_runs_dir
from hop.errors import HopError
from hop.kitty import KittyWindowState


class UnknownRunError(HopError):
    """Raised when hop tail is given a run id with no matching dispatch state."""


class TailTimeoutError(HopError):
    """Raised when hop tail gives up waiting for the dispatched command to complete."""


@dataclass(frozen=True, slots=True)
class TailResult:
    """The captured output of a dispatched command plus its exit status.

    ``exit_status`` comes from Kitty's OSC 133 ``D`` marker for the last
    command in the role window. Standalone ``hop tail`` writes only
    ``output`` and exits 0; ``hop run --wait`` propagates ``exit_status``.
    """

    output: str
    exit_status: int


class TailKittyAdapter(Protocol):
    def get_window_state(self, session_name: str, window_id: int) -> KittyWindowState: ...

    def get_last_cmd_output(self, session_name: str, window_id: int) -> str: ...


def tail_command(
    run_id: str,
    *,
    kitty: TailKittyAdapter,
    runs_dir: Path | None = None,
    timeout_seconds: float = 600.0,
    fast_done_seconds: float = 0.5,
    poll_interval_seconds: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TailResult:
    target_dir = runs_dir if runs_dir is not None else default_runs_dir()
    state_path = target_dir / f"{run_id}.json"
    try:
        state = json.loads(state_path.read_text())
    except FileNotFoundError as error:
        msg = f"Unknown hop run {run_id!r}; no dispatch state at {state_path}."
        raise UnknownRunError(msg) from error

    window_id = int(state["window_id"])
    session_name = str(state["session"])

    started_running = False
    start = clock()
    while True:
        ws = kitty.get_window_state(session_name, window_id)
        if not ws.at_prompt:
            started_running = True
        elif started_running or (clock() - start) > fast_done_seconds:
            return TailResult(
                output=kitty.get_last_cmd_output(session_name, window_id),
                exit_status=ws.last_cmd_exit_status,
            )

        if (clock() - start) > timeout_seconds:
            msg = f"hop tail timed out after {timeout_seconds:.0f}s waiting for run {run_id!r}."
            raise TailTimeoutError(msg)

        sleep(poll_interval_seconds)


def wait_for_dispatch(
    *,
    run_id: str,
    session_name: str,
    window_id: int,
    role: str,
    kitty: TailKittyAdapter,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    runs_dir: Path | None = None,
    timeout_seconds: float = 600.0,
    fast_done_seconds: float = 0.5,
    poll_interval_seconds: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Block on ``tail_command`` for a dispatched run, stream its output, and
    return the command's exit status. On timeout: write whatever output the
    role window has so far, warn on stderr that the command is still running,
    and return ``124`` (the ``timeout(1)`` convention). This is the body of
    ``hop run --wait``.
    """

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        result = tail_command(
            run_id,
            kitty=kitty,
            runs_dir=runs_dir,
            timeout_seconds=timeout_seconds,
            fast_done_seconds=fast_done_seconds,
            poll_interval_seconds=poll_interval_seconds,
            clock=clock,
            sleep=sleep,
        )
    except TailTimeoutError:
        out.write(kitty.get_last_cmd_output(session_name, window_id))
        err.write(
            f"hop run --wait: timed out waiting for the {role!r} window to return to its "
            "prompt; the command is still running there\n"
        )
        return 124
    out.write(result.output)
    return result.exit_status
