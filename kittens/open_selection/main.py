from __future__ import annotations

import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# handle_result runs in the long-lived kitty boss process, which caches
# `hop.*` imports in sys.modules. When we detect we're inside a kitty boss
# (kitty's C extension is loaded), drop the cached hop modules so source edits
# are picked up without requiring a kitty restart. Outside that context (e.g.
# pytest) leave sys.modules alone — clearing it would break other tests that
# already imported hop modules.
if "kitty.fast_data_types" in sys.modules:
    for _hop_module in [n for n in list(sys.modules) if n == "hop" or n.startswith("hop.")]:
        sys.modules.pop(_hop_module, None)

from hop.app import build_default_services  # noqa: E402
from hop.commands.open_selection import open_selection_in_window  # noqa: E402
from hop.targets import VISIBLE_OUTPUT_TARGET_PATTERN, resolve_visible_output_target  # noqa: E402

LOGGER_NAME = "hop.open_selection"


def _log_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "hop" / "open-selection.log"


def _configure_logger() -> logging.Logger:
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


def mark(text: Any, args: Any, Mark: Any, extra_cli_args: Any, *unused_args: Any) -> Any:
    # The kitten subprocess inherits kitty's cwd; for hop sessions kitty is
    # launched with --directory <project_root>, so this is the project root.
    base_cwd = Path.cwd()
    index = 0
    for match in VISIBLE_OUTPUT_TARGET_PATTERN.finditer(text):
        start, end = match.span()
        selected_text = match.group(0).replace("\0", "").replace("\n", "")
        if resolve_visible_output_target(
            selected_text,
            terminal_cwd=base_cwd,
            project_root=base_cwd,
        ) is None:
            continue
        yield Mark(index, start, end, selected_text, {})
        index += 1


def handle_result(  # noqa: PLR0913
    args: Any,
    data: Any,
    target_window_id: Any,
    boss: Any,
    extra_cli_args: Any,
    *unused_args: Any,
) -> None:
    # The kitten is *already running inside* the kitty boss process, so resolve
    # the source window directly from boss.window_id_map. Using kitty remote
    # control to talk to ourselves is fragile: KITTY_LISTEN_ON in the boss can
    # leak in from a parent kitty, and boss.listening_on can disagree with the
    # kitty instance whose window-id namespace target_window_id belongs to.
    log = _configure_logger()
    listen_on = getattr(boss, "listening_on", None) or None
    window_map = getattr(boss, "window_id_map", None)
    window = window_map.get(target_window_id) if window_map is not None else None
    source_cwd = getattr(window, "cwd_of_child", None) if window is not None else None
    log.info(
        "handle_result: target_window_id=%s known=%s cwd=%r listen_on=%r",
        target_window_id,
        window is not None,
        source_cwd,
        listen_on,
    )
    for matched_text in data.get("match", ()):
        if not matched_text:
            continue
        dispatch_selected_match(
            matched_text,
            source_cwd=source_cwd,
            listen_on=listen_on,
        )


def dispatch_selected_match(
    selection: str,
    *,
    source_cwd: str | None,
    listen_on: str | None,
) -> None:
    log = _configure_logger()
    services = build_default_services()
    try:
        open_selection_in_window(
            selection,
            source_cwd=source_cwd,
            listen_on=listen_on,
            neovim=services.neovim,
            browser=services.browser,
        )
    except Exception:
        log.error("dispatch raised for selection=%r:\n%s", selection, traceback.format_exc())
        raise
