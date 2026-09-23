from __future__ import annotations

import logging
import os
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from hop.app import build_kitten_services
from hop.commands.open_selection import open_selection_in_window

LOGGER_NAME = "hop.open_selection"


def _log_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "hop" / "open-selection.log"


def configure_logger() -> logging.Logger:
    log = logging.getLogger(LOGGER_NAME)
    if any(getattr(h, "_hop_kitten", False) for h in log.handlers):
        return log
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=128 * 1024, backupCount=2)
    handler._hop_kitten = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


def dispatch_selected_match(
    selection: str,
    *,
    source_cwd: str | None,
    listen_on: str | None,
    boss: Any,
) -> None:
    log = configure_logger()
    # In-kitten path: the editor adapter must drive kitty via the boss
    # API, not synchronous IPC against the same kitty boss (would deadlock
    # while handle_result is running).
    services = build_kitten_services(boss)
    try:
        open_selection_in_window(
            selection,
            source_cwd=source_cwd,
            listen_on=listen_on,
            neovim=services.neovim,
            browser=services.browser,
            session_backend_for=services.session_backends.for_session,
        )
    except Exception:
        log.error("dispatch raised for selection=%r:\n%s", selection, traceback.format_exc())
        raise
