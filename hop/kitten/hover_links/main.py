from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

# kitty's bundled build ships a hermetic Python whose sys.path starts empty, so
# it never sees the site-packages `hop` was installed into. This file always
# lives at hop/kitten/hover_links/main.py, so the directory holding the `hop`
# package is three parents up — same trick as the hints kitten.
_HOP_PARENT = str(Path(__file__).resolve().parents[3])
if _HOP_PARENT not in sys.path:
    sys.path.insert(0, _HOP_PARENT)

from hop.focused import session_paths_exist  # noqa: E402
from hop.hover_links import (  # noqa: E402
    OPEN_ACTION,
    HoverLinkResolver,
    Row,
    logical_line_rows,
    paint_operations,
    parse_hop_url,
)
from hop.kitten.dispatch import dispatch_selected_match  # noqa: E402
from hop.kitty import session_name_from_listen_on  # noqa: E402

_resolver = HoverLinkResolver(
    lambda candidates, session_name, source_cwd: session_paths_exist(
        candidates, session_name=session_name, source_cwd=source_cwd
    ),
    ThreadPoolExecutor(max_workers=1, thread_name_prefix="hop-hover-links"),
)


def on_mouse_move(boss: Any, window: Any, data: dict[str, Any]) -> None:
    session_name = session_name_from_listen_on(boss.listening_on)
    assert session_name is not None, f"hover links watcher loaded outside a hop session kitty: {boss.listening_on!r}"
    window.open_url_handler = _open_hop_url
    screen = window.screen
    row_numbers = logical_line_rows(
        data["y"], screen.lines, lambda y: screen.visual_line(y).last_char_has_wrapped_flag()
    )
    rows = [Row.from_line(screen.visual_line(y)) for y in row_numbers]
    links = _resolver.links_for(rows, session_name=session_name, source_cwd=window.cwd_of_child)
    if links is None:
        return
    for paint in paint_operations(links, rows, screen.hyperlink_for_id):
        screen.set_hyperlink_for_range(row_numbers[paint.row], paint.first_column, paint.last_column, paint.url)


def _open(boss: Any, window: Any, selection: str) -> None:
    dispatch_selected_match(selection, source_cwd=window.cwd_of_child, listen_on=boss.listening_on, boss=boss)


_ACTIONS: dict[str, Callable[[Any, Any, str], None]] = {OPEN_ACTION: _open}


def _open_hop_url(boss: Any, window: Any, url: str, hyperlink_id: int, cwd: str) -> bool:
    parsed = parse_hop_url(url)
    if parsed is None:
        return False
    action, argument = parsed
    _ACTIONS[action](boss, window, argument)
    return True
