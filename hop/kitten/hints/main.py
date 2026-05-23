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

from hop.app import build_kitten_services  # noqa: E402
from hop.commands.open_selection import open_selection_in_window  # noqa: E402
from hop.focused import paths_exist as focused_paths_exist  # noqa: E402
from hop.targets import VISIBLE_OUTPUT_TARGET_PATTERN  # noqa: E402

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
    # The kitten is a thin shell: extract candidates, ask hop which exist for
    # the focused session, yield marks for the survivors. Session, backend,
    # cwd, and IPC live behind hop.focused.paths_exist.
    matches: list[tuple[int, int, str]] = []
    for match in VISIBLE_OUTPUT_TARGET_PATTERN.finditer(text):
        for group_name in ("url", "rails", "rails_bare", "file"):
            group_value = match.group(group_name)
            if group_value:
                start, end = match.span(group_name)
                break
        else:
            continue
        selected_text = group_value.replace("\0", "").replace("\n", "")
        matches.append((start, end, selected_text))

    if not matches:
        return

    # URLs always highlight — existence is a filesystem concept and doesn't
    # apply. Files are filtered through the focused-session backend.
    file_candidates = [text_ for _, _, text_ in matches if not _looks_like_url(text_)]
    existing_files: set[str] = focused_paths_exist(file_candidates) if file_candidates else set()

    for index, (start, end, selected_text) in enumerate(
        (entry for entry in matches if _looks_like_url(entry[2]) or entry[2] in existing_files),
    ):
        yield Mark(index, start, end, selected_text, {})


def _looks_like_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


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
            boss=boss,
        )


def dispatch_selected_match(
    selection: str,
    *,
    source_cwd: str | None,
    listen_on: str | None,
    boss: Any,
) -> None:
    log = _configure_logger()
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
