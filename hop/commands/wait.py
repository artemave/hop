from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Protocol

from hop.cmd_events import read_events
from hop.commands.run import default_runs_dir
from hop.errors import HopError
from hop.kitty import KittyWindowState


class UnknownRunError(HopError):
    """Raised when hop wait is given a run id with no matching dispatch state."""


class WaitTimeoutError(HopError):
    """Raised when hop wait gives up waiting for the dispatched command to complete."""


WAIT_TIMEOUT_SECONDS = 600.0

WAIT_POLL_INTERVAL_SECONDS = 0.05


class WaitKittyAdapter(Protocol):
    def get_window_state(self, session_name: str, window_id: int) -> KittyWindowState: ...

    def get_last_cmd_output(self, session_name: str, window_id: int) -> str: ...


def wait_command(
    run_id: str,
    *,
    kitty: WaitKittyAdapter,
    runs_dir: Path | None = None,
    events_dir: Path | None = None,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, int]:
    timeout_seconds = WAIT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    poll_interval_seconds = WAIT_POLL_INTERVAL_SECONDS if poll_interval_seconds is None else poll_interval_seconds
    target_dir = runs_dir if runs_dir is not None else default_runs_dir()
    state_path = target_dir / f"{run_id}.json"
    try:
        state = json.loads(state_path.read_text())
    except FileNotFoundError as error:
        msg = f"Unknown hop run {run_id!r}; no dispatch state at {state_path}."
        raise UnknownRunError(msg) from error

    window_id = int(state["window_id"])
    session_name = str(state["session"])
    cursor = int(state["events_cursor"])

    start = clock()
    while True:
        events = read_events(window_id, since=cursor, base=events_dir)
        if any(not event.is_start for event in events):
            output = kitty.get_last_cmd_output(session_name, window_id)
            exit_status = kitty.get_window_state(session_name, window_id).last_cmd_exit_status
            return output, exit_status

        if (clock() - start) > timeout_seconds:
            msg = f"hop wait timed out after {timeout_seconds:.0f}s waiting for run {run_id!r}."
            raise WaitTimeoutError(msg)

        sleep(poll_interval_seconds)
