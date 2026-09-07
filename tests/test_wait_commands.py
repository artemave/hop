import json
from pathlib import Path

import pytest

from hop.cmd_events import append_event
from hop.commands.wait import UnknownRunError, WaitTimeoutError, wait_command
from hop.kitty import KittyWindowState


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class StubKittyAdapter:
    def __init__(self, *, exit_status: int = 0, output: str = "") -> None:
        self._exit_status = exit_status
        self._output = output
        self.state_calls = 0
        self.output_calls = 0

    def get_window_state(self, session_name: str, window_id: int) -> KittyWindowState:
        self.state_calls += 1
        return KittyWindowState(last_cmd_exit_status=self._exit_status)

    def get_last_cmd_output(self, session_name: str, window_id: int) -> str:
        self.output_calls += 1
        return self._output


def write_state(runs_dir: Path, run_id: str, *, window_id: int = 42, events_cursor: int = 0) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.json").write_text(
        json.dumps(
            {
                "window_id": window_id,
                "session": "demo",
                "role": "test",
                "dispatched_at": 0.0,
                "events_cursor": events_cursor,
            }
        )
    )


def test_wait_returns_output_and_exit_status_after_stop_event(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    events_dir = tmp_path / "events"
    write_state(runs_dir, "abc")
    append_event(42, is_start=True, cmdline="pytest", at=1.0, base=events_dir)
    append_event(42, is_start=False, cmdline="pytest", at=2.0, base=events_dir)

    kitty = StubKittyAdapter(exit_status=3, output="boom\n")

    output, exit_status = wait_command(
        "abc",
        kitty=kitty,
        runs_dir=runs_dir,
        events_dir=events_dir,
        sleep=lambda _: None,
    )

    assert (output, exit_status) == ("boom\n", 3)
    assert kitty.output_calls == 1
    assert kitty.state_calls == 1


def test_wait_does_not_complete_on_a_stop_event_before_the_dispatch_cursor(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    events_dir = tmp_path / "events"
    write_state(runs_dir, "abc", events_cursor=2)
    # A previous run's start/stop (indices 0-1). This dispatch has started
    # (index 2) but not finished — the only stop event is the stale one, which
    # the cursor must exclude, so wait keeps blocking until it times out.
    append_event(42, is_start=True, cmdline="old", at=1.0, base=events_dir)
    append_event(42, is_start=False, cmdline="old", at=2.0, base=events_dir)
    append_event(42, is_start=True, cmdline="new", at=3.0, base=events_dir)

    clock = FakeClock()

    with pytest.raises(WaitTimeoutError):
        wait_command(
            "abc",
            kitty=StubKittyAdapter(),
            runs_dir=runs_dir,
            events_dir=events_dir,
            clock=clock,
            sleep=lambda dt: clock.advance(dt),
            timeout_seconds=1.0,
            poll_interval_seconds=0.1,
        )


def test_wait_raises_for_unknown_run_id(tmp_path: Path) -> None:
    with pytest.raises(UnknownRunError):
        wait_command("nope", kitty=StubKittyAdapter(), runs_dir=tmp_path / "runs", events_dir=tmp_path / "events")


def test_wait_times_out_when_command_never_returns(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    events_dir = tmp_path / "events"
    write_state(runs_dir, "stuck")
    append_event(42, is_start=True, cmdline="hang", at=1.0, base=events_dir)

    clock = FakeClock()

    def sleep_fn(dt: float) -> None:
        clock.advance(dt)

    with pytest.raises(WaitTimeoutError):
        wait_command(
            "stuck",
            kitty=StubKittyAdapter(),
            runs_dir=runs_dir,
            events_dir=events_dir,
            clock=clock,
            sleep=sleep_fn,
            timeout_seconds=1.0,
            poll_interval_seconds=0.1,
        )
