from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# kitty's bundled build ships a hermetic Python whose sys.path starts empty, so
# it never sees the site-packages `hop` was installed into. This file always
# lives at hop/kitten/cmd_events/main.py, so the directory holding the `hop`
# package is three parents up — same trick as the hints kitten.
_HOP_PARENT = str(Path(__file__).resolve().parents[3])
if _HOP_PARENT not in sys.path:
    sys.path.insert(0, _HOP_PARENT)

from hop.cmd_events import append_event, discard_events  # noqa: E402


def on_cmd_startstop(boss: Any, window: Any, data: dict[str, Any]) -> None:
    append_event(
        window.id,
        is_start=bool(data["is_start"]),
        cmdline=str(data.get("cmdline", "")),
        at=float(data["time"]),
    )


def on_close(boss: Any, window: Any, data: dict[str, Any]) -> None:
    discard_events(window.id)
