"""Hover links: the hints kitten's targets, as hyperlinks under the mouse.

The kitty watcher at ``hop/kitten/hover_links/main.py`` feeds the hovered
logical line (the hovered row plus the rows soft-wrapped onto it) to a
``HoverLinkResolver`` and paints the links it returns. The watcher runs on
kitty's boss (UI) thread, while an existence check may be a ``podman exec``
or an ssh round trip — so lookups run on an executor, and a line's links
appear on the first mouse move after its lookup lands.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence
from urllib.parse import quote, unquote

from hop.targets import VisibleOutputMatch, existing_visible_output_targets

HOP_URL_PREFIX = "hop://"
OPEN_ACTION = "open"
MAX_CACHED_LINES = 2048

logger = logging.getLogger("hop.hover_links")

SessionPathsExist = Callable[[list[str], str, str | None], set[str]]


class KittyLine(Protocol):
    """The slice of kitty's ``Line`` a hover lookup reads."""

    def __len__(self) -> int: ...

    def __getitem__(self, x: int) -> str: ...

    def width(self, x: int) -> int: ...

    def hyperlink_ids(self) -> tuple[int, ...]: ...


@dataclass(frozen=True, slots=True)
class Row:
    """A snapshot of one screen row.

    ``screen.visual_line(y)`` hands out one shared ``Line`` that the next
    call overwrites, so a multi-row line has to be copied row by row.
    ``cells`` holds each cell's ``(text, width)``: a wide character's
    continuation cell carries the same text with width 0, and an empty
    cell is ``("\\0", 0)``.
    """

    cells: tuple[tuple[str, int], ...]
    hyperlink_ids: tuple[int, ...]

    @classmethod
    def from_line(cls, line: KittyLine) -> Row:
        cells: list[tuple[str, int]] = []
        for x in range(len(line)):
            text = line[x]
            # kitty's ``Line.width`` fails with a SystemError on an empty cell.
            cells.append((text, 0 if text == "\0" else line.width(x)))
        return cls(cells=tuple(cells), hyperlink_ids=line.hyperlink_ids())


def hop_url(action: str, argument: str) -> str:
    return f"{HOP_URL_PREFIX}{action}/{quote(argument, safe='/:')}"


def parse_hop_url(url: str) -> tuple[str, str] | None:
    """``(action, argument)`` of a ``hop://<action>/<argument>`` URL."""

    if not url.startswith(HOP_URL_PREFIX):
        return None
    action, _, argument = url.removeprefix(HOP_URL_PREFIX).partition("/")
    return action, unquote(argument)


def action_for_clicked_url(url: str) -> tuple[str, str] | None:
    """What a click on ``url`` in a hop session should do, or ``None`` to
    leave it to kitty.

    A plain ``http(s)`` URL opens through hop too: kitty detects URLs in the
    text itself, so a click that lands before the hovered line's lookup has
    painted its ``hop://`` link — or on a program's own OSC 8 link — still
    reaches the session browser with the backend's localhost translation.
    """

    if url.startswith(("http://", "https://")):
        return OPEN_ACTION, url
    return parse_hop_url(url)


@dataclass(frozen=True, slots=True)
class CellRange:
    row: int
    first_column: int
    last_column: int


@dataclass(frozen=True, slots=True)
class HoverLink:
    selection: str
    ranges: tuple[CellRange, ...]

    @property
    def url(self) -> str:
        return hop_url(OPEN_ACTION, self.selection)


@dataclass(frozen=True, slots=True)
class LinkPaint:
    """One ``screen.set_hyperlink_for_range`` call; ``url=None`` clears."""

    row: int
    first_column: int
    last_column: int
    url: str | None


def logical_line_rows(row: int, row_count: int, wraps: Callable[[int], bool]) -> range:
    """The visible rows forming the logical line that ``row`` is part of;
    ``wraps(y)`` tells whether row ``y`` soft-wraps onto the next."""

    top = row
    while top > 0 and wraps(top - 1):
        top -= 1
    bottom = row
    while bottom < row_count - 1 and wraps(bottom):
        bottom += 1
    return range(top, bottom + 1)


def line_text(rows: Sequence[Row]) -> tuple[str, list[tuple[int, int]]]:
    """The logical line's text and, per character, its ``(row, column)`` cell.

    ``str(line)`` can't be used: it drops the continuation cells of wide
    characters, so its offsets drift from cell columns after the first CJK
    character or emoji. A wrapped row can end in empty cells (a wide
    character that didn't fit moves to the next row); those are skipped so
    a token split there still reads as one.
    """

    characters: list[str] = []
    cells: list[tuple[int, int]] = []
    for row_index, row in enumerate(rows):
        end = len(row.cells)
        if row_index < len(rows) - 1:
            while end and row.cells[end - 1][0] == "\0":
                end -= 1
        for x, (text, width) in enumerate(row.cells[:end]):
            if text == "\0":
                characters.append(" ")
                cells.append((row_index, x))
            elif width:
                characters.extend(text)
                cells.extend([(row_index, x)] * len(text))
    return "".join(characters), cells


def paint_operations(
    links: Sequence[HoverLink],
    rows: Sequence[Row],
    url_for_id: Callable[[int], str | None],
) -> list[LinkPaint]:
    """What to paint so each link covers its cells, and nothing overlaps it.

    A link already painted exactly is left alone, so repeated mouse moves
    don't redraw. Any other link touching a link's cells — a stale paint or
    the program's own OSC 8 link — is cleared across the whole logical line
    first, so no fragment of it survives next to ours.
    """

    ids = [list(row.hyperlink_ids) for row in rows]
    paints: list[LinkPaint] = []
    for link in links:
        covered = [ids[r.row][x] for r in link.ranges for x in range(r.first_column, r.last_column + 1)]
        if covered[0] and all(hid == covered[0] for hid in covered) and url_for_id(covered[0]) == link.url:
            continue
        for foreign_id in {hid for hid in covered if hid}:
            paints.extend(_clear_id(ids, foreign_id))
        for r in link.ranges:
            paints.append(LinkPaint(r.row, r.first_column, r.last_column, link.url))
    return paints


def _clear_id(ids: list[list[int]], hyperlink_id: int) -> list[LinkPaint]:
    paints: list[LinkPaint] = []
    for row, row_ids in enumerate(ids):
        x = 0
        while x < len(row_ids):
            if row_ids[x] != hyperlink_id:
                x += 1
                continue
            start = x
            while x < len(row_ids) and row_ids[x] == hyperlink_id:
                row_ids[x] = 0
                x += 1
            paints.append(LinkPaint(row, start, x - 1, None))
    return paints


_LineKey = tuple[str, str | None, str]


class HoverLinkResolver:
    def __init__(self, paths_exist: SessionPathsExist, executor: Executor) -> None:
        self._paths_exist = paths_exist
        self._executor = executor
        self._lock = threading.Lock()
        self._resolved: dict[_LineKey, list[VisibleOutputMatch]] = {}
        self._pending: set[_LineKey] = set()

    def links_for(self, rows: Sequence[Row], *, session_name: str, source_cwd: str | None) -> list[HoverLink] | None:
        """The logical line's links, or ``None`` while its lookup is still running.

        ``rows`` are the logical line's visible rows; each link's ranges
        index into them.
        """

        text, cells = line_text(rows)
        key = (session_name, source_cwd, text)
        with self._lock:
            matches = self._resolved.get(key)
            should_submit = matches is None and key not in self._pending
            if should_submit:
                self._pending.add(key)
        if should_submit:
            self._executor.submit(self._resolve, key).add_done_callback(lambda future: self._settle(key, future))
        if matches is None:
            return None
        return [
            HoverLink(selection=match.selection, ranges=_cell_ranges(rows, cells[match.start : match.end]))
            for match in matches
        ]

    def _resolve(self, key: _LineKey) -> list[VisibleOutputMatch]:
        session_name, source_cwd, text = key
        return existing_visible_output_targets(
            text, lambda candidates: self._paths_exist(candidates, session_name, source_cwd)
        )

    def _settle(self, key: _LineKey, future: Future[list[VisibleOutputMatch]]) -> None:
        # A failed lookup (e.g. a dead ssh master) caches as "no links" so
        # every later mouse move over the line doesn't retry it.
        error = future.exception()
        if error is not None:
            logger.error("hover link lookup failed for %r", key, exc_info=error)
        with self._lock:
            if len(self._resolved) >= MAX_CACHED_LINES:
                self._resolved.clear()
            self._resolved[key] = [] if error is not None else future.result()
            self._pending.discard(key)


def _cell_ranges(rows: Sequence[Row], cells: list[tuple[int, int]]) -> tuple[CellRange, ...]:
    ranges: list[CellRange] = []
    for row in sorted({row for row, _ in cells}):
        columns = [x for r, x in cells if r == row]
        last = max(columns)
        ranges.append(CellRange(row, min(columns), last + rows[row].cells[last][1] - 1))
    return tuple(ranges)
