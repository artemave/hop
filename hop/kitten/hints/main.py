from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# kitty's bundled build ships a hermetic Python: `site` is stripped and sys.path
# starts empty, so it never reads the site-packages `hop` was installed into.
# (The Fedora system kitty uses the system Python and does, which is why this
# only broke after switching kitty builds.) This file always lives at
# hop/kitten/hints/main.py, so the directory holding the `hop` package is three
# parents up — put it on sys.path so the import resolves under any kitty build.
_HOP_PARENT = str(Path(__file__).resolve().parents[3])
if _HOP_PARENT not in sys.path:
    sys.path.insert(0, _HOP_PARENT)

# handle_result runs in the long-lived kitty boss process, which caches
# `hop.*` imports in sys.modules. When we detect we're inside a kitty boss
# (kitty's C extension is loaded), drop the cached hop modules so source edits
# are picked up without requiring a kitty restart. Outside that context (e.g.
# pytest) leave sys.modules alone — clearing it would break other tests that
# already imported hop modules.
if "kitty.fast_data_types" in sys.modules:
    for _hop_module in [n for n in list(sys.modules) if n == "hop" or n.startswith("hop.")]:
        sys.modules.pop(_hop_module, None)

from hop.focused import paths_exist as focused_paths_exist  # noqa: E402
from hop.kitten.dispatch import configure_logger, dispatch_selected_match  # noqa: E402
from hop.targets import existing_visible_output_targets  # noqa: E402


def mark(text: Any, args: Any, Mark: Any, extra_cli_args: Any, *unused_args: Any) -> Any:
    for index, match in enumerate(existing_visible_output_targets(text, focused_paths_exist)):
        yield Mark(index, match.start, match.end, match.selection, {})


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
    log = configure_logger()
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
