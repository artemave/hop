"""Shell command start/stop events, bridged from kitty to ``hop wait``.

The kitty watcher at ``hop/kitten/cmd_events/main.py`` calls ``append_event``
on every ``on_cmd_startstop`` and ``discard_events`` on ``on_close``. ``hop
wait`` reads the per-window log to learn when the dispatched command returned
to its prompt.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CmdEvent:
    is_start: bool
    cmdline: str
    time: float


def default_events_dir() -> Path:
    if override := os.environ.get("HOP_CMD_EVENTS_DIR"):
        return Path(override)
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "hop" / "cmd-events"


def events_file(window_id: int, *, base: Path | None = None) -> Path:
    return (base if base is not None else default_events_dir()) / f"{window_id}.jsonl"


def append_event(window_id: int, *, is_start: bool, cmdline: str, at: float, base: Path | None = None) -> None:
    path = events_file(window_id, base=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"is_start": is_start, "cmdline": cmdline, "time": at})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def discard_events(window_id: int, *, base: Path | None = None) -> None:
    events_file(window_id, base=base).unlink(missing_ok=True)


def event_count(window_id: int, *, base: Path | None = None) -> int:
    try:
        text = events_file(window_id, base=base).read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    return len(text.splitlines())


def read_events(window_id: int, *, since: int, base: Path | None = None) -> list[CmdEvent]:
    try:
        text = events_file(window_id, base=base).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    events: list[CmdEvent] = []
    for raw in text.splitlines()[since:]:
        record = json.loads(raw)
        events.append(
            CmdEvent(
                is_start=bool(record["is_start"]),
                cmdline=str(record["cmdline"]),
                time=float(record["time"]),
            )
        )
    return events
