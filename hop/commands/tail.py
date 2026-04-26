from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Protocol

from hop.commands.run import default_runs_dir
from hop.errors import HopError
from hop.kitty import KittyWindowState


class UnknownRunError(HopError):
    """Raised when hop tail is given a run id with no matching dispatch state."""


class TailTimeoutError(HopError):
    """Raised when hop tail gives up waiting for the dispatched command to complete."""


class TailKittyAdapter(Protocol):
    def get_window_state(self, window_id: int) -> KittyWindowState: ...

    def get_last_cmd_output(self, window_id: int) -> str: ...


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
) -> str:
    target_dir = runs_dir if runs_dir is not None else default_runs_dir()
    state_path = target_dir / f"{run_id}.json"
    try:
        state = json.loads(state_path.read_text())
    except FileNotFoundError as error:
        msg = f"Unknown hop run {run_id!r}; no dispatch state at {state_path}."
        raise UnknownRunError(msg) from error

    window_id = int(state["window_id"])

    started_running = False
    start = clock()
    while True:
        ws = kitty.get_window_state(window_id)
        if not ws.at_prompt:
            started_running = True
        elif started_running or (clock() - start) > fast_done_seconds:
            return kitty.get_last_cmd_output(window_id)

        if (clock() - start) > timeout_seconds:
            msg = f"hop tail timed out after {timeout_seconds:.0f}s waiting for run {run_id!r}."
            raise TailTimeoutError(msg)

        sleep(poll_interval_seconds)
