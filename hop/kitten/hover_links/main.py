from __future__ import annotations

import os
import shutil
import sys
import time
from functools import partial
from pathlib import Path
from typing import Any, Callable, cast

from kitty.constants import (  # pyright: ignore[reportMissingImports]
    kitten_exe as _kitty_kitten_exe,  # pyright: ignore[reportUnknownVariableType]
)
from kitty.fast_data_types import (  # pyright: ignore[reportMissingImports]
    add_timer as _kitty_add_timer,  # pyright: ignore[reportUnknownVariableType]
)
from kitty.fast_data_types import (  # pyright: ignore[reportMissingImports]
    get_os_window_size as _kitty_get_os_window_size,  # pyright: ignore[reportUnknownVariableType]
)
from kitty.fast_data_types import (  # pyright: ignore[reportMissingImports]
    remove_timer as _kitty_remove_timer,  # pyright: ignore[reportUnknownVariableType]
)
from kitty.fast_data_types import (  # pyright: ignore[reportMissingImports]
    wakeup_main_loop as _kitty_wakeup_main_loop,  # pyright: ignore[reportUnknownVariableType]
)
from kitty.launch import (  # pyright: ignore[reportMissingImports]
    launch as _kitty_launch,  # pyright: ignore[reportUnknownVariableType]
)
from kitty.launch import (  # pyright: ignore[reportMissingImports]
    parse_launch_args as _kitty_parse_launch_args,  # pyright: ignore[reportUnknownVariableType]
)

# kitty's bundled build ships a hermetic Python whose sys.path starts empty, so
# it never sees the site-packages `hop` was installed into. This file always
# lives at hop/kitten/hover_links/main.py, so the directory holding the `hop`
# package is three parents up — same trick as the hints kitten.
_HOP_PARENT = str(Path(__file__).resolve().parents[3])
if _HOP_PARENT not in sys.path:
    sys.path.insert(0, _HOP_PARENT)

from hop.config import PreviewSettings  # noqa: E402
from hop.focused import FilePreview, ImagePreview, session_file_preview, session_paths_exist  # noqa: E402
from hop.hover_links import (  # noqa: E402
    OPEN_ACTION,
    PathExistence,
    Row,
    ViewportLinks,
    action_for_clicked_url,
    logical_line_rows,
    paint_operations,
    rows_join,
    viewport_lines,
)
from hop.hover_preview import (  # noqa: E402
    KittyCells,
    find_bat,
    link_columns,
    new_runtime_file,
    panel_background,
    panel_launch_args,
    panel_placement,
    preview_room,
    preview_selection,
    render_image_preview,
    render_link_card,
    render_preview,
    session_preview_settings,
    write_preview_files,
)
from hop.kitten.dispatch import dispatch_selected_match  # noqa: E402
from hop.kitty import session_name_from_listen_on  # noqa: E402
from hop.link_preview import LinkCard  # noqa: E402
from hop.sway import SwayIpcAdapter, SwayWindowPlacement  # noqa: E402
from hop.targets import SyntacticUrlTarget, parse_visible_output_target  # noqa: E402

PREVIEW_IDLE_SECONDS = 0.3
LINK_CARD_TTL_SECONDS = 300.0

Preview = FilePreview | ImagePreview | LinkCard

add_timer = cast("Callable[[Callable[[int | None], None], float, bool], int]", _kitty_add_timer)
remove_timer = cast("Callable[[int], None]", _kitty_remove_timer)
wakeup_main_loop = cast("Callable[[], None]", _kitty_wakeup_main_loop)
get_os_window_size = cast("Callable[[int], dict[str, Any]]", _kitty_get_os_window_size)
parse_launch_args = cast("Callable[[list[str]], tuple[Any, list[str]]]", _kitty_parse_launch_args)
kitten_exe = cast("Callable[[], str]", _kitty_kitten_exe)
launch = cast("Callable[[Any, Any, list[str]], Any]", _kitty_launch)

_existence = PathExistence(
    lambda candidates, session_name, source_cwd: session_paths_exist(
        candidates, session_name=session_name, source_cwd=source_cwd
    )
)
_viewports: dict[int, ViewportLinks] = {}
_screen_texts: dict[int, tuple[str | None, tuple[tuple[str, bool], ...]]] = {}
_hovered_rows: dict[int, int] = {}

_UNREAD: Any = object()
_preview_settings: PreviewSettings | None = _UNREAD
_last_moves: dict[int, dict[str, Any]] = {}
# (window id, link url) the preview is for while the modifier is held over a
# link; the panel shows only while it still matches.
_preview_wanted: tuple[int, str] | None = None
_preview_anchor: tuple[int, int] = (0, 0)
_preview_loaded: tuple[Preview, SwayWindowPlacement, int, int] | None = None
_preview_timer: int | None = None
_preview_panel: Any = None
_bat = find_bat()
# url -> (when it was fetched, its card or None when it has none)
_link_cards: dict[str, tuple[float, LinkCard | None]] = {}
_link_fetches: set[str] = set()
# The url whose card the wanted preview waits for.
_awaited_link: str | None = None


def on_mouse_move(boss: Any, window: Any, data: dict[str, Any]) -> None:
    if window is _preview_panel:
        return
    window.open_url_handler = _open_clicked_url
    _hovered_rows[window.id] = data["y"]
    _last_moves[window.id] = data
    _show_viewport(boss, window)
    _paint_hovered(boss, window)
    _update_preview(boss, window)


def on_close(boss: Any, window: Any, data: dict[str, Any]) -> None:
    global _preview_panel
    if window is _preview_panel:
        _preview_panel = None
        return
    for state in (_viewports, _screen_texts, _hovered_rows, _last_moves):
        state.pop(window.id, None)
    if _preview_wanted is not None and _preview_wanted[0] == window.id:
        _want_preview(boss, None)


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
    viewport = _viewports.setdefault(window.id, ViewportLinks(_existence))
    viewport.show(
        viewport_lines(rows),
        session_name=session_name,
        source_cwd=source_cwd,
    )


def _paint_hovered(boss: Any, window: Any) -> None:
    screen = window.screen
    row_numbers = logical_line_rows(
        _hovered_rows[window.id],
        screen.lines,
        lambda y: rows_join(Row.from_line(screen.visual_line(y)), Row.from_line(screen.visual_line(y + 1))),
    )
    rows = [Row.from_line(screen.visual_line(y)) for y in row_numbers]
    links = _viewports[window.id].links_for(rows)
    for paint in paint_operations(links, rows, screen.hyperlink_for_id):
        screen.set_hyperlink_for_range(row_numbers[paint.row], paint.first_column, paint.last_column, paint.url)


def _update_preview(boss: Any, window: Any) -> None:
    global _preview_settings, _preview_loaded, _preview_anchor
    if _preview_settings is _UNREAD:
        session_name = session_name_from_listen_on(boss.listening_on)
        assert session_name is not None
        _preview_settings = session_preview_settings(session_name)
    if _preview_settings is None:
        return
    move = _last_moves[window.id]
    if not move[_preview_settings.modifier]:
        _want_preview(boss, None)
        return
    row = Row.from_line(window.screen.visual_line(move["y"]))
    columns = link_columns(row.hyperlink_ids, move["x"])
    url = window.screen.hyperlink_for_id(row.hyperlink_ids[move["x"]]) if columns is not None else None
    selection = preview_selection(url) if url is not None else None
    if selection is None:
        _want_preview(boss, None)
        return
    assert columns is not None and url is not None
    wanted = (window.id, url)
    if wanted != _preview_wanted:
        _want_preview(boss, wanted)
        _preview_anchor = (columns[0], move["y"])
        target = parse_visible_output_target(selection)
        if isinstance(target, SyntacticUrlTarget):
            _request_link_card(boss, window, target.url)
        else:
            session_name = session_name_from_listen_on(boss.listening_on)
            assert session_name is not None
            preview = session_file_preview(selection, session_name=session_name, source_cwd=window.cwd_of_child)
            _preview_loaded = _placed(boss, window, preview)
    if _preview_panel is None:
        _restart_preview_timer(boss)


def _request_link_card(boss: Any, window: Any, url: str) -> None:
    global _preview_loaded, _awaited_link
    cached = _link_cards.get(url)
    if cached is not None and time.monotonic() - cached[0] < LINK_CARD_TTL_SECONDS:
        _preview_loaded = _placed(boss, window, cached[1])
        return
    _awaited_link = url
    if url in _link_fetches:
        return
    session_name = session_name_from_listen_on(boss.listening_on)
    assert session_name is not None
    write_path = new_runtime_file()
    _link_fetches.add(url)
    boss.run_background_process(
        [shutil.which("hop") or "hop", "__link-preview", session_name, url, str(write_path)],
        notify_on_death=partial(_on_link_card, boss, url, write_path),
    )


def _on_link_card(boss: Any, url: str, write_path: Path, exit_status: int, error: Exception | None) -> None:
    global _preview_loaded, _awaited_link
    _link_fetches.discard(url)
    card = LinkCard.from_json(write_path.read_text(encoding="utf-8")) if error is None and exit_status == 0 else None
    write_path.unlink(missing_ok=True)
    _link_cards[url] = (time.monotonic(), card)
    if url != _awaited_link:
        return
    _awaited_link = None
    assert _preview_wanted is not None
    _preview_loaded = _placed(boss, boss.window_id_map[_preview_wanted[0]], card)
    if _preview_timer is None and _preview_panel is None:
        _show_preview(boss)


def _placed(boss: Any, window: Any, preview: Preview | None) -> tuple[Preview, SwayWindowPlacement, int, int] | None:
    if preview is None:
        return None
    app_id = boss.os_window_map[window.os_window_id].wm_class
    placement = SwayIpcAdapter().window_placement(pid=os.getpid(), app_id=app_id)
    if placement is None:
        return None
    return preview, placement, *_preview_anchor


def _restart_preview_timer(boss: Any) -> None:
    global _preview_timer
    _cancel_preview_timer()
    _preview_timer = add_timer(lambda _timer_id: _show_preview(boss), PREVIEW_IDLE_SECONDS, False)


def _cancel_preview_timer() -> None:
    global _preview_timer
    if _preview_timer is not None:
        remove_timer(_preview_timer)
        _preview_timer = None


def _show_preview(boss: Any) -> None:
    global _preview_timer
    _preview_timer = None
    if _preview_wanted is None or _preview_loaded is None:
        return
    preview, placement, column, row = _preview_loaded
    _open_preview(boss, boss.window_id_map[_preview_wanted[0]], preview, placement, column=column, row=row)
    # A kitty timer doesn't wake the main loop, so the panel would only
    # appear on the next input event.
    wakeup_main_loop()


def _open_preview(
    boss: Any,
    window: Any,
    preview: Preview,
    placement: SwayWindowPlacement,
    *,
    column: int,
    row: int,
) -> None:
    global _preview_panel
    room = preview_room(row, window.screen.lines)
    size = get_os_window_size(window.os_window_id)
    if isinstance(preview, ImagePreview):
        rendered = render_image_preview(
            preview,
            kitten=kitten_exe(),
            max_lines=room.lines,
            max_columns=window.screen.columns,
            cell_width=size["cell_width"],
            cell_height=size["cell_height"],
        )
        if rendered is None:
            return
    elif isinstance(preview, LinkCard):
        rendered = render_link_card(
            preview,
            kitten=kitten_exe(),
            max_lines=room.lines,
            max_columns=window.screen.columns,
            cell_width=size["cell_width"],
            cell_height=size["cell_height"],
        )
    else:
        rendered = render_preview(preview, bat=_bat, max_lines=room.lines, max_columns=window.screen.columns)
    cells = KittyCells(
        left=window.geometry.left,
        top=window.geometry.top,
        cell_width=size["cell_width"],
        cell_height=size["cell_height"],
        scale=size["width"] / size["framebuffer_width"],
    )
    panel = panel_placement(
        placement,
        cells,
        column=column,
        row=row,
        columns=rendered.columns,
        lines=rendered.lines,
        below=room.below,
        window_columns=window.screen.columns,
    )
    assert _preview_settings is not None
    background = panel_background(window.screen.color_profile.default_bg.rgb, shift=_preview_settings.background_shift)
    opts, args = parse_launch_args(
        panel_launch_args(panel, rendered, write_preview_files(rendered), background=background)
    )
    _preview_panel = launch(boss, opts, args)


def _want_preview(boss: Any, wanted: tuple[int, str] | None) -> None:
    global _preview_wanted, _preview_loaded, _preview_panel, _awaited_link
    if wanted == _preview_wanted:
        return
    _preview_wanted = wanted
    _preview_loaded = None
    _awaited_link = None
    _cancel_preview_timer()
    if _preview_panel is not None:
        boss.mark_window_for_close(_preview_panel)
        _preview_panel = None


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
