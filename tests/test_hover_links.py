from __future__ import annotations

import unicodedata
from concurrent.futures import Executor, Future
from typing import Callable, ParamSpec, TypeVar

import pytest

from hop.hover_links import (
    MAX_CACHED_LINES,
    CellRange,
    HoverLink,
    HoverLinkResolver,
    LinkPaint,
    Row,
    hop_url,
    line_text,
    logical_line_rows,
    paint_operations,
    parse_hop_url,
)

P = ParamSpec("P")
T = TypeVar("T")


class KittyLikeLine:
    """A row laid out the way kitty's ``Line`` does: a wide character fills
    its cell plus a zero-width continuation cell carrying the same text,
    unused cells read as ``\\0``, and ``width`` fails on them like kitty's."""

    def __init__(self, cells: list[tuple[str, int]], columns: int) -> None:
        self.cells = cells + [("\0", 0)] * (columns - len(cells))

    def __len__(self) -> int:
        return len(self.cells)

    def __getitem__(self, x: int) -> str:
        return self.cells[x][0]

    def width(self, x: int) -> int:
        text, width = self.cells[x]
        if text == "\0":
            raise SystemError("error return without exception set")
        return width

    def hyperlink_ids(self) -> tuple[int, ...]:
        return (0,) * len(self.cells)


def draw(text: str, columns: int = 40) -> list[Row]:
    """Soft-wrap ``text`` into rows like kitty: a wide character that doesn't
    fit the row's last cell leaves it empty and starts the next row."""

    rows: list[list[tuple[str, int]]] = [[]]
    for character in text:
        if unicodedata.combining(character):
            previous, width = rows[-1][-1]
            rows[-1][-1] = (previous + character, width)
            continue
        wide = unicodedata.east_asian_width(character) in "WF"
        if len(rows[-1]) + (2 if wide else 1) > columns:
            rows.append([])
        rows[-1].extend([(character, 2), (character, 0)] if wide else [(character, 1)])
    return [Row.from_line(KittyLikeLine(cells, columns)) for cells in rows]


class QueuedExecutor(Executor):
    """Holds submitted work until ``run_all`` — stands in for the watcher's
    worker thread so a test controls when a lookup lands."""

    def __init__(self) -> None:
        self.queue: list[Callable[[], None]] = []

    def submit(self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        future: Future[T] = Future()

        def run() -> None:
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as error:
                future.set_exception(error)

        self.queue.append(run)
        return future

    def run_all(self) -> None:
        while self.queue:
            self.queue.pop(0)()


class InlineExecutor(QueuedExecutor):
    def submit(self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        future = super().submit(fn, *args, **kwargs)
        self.run_all()
        return future


class PathsExist:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing
        self.calls: list[tuple[list[str], str, str | None]] = []

    def __call__(self, candidates: list[str], session_name: str, source_cwd: str | None) -> set[str]:
        self.calls.append((candidates, session_name, source_cwd))
        return set(candidates) & self.existing


def resolved_links(resolver: HoverLinkResolver, rows: list[Row]) -> list[HoverLink] | None:
    resolver.links_for(rows, session_name="demo", source_cwd=None)
    return resolver.links_for(rows, session_name="demo", source_cwd=None)


# --- line layout -------------------------------------------------------------


def test_line_text_maps_each_character_to_its_cell() -> None:
    text, cells = line_text(draw("a中b e\u0301x", columns=8))

    assert text == "a中b e\u0301x "
    assert cells == [(0, 0), (0, 1), (0, 3), (0, 4), (0, 5), (0, 5), (0, 6), (0, 7)]


def test_line_text_joins_wrapped_rows_across_a_wide_character_that_did_not_fit() -> None:
    rows = draw("abcd中x", columns=5)

    text, cells = line_text(rows)

    assert text == "abcd中x  "
    assert cells[3:6] == [(0, 3), (1, 0), (1, 2)]


def test_logical_line_rows_spans_the_rows_wrapped_onto_the_hovered_one() -> None:
    wrapping_rows = {1, 2}

    def wraps(y: int) -> bool:
        return y in wrapping_rows

    assert logical_line_rows(2, 5, wraps) == range(1, 4)
    assert logical_line_rows(0, 5, wraps) == range(0, 1)
    assert logical_line_rows(4, 5, wraps) == range(4, 5)


# --- resolver ------------------------------------------------------------------


def test_links_are_absent_until_the_lookup_lands() -> None:
    executor = QueuedExecutor()
    paths_exist = PathsExist({"app.rb"})
    resolver = HoverLinkResolver(paths_exist, executor)
    rows = draw("app.rb gone.rb https://example.com")

    assert resolver.links_for(rows, session_name="demo", source_cwd="/work") is None
    executor.run_all()

    assert resolver.links_for(rows, session_name="demo", source_cwd="/work") == [
        HoverLink("app.rb", (CellRange(0, 0, 5),)),
        HoverLink("https://example.com", (CellRange(0, 15, 33),)),
    ]
    assert paths_exist.calls == [(["app.rb", "gone.rb"], "demo", "/work")]


def test_link_columns_account_for_wide_characters() -> None:
    resolver = HoverLinkResolver(PathsExist({"app.rb:3"}), InlineExecutor())

    assert resolved_links(resolver, draw("失败 app.rb:3")) == [HoverLink("app.rb:3", (CellRange(0, 5, 12),))]


def test_a_link_ending_in_a_wide_character_covers_both_of_its_cells() -> None:
    resolver = HoverLinkResolver(PathsExist(set()), InlineExecutor())

    assert resolved_links(resolver, draw("https://example.com/中")) == [
        HoverLink("https://example.com/中", (CellRange(0, 0, 21),)),
    ]


def test_a_soft_wrapped_target_links_a_range_on_each_row() -> None:
    resolver = HoverLinkResolver(PathsExist({"app/models/user.rb:3"}), InlineExecutor())

    assert resolved_links(resolver, draw("see app/models/user.rb:3 ok", columns=10)) == [
        HoverLink("app/models/user.rb:3", (CellRange(0, 4, 9), CellRange(1, 0, 9), CellRange(2, 0, 3))),
    ]


def test_a_line_is_looked_up_once_while_its_lookup_is_in_flight() -> None:
    executor = QueuedExecutor()
    paths_exist = PathsExist(set())
    resolver = HoverLinkResolver(paths_exist, executor)
    rows = draw("app.rb")

    resolver.links_for(rows, session_name="demo", source_cwd=None)
    resolver.links_for(rows, session_name="demo", source_cwd=None)
    executor.run_all()
    resolver.links_for(rows, session_name="demo", source_cwd=None)

    assert len(paths_exist.calls) == 1


def test_the_same_text_is_looked_up_again_for_another_cwd() -> None:
    paths_exist = PathsExist(set())
    resolver = HoverLinkResolver(paths_exist, InlineExecutor())
    rows = draw("app.rb")

    resolver.links_for(rows, session_name="demo", source_cwd="/a")
    resolver.links_for(rows, session_name="demo", source_cwd="/b")

    assert [call[2] for call in paths_exist.calls] == ["/a", "/b"]


def test_a_failed_lookup_is_logged_and_settles_as_no_links(caplog: pytest.LogCaptureFixture) -> None:
    def failing_paths_exist(candidates: list[str], session_name: str, source_cwd: str | None) -> set[str]:
        raise RuntimeError("ssh master gone")

    resolver = HoverLinkResolver(failing_paths_exist, InlineExecutor())

    assert resolved_links(resolver, draw("app.rb")) == []
    assert "hover link lookup failed" in caplog.text


def test_the_cache_starts_over_once_full() -> None:
    paths_exist = PathsExist(set())
    resolver = HoverLinkResolver(paths_exist, InlineExecutor())
    first = draw("line-0")
    resolver.links_for(first, session_name="demo", source_cwd=None)
    for index in range(1, MAX_CACHED_LINES + 1):
        resolver.links_for(draw(f"line-{index}"), session_name="demo", source_cwd=None)

    calls_before = len(paths_exist.calls)
    resolver.links_for(first, session_name="demo", source_cwd=None)

    assert len(paths_exist.calls) == calls_before + 1


# --- painting -----------------------------------------------------------------

LINK = HoverLink("app.rb", (CellRange(0, 2, 4), CellRange(1, 0, 1)))


def rows_with_ids(ids: list[list[int]]) -> list[Row]:
    return [Row(cells=(("x", 1),) * len(row_ids), hyperlink_ids=tuple(row_ids)) for row_ids in ids]


def test_paint_links_every_range_of_an_unlinked_target() -> None:
    rows = rows_with_ids([[0] * 6, [0] * 6])

    assert paint_operations([LINK], rows, lambda _hid: None) == [
        LinkPaint(0, 2, 4, LINK.url),
        LinkPaint(1, 0, 1, LINK.url),
    ]


def test_paint_leaves_a_target_that_already_carries_its_link() -> None:
    rows = rows_with_ids([[0, 0, 7, 7, 7, 0], [7, 7, 0, 0, 0, 0]])

    assert paint_operations([LINK], rows, {7: LINK.url}.get) == []


def test_paint_clears_the_whole_of_any_link_overlapping_a_target() -> None:
    rows = rows_with_ids([[0, 0, 0, 0, 5, 5], [5, 5, 5, 0, 9, 9]])

    assert paint_operations([LINK], rows, {5: "https://program.example/osc8"}.get) == [
        LinkPaint(0, 4, 5, None),
        LinkPaint(1, 0, 2, None),
        LinkPaint(0, 2, 4, LINK.url),
        LinkPaint(1, 0, 1, LINK.url),
    ]


def test_paint_repaints_a_target_covered_only_partly_by_its_own_link() -> None:
    rows = rows_with_ids([[0, 0, 7, 7, 0, 0], [0] * 6])

    assert paint_operations([LINK], rows, {7: LINK.url}.get) == [
        LinkPaint(0, 2, 3, None),
        LinkPaint(0, 2, 4, LINK.url),
        LinkPaint(1, 0, 1, LINK.url),
    ]


# --- hop:// URLs ---------------------------------------------------------------


def test_hop_url_round_trips_through_parse_hop_url() -> None:
    url = hop_url("open", "Processing UsersController#index")

    assert url == "hop://open/Processing%20UsersController%23index"
    assert parse_hop_url(url) == ("open", "Processing UsersController#index")


def test_hop_url_keeps_paths_readable() -> None:
    assert LINK.url == "hop://open/app.rb"
    assert hop_url("open", "/abs/app.rb:3") == "hop://open//abs/app.rb:3"
    assert parse_hop_url("hop://open//abs/app.rb:3") == ("open", "/abs/app.rb:3")


def test_parse_hop_url_ignores_other_links() -> None:
    assert parse_hop_url("https://example.com") is None
