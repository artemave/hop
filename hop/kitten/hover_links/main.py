from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, cast

from kitty.fast_data_types import (  # pyright: ignore[reportMissingImports]
    add_timer as _kitty_add_timer,  # pyright: ignore[reportUnknownVariableType]
)

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
    PathExistence,
    Row,
    ViewportLinks,
    action_for_clicked_url,
    logical_line_rows,
    paint_operations,
    viewport_lines,
)
from hop.kitten.dispatch import dispatch_selected_match  # noqa: E402
from hop.kitty import session_name_from_listen_on  # noqa: E402

PAINT_RETRY_SECONDS = 0.05

add_timer = cast("Callable[[Callable[[int | None], None], float, bool], int]", _kitty_add_timer)

_existence = PathExistence(
    lambda candidates, session_name, source_cwd: session_paths_exist(
        candidates, session_name=session_name, source_cwd=source_cwd
    )
)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hop-hover-links")
_viewports: dict[int, ViewportLinks] = {}
_screen_texts: dict[int, tuple[str | None, tuple[tuple[str, bool], ...]]] = {}
_hovered_rows: dict[int, int] = {}
_retrying: set[int] = set()


def on_mouse_move(boss: Any, window: Any, data: dict[str, Any]) -> None:
    window.open_url_handler = _open_clicked_url
    _hovered_rows[window.id] = data["y"]
    _show_viewport(boss, window)
    _paint_hovered(boss, window)


def on_close(boss: Any, window: Any, data: dict[str, Any]) -> None:
    for state in (_viewports, _screen_texts, _hovered_rows):
        state.pop(window.id, None)
    _retrying.discard(window.id)


def _show_viewport(boss: Any, window: Any) -> None:
    session_name = session_name_from_listen_on(boss.listening_on)
    assert session_name is not None, f"hover links watcher loaded outside a hop session kitty: {boss.listening_on!r}"
    screen = window.screen
    # ``visual_line`` hands out one shared Line that the next call overwrites,
    # so each row is read before asking for the next.
    texts: list[tuple[str, bool]] = []
    for y in range(screen.lines):
        line = screen.visual_line(y)
        texts.append((str(line), line.last_char_has_wrapped_flag()))
    source_cwd = window.cwd_of_child
    screen_text = (source_cwd, tuple(texts))
    if _screen_texts.get(window.id) == screen_text:
        return
    _screen_texts[window.id] = screen_text
    rows = [Row.from_line(screen.visual_line(y)) for y in range(screen.lines)]
    viewport = _viewports.setdefault(window.id, ViewportLinks(_existence, _executor))
    viewport.show(
        viewport_lines(rows, [wraps for _, wraps in texts]),
        session_name=session_name,
        source_cwd=source_cwd,
    )


def _paint_hovered(boss: Any, window: Any) -> None:
    screen = window.screen
    row_numbers = logical_line_rows(
        _hovered_rows[window.id], screen.lines, lambda y: screen.visual_line(y).last_char_has_wrapped_flag()
    )
    rows = [Row.from_line(screen.visual_line(y)) for y in row_numbers]
    viewport = _viewports[window.id]
    links = viewport.links_for(rows)
    if links is None:
        if viewport.resolving and window.id not in _retrying:
            _retrying.add(window.id)
            add_timer(lambda _timer_id: _retry_paint(boss, window.id), PAINT_RETRY_SECONDS, False)
        return
    for paint in paint_operations(links, rows, screen.hyperlink_for_id):
        screen.set_hyperlink_for_range(row_numbers[paint.row], paint.first_column, paint.last_column, paint.url)


def _retry_paint(boss: Any, window_id: int) -> None:
    if window_id not in _retrying:
        return
    _retrying.discard(window_id)
    window = boss.window_id_map.get(window_id)
    if window is not None:
        _paint_hovered(boss, window)


def _open(boss: Any, window: Any, selection: str) -> None:
    dispatch_selected_match(selection, source_cwd=window.cwd_of_child, listen_on=boss.listening_on, boss=boss)


_ACTIONS: dict[str, Callable[[Any, Any, str], None]] = {OPEN_ACTION: _open}


def _open_clicked_url(boss: Any, window: Any, url: str, hyperlink_id: int, cwd: str) -> bool:
    clicked = action_for_clicked_url(url)
    if clicked is None:
        return False
    action, argument = clicked
    _ACTIONS[action](boss, window, argument)
    return True
