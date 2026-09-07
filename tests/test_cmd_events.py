import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import hop
from hop.cmd_events import (
    CmdEvent,
    append_event,
    default_events_dir,
    discard_events,
    event_count,
    events_file,
    read_events,
)


def _load_watcher():
    path = Path(hop.__file__).parent / "kitten" / "cmd_events" / "main.py"
    spec = importlib.util.spec_from_file_location("hop_cmd_events_watcher", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watcher_writes_events_that_read_events_parses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOP_CMD_EVENTS_DIR", str(tmp_path))
    watcher = _load_watcher()
    window = SimpleNamespace(id=12)

    watcher.on_cmd_startstop(None, window, {"is_start": True, "cmdline": "pytest -q", "time": 1.0})
    watcher.on_cmd_startstop(None, window, {"is_start": False, "cmdline": "pytest -q", "time": 4.0})

    assert read_events(12, since=0) == [
        CmdEvent(is_start=True, cmdline="pytest -q", time=1.0),
        CmdEvent(is_start=False, cmdline="pytest -q", time=4.0),
    ]

    watcher.on_close(None, window, {})
    assert not events_file(12).exists()


def test_append_then_read_round_trips_events(tmp_path: Path) -> None:
    append_event(7, is_start=True, cmdline="pytest -q", at=1.0, base=tmp_path)
    append_event(7, is_start=False, cmdline="pytest -q", at=2.5, base=tmp_path)

    assert read_events(7, since=0, base=tmp_path) == [
        CmdEvent(is_start=True, cmdline="pytest -q", time=1.0),
        CmdEvent(is_start=False, cmdline="pytest -q", time=2.5),
    ]


def test_read_events_skips_everything_before_the_cursor(tmp_path: Path) -> None:
    append_event(1, is_start=True, cmdline="old", at=1.0, base=tmp_path)
    append_event(1, is_start=False, cmdline="old", at=2.0, base=tmp_path)
    append_event(1, is_start=True, cmdline="new", at=3.0, base=tmp_path)

    assert read_events(1, since=2, base=tmp_path) == [CmdEvent(is_start=True, cmdline="new", time=3.0)]


def test_read_events_for_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_events(99, since=0, base=tmp_path) == []


def test_event_count_counts_lines_and_zero_when_missing(tmp_path: Path) -> None:
    assert event_count(3, base=tmp_path) == 0
    append_event(3, is_start=True, cmdline="a", at=1.0, base=tmp_path)
    append_event(3, is_start=False, cmdline="a", at=2.0, base=tmp_path)
    assert event_count(3, base=tmp_path) == 2


def test_discard_events_removes_the_log_and_is_idempotent(tmp_path: Path) -> None:
    append_event(5, is_start=True, cmdline="a", at=1.0, base=tmp_path)
    assert events_file(5, base=tmp_path).is_file()

    discard_events(5, base=tmp_path)
    discard_events(5, base=tmp_path)

    assert not events_file(5, base=tmp_path).exists()


def test_default_events_dir_prefers_xdg_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOP_CMD_EVENTS_DIR", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert default_events_dir() == Path("/run/user/1000/hop/cmd-events")


def test_default_events_dir_falls_back_to_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOP_CMD_EVENTS_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_events_dir() == Path("/tmp/hop/cmd-events")


def test_default_events_dir_honors_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOP_CMD_EVENTS_DIR", "/custom/events")
    assert default_events_dir() == Path("/custom/events")


def test_events_file_without_base_uses_default_events_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOP_CMD_EVENTS_DIR", "/custom/events")
    assert events_file(4) == Path("/custom/events/4.jsonl")
