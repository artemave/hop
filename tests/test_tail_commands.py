import json
from pathlib import Path

import pytest
from hop.commands.tail import TailTimeoutError, UnknownRunError, tail_command
from hop.kitty import KittyWindowState


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class StubKittyAdapter:
    def __init__(self, *, states: list[KittyWindowState], output: str) -> None:
        self._states = states
        self._output = output
        self.state_calls = 0
        self.output_calls = 0

    def get_window_state(self, session_name: str, window_id: int) -> KittyWindowState:
        index = min(self.state_calls, len(self._states) - 1)
        self.state_calls += 1
        return self._states[index]

    def get_last_cmd_output(self, session_name: str, window_id: int) -> str:
        self.output_calls += 1
        return self._output


def write_state(runs_dir: Path, run_id: str, *, window_id: int = 42) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.json").write_text(
        json.dumps({"window_id": window_id, "session": "demo", "role": "test", "dispatched_at": 0.0})
    )


def test_tail_returns_output_after_command_finishes(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    write_state(runs_dir, "abc")

    kitty = StubKittyAdapter(
        states=[
            KittyWindowState(at_prompt=False, last_cmd_exit_status=0),
            KittyWindowState(at_prompt=False, last_cmd_exit_status=0),
            KittyWindowState(at_prompt=True, last_cmd_exit_status=0),
        ],
        output="hello\n",
    )

    output = tail_command(
        "abc",
        kitty=kitty,
        runs_dir=runs_dir,
        sleep=lambda _: None,
    )

    assert output == "hello\n"
    assert kitty.output_calls == 1


def test_tail_returns_output_for_command_too_fast_to_observe_running(tmp_path: Path) -> None:
    """If at_prompt stays True past fast_done_seconds, fetch output anyway."""
    runs_dir = tmp_path / "runs"
    write_state(runs_dir, "fast")

    kitty = StubKittyAdapter(
        states=[KittyWindowState(at_prompt=True, last_cmd_exit_status=0)],
        output="quick\n",
    )
    clock = FakeClock()

    def sleep_fn(dt: float) -> None:
        clock.advance(dt)

    output = tail_command(
        "fast",
        kitty=kitty,
        runs_dir=runs_dir,
        clock=clock,
        sleep=sleep_fn,
        fast_done_seconds=0.5,
        poll_interval_seconds=0.1,
    )

    assert output == "quick\n"


def test_tail_raises_for_unknown_run_id(tmp_path: Path) -> None:
    kitty = StubKittyAdapter(states=[KittyWindowState(at_prompt=True, last_cmd_exit_status=0)], output="")

    with pytest.raises(UnknownRunError):
        tail_command("nope", kitty=kitty, runs_dir=tmp_path / "runs")


def test_tail_times_out_when_command_never_returns(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    write_state(runs_dir, "stuck")

    kitty = StubKittyAdapter(
        states=[KittyWindowState(at_prompt=False, last_cmd_exit_status=0)],
        output="",
    )
    clock = FakeClock()

    def sleep_fn(dt: float) -> None:
        clock.advance(dt)

    with pytest.raises(TailTimeoutError):
        tail_command(
            "stuck",
            kitty=kitty,
            runs_dir=runs_dir,
            clock=clock,
            sleep=sleep_fn,
            timeout_seconds=1.0,
            poll_interval_seconds=0.1,
        )


def test_tail_does_not_exit_early_during_fast_done_window(tmp_path: Path) -> None:
    """Within fast_done_seconds, tail must wait for at_prompt=False before fetching."""
    runs_dir = tmp_path / "runs"
    write_state(runs_dir, "soon")

    kitty = StubKittyAdapter(
        states=[
            KittyWindowState(at_prompt=True, last_cmd_exit_status=0),
            KittyWindowState(at_prompt=False, last_cmd_exit_status=0),
            KittyWindowState(at_prompt=True, last_cmd_exit_status=0),
        ],
        output="ran\n",
    )
    clock = FakeClock()

    def sleep_fn(dt: float) -> None:
        clock.advance(dt)

    output = tail_command(
        "soon",
        kitty=kitty,
        runs_dir=runs_dir,
        clock=clock,
        sleep=sleep_fn,
        fast_done_seconds=10.0,
        poll_interval_seconds=0.1,
    )

    assert output == "ran\n"
    assert kitty.state_calls == 3
