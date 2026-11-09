from __future__ import annotations

import struct
from pathlib import Path

import pytest

from hop.config import PreviewSettings
from hop.focused import FilePreview, ImagePreview
from hop.hover_links import hop_url
from hop.hover_preview import (
    KittyCells,
    PanelPlacement,
    PreviewRoom,
    RenderedPreview,
    find_bat,
    image_size,
    link_columns,
    panel_background,
    panel_launch_args,
    panel_placement,
    preview_room,
    preview_selection,
    render_image_preview,
    render_preview,
    session_preview_settings,
    write_preview_file,
)
from hop.session import ProjectSession, resolve_project_session
from hop.state import CommandBackendRecord, SessionState
from hop.sway import SwayWindowPlacement

REVERSE = "\x1b[7m"
REVERSE_OFF = "\x1b[27m"


def _state(session_root: Path, project_config_toml: str | None = None) -> SessionState:
    return SessionState(
        name=resolve_project_session(session_root).session_name,
        session_root=session_root,
        backend=CommandBackendRecord(
            name="host", interactive_prefix="", noninteractive_prefix="", project_config_toml=project_config_toml
        ),
    )


def test_session_preview_settings_asks_for_the_session() -> None:
    asked: list[ProjectSession] = []
    settings = PreviewSettings(modifier="alt", background_shift=5)

    def settings_for(session: ProjectSession) -> PreviewSettings | None:
        asked.append(session)
        return settings

    found = session_preview_settings(
        "demo", sessions_loader=lambda: {"demo": _state(Path("/srv/demo"))}, settings_for=settings_for
    )

    assert found == settings
    assert [session.session_root for session in asked] == [Path("/srv/demo")]


def test_session_preview_settings_reads_the_session_config_by_default(tmp_path: Path) -> None:
    state = _state(tmp_path, project_config_toml='[keys]\npreview = "super"\n[preview]\nbackground_shift = 20\n')

    assert session_preview_settings(state.name, sessions_loader=lambda: {state.name: state}) == PreviewSettings(
        modifier="super", background_shift=20
    )


def test_session_preview_settings_is_none_when_session_state_is_gone() -> None:
    assert session_preview_settings("demo", sessions_loader=lambda: {}) is None


@pytest.mark.parametrize("selection", ["app/models/user.rb:42", "UsersController#show"])
def test_preview_selection_is_the_file_reference_a_link_opens(selection: str) -> None:
    assert preview_selection(hop_url("open", selection)) == selection


@pytest.mark.parametrize(
    "url",
    [
        hop_url("open", "https://example.com/app.rb"),
        hop_url("copy", "app.rb"),
        "https://example.com/app.rb",
        "file:///srv/app.rb",
    ],
)
def test_preview_selection_is_none_for_other_links(url: str) -> None:
    assert preview_selection(url) is None


def test_link_columns_spans_the_hyperlink_under_the_column() -> None:
    ids = (0, 3, 3, 3, 5, 5, 0)

    assert link_columns(ids, 2) == (1, 3)
    assert link_columns(ids, 4) == (4, 5)
    assert link_columns(ids, 6) is None


def test_link_columns_reaches_the_row_edges() -> None:
    assert link_columns((7, 7, 7), 1) == (0, 2)


def _numbered(count: int) -> str:
    return "".join(f"line {n}\n" for n in range(1, count + 1))


def test_find_bat_prefers_bat_then_batcat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("bat", "batcat"):
        (tmp_path / name).write_text("#!/bin/sh\n")
        (tmp_path / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert find_bat() == str(tmp_path / "bat")
    (tmp_path / "bat").unlink()
    assert find_bat() == str(tmp_path / "batcat")
    (tmp_path / "batcat").unlink()
    assert find_bat() is None


def test_render_preview_with_bat_hands_bat_the_whole_file_and_the_excerpt() -> None:
    text = _numbered(30)

    rendered = render_preview(
        FilePreview(Path("src/app.rb"), 10, text), bat="/usr/bin/bat", max_lines=6, max_columns=100
    )

    assert rendered == RenderedPreview(
        content=text,
        lines=6,
        columns=5 + len("line 10"),
        command=(
            "/usr/bin/bat",
            "--color=always",
            "--decorations=always",
            "--paging=never",
            "--wrap=never",
            "--style=numbers",
            "--tabs=4",
            "--terminal-width=12",
            "--line-range=8:13",
            "--highlight-line=10",
            "--file-name=app.rb",
            "--",
        ),
    )


def test_render_preview_with_bat_without_a_line_highlights_nothing() -> None:
    rendered = render_preview(FilePreview(Path("a.rb"), None, _numbered(3)), bat="bat", max_lines=6, max_columns=100)

    assert "--line-range=1:3" in rendered.command
    assert not any(argument.startswith("--highlight-line") for argument in rendered.command)


def test_render_preview_with_bat_widens_the_gutter_past_four_digits_and_caps_columns() -> None:
    text = "".join(f"{'x' * 200 if n == 12000 else 'y'}\n" for n in range(1, 12001))

    rendered = render_preview(FilePreview(Path("a.rb"), 12000, text), bat="bat", max_lines=3, max_columns=100)

    assert rendered.columns == 100
    assert "--line-range=11998:12000" in rendered.command


def test_render_preview_with_bat_sizes_by_expanded_tabs_and_wide_characters() -> None:
    rendered = render_preview(FilePreview(Path("a.rb"), None, "\t日本\n"), bat="bat", max_lines=3, max_columns=100)

    assert rendered.columns == 5 + 4 + 4


def test_render_preview_puts_the_line_a_third_of_the_way_down_highlighted() -> None:
    rendered = render_preview(FilePreview(Path("a.rb"), 10, _numbered(30)), bat=None, max_lines=6, max_columns=40)

    assert rendered == RenderedPreview(
        content=" 8 line 8\n 9 line 9\n" + f"{REVERSE}10 line 10{REVERSE_OFF}\n" + "11 line 11\n12 line 12\n13 line 13",
        lines=6,
        columns=10,
    )


def test_render_preview_starts_at_the_top_without_a_line() -> None:
    rendered = render_preview(FilePreview(Path("a.rb"), None, _numbered(5)), bat=None, max_lines=3, max_columns=40)

    assert rendered.content == "1 line 1\n2 line 2\n3 line 3"


def test_render_preview_fills_the_panel_near_the_end_of_the_file() -> None:
    rendered = render_preview(FilePreview(Path("a.rb"), 99, _numbered(5)), bat=None, max_lines=3, max_columns=40)

    assert rendered.content == f"3 line 3\n4 line 4\n{REVERSE}5 line 5{REVERSE_OFF}"


def test_render_preview_pads_the_highlight_to_the_widest_row() -> None:
    rendered = render_preview(FilePreview(Path("a.rb"), 1, "x\nlonger\n"), bat=None, max_lines=5, max_columns=40)

    assert rendered.content == f"{REVERSE}1 x     {REVERSE_OFF}\n2 longer"
    assert rendered.columns == 8


def test_render_preview_expands_tabs_and_drops_control_characters() -> None:
    rendered = render_preview(
        FilePreview(Path("a.rb"), None, "\tx\x1b[31m\x07\r\n"), bat=None, max_lines=5, max_columns=40
    )

    assert rendered.content == "1     x[31m"


def test_render_preview_cuts_rows_at_the_column_limit_counting_wide_characters() -> None:
    rendered = render_preview(
        FilePreview(Path("a.rb"), None, "日本語テキスト\nabcdef\n"), bat=None, max_lines=5, max_columns=8
    )

    assert rendered.content == "1 日本語\n2 abcdef"
    assert rendered.columns == 8


def test_render_preview_counts_combining_characters_as_zero_width() -> None:
    rendered = render_preview(FilePreview(Path("a.rb"), None, "éé\n"), bat=None, max_lines=5, max_columns=40)

    assert rendered.columns == 4


def test_render_preview_of_an_empty_file_is_one_empty_line() -> None:
    rendered = render_preview(FilePreview(Path("a.rb"), 3, ""), bat=None, max_lines=5, max_columns=40)

    assert rendered == RenderedPreview(content=f"{REVERSE}1 {REVERSE_OFF}", lines=1, columns=2)


_WINDOW = SwayWindowPlacement(output_name="DP-2", output_width=1000, output_height=800, x=100, y=50)
_CELLS = KittyCells(left=4, top=4, cell_width=10, cell_height=20, scale=1.0)


def test_preview_room_is_below_the_link_when_more_rows_are_below() -> None:
    assert preview_room(3, 24) == PreviewRoom(below=True, lines=20)
    assert preview_room(11, 24) == PreviewRoom(below=True, lines=12)


def test_preview_room_is_above_the_link_when_more_rows_are_above() -> None:
    assert preview_room(12, 24) == PreviewRoom(below=False, lines=12)
    assert preview_room(23, 24) == PreviewRoom(below=False, lines=23)


def test_preview_room_has_a_line_in_a_one_row_window() -> None:
    assert preview_room(0, 1) == PreviewRoom(below=True, lines=1)


def test_panel_placement_goes_just_below_the_link() -> None:
    placement = panel_placement(_WINDOW, _CELLS, column=3, row=2, columns=20, lines=5, below=True, window_columns=100)

    assert placement == PanelPlacement(output_name="DP-2", left=134, top=114)


def test_panel_placement_goes_just_above_the_link() -> None:
    placement = panel_placement(_WINDOW, _CELLS, column=3, row=34, columns=20, lines=5, below=False, window_columns=100)

    assert placement.top == 734 - 100


def test_panel_placement_ends_inside_the_window() -> None:
    placement = panel_placement(_WINDOW, _CELLS, column=30, row=2, columns=20, lines=5, below=True, window_columns=40)

    assert placement.left == 100 + 4 + 400 - 200


def test_panel_placement_stays_on_the_output() -> None:
    near_the_right = panel_placement(
        _WINDOW, _CELLS, column=85, row=2, columns=20, lines=5, below=True, window_columns=100
    )
    tall_below = panel_placement(_WINDOW, _CELLS, column=3, row=2, columns=20, lines=40, below=True, window_columns=100)
    tall_above = panel_placement(
        _WINDOW, _CELLS, column=3, row=2, columns=20, lines=40, below=False, window_columns=100
    )

    assert near_the_right.left == 1000 - 200
    assert tall_below.top == 0
    assert tall_above.top == 0


def test_panel_placement_converts_framebuffer_pixels_to_logical() -> None:
    cells = KittyCells(left=8, top=8, cell_width=20, cell_height=40, scale=0.5)

    placement = panel_placement(_WINDOW, cells, column=3, row=2, columns=20, lines=5, below=True, window_columns=100)

    assert placement == PanelPlacement(output_name="DP-2", left=134, top=114)


def test_panel_background_lightens_a_dark_background() -> None:
    assert panel_background(0x000000, shift=10) == "#1a1a1a"
    assert panel_background(0x282C34, shift=10) == "#3e4148"


def test_panel_background_darkens_a_light_background() -> None:
    assert panel_background(0xFFFFFF, shift=10) == "#e6e6e6"
    assert panel_background(0xFDF6E3, shift=10) == "#e4ddcc"


def test_panel_background_moves_by_the_shift() -> None:
    assert panel_background(0x000000, shift=0) == "#000000"
    assert panel_background(0x000000, shift=50) == "#808080"


def test_panel_launch_args_place_a_focusless_panel_showing_the_file() -> None:
    args = panel_launch_args(
        PanelPlacement(output_name="DP-2", left=134, top=114),
        RenderedPreview(content="x", lines=6, columns=40),
        Path("/run/user/1000/hop/preview-1.txt"),
        background="#1a1a1a",
    )

    assert args == [
        "--type=os-panel",
        *("--os-panel", "edge=none"),
        *("--os-panel", "layer=overlay"),
        *("--os-panel", "focus-policy=not-allowed"),
        *("--os-panel", "output-name=DP-2"),
        *("--os-panel", "margin-left=134"),
        *("--os-panel", "margin-top=114"),
        *("--os-panel", "columns=40"),
        *("--os-panel", "lines=6"),
        *("--color", "background=#1a1a1a"),
        "sh",
        "-c",
        'file=$1; shift; printf "\\033[?25l\\033[?7l"; out=$("$@" "$file"); printf %s "$out"; '
        'rm -f "$file"; exec sleep infinity',
        "hop-preview",
        "/run/user/1000/hop/preview-1.txt",
        "cat",
    ]


def test_panel_launch_args_run_an_image_without_capturing_its_output() -> None:
    rendered = render_image_preview(
        ImagePreview(Path("a.png"), _png(10, 20)),
        kitten="kitten",
        max_lines=5,
        max_columns=5,
        cell_width=10,
        cell_height=20,
    )
    assert rendered is not None

    args = panel_launch_args(
        PanelPlacement(output_name="DP-2", left=0, top=0), rendered, Path("/run/preview-1"), background="#000000"
    )

    assert args[-10:] == [
        "sh",
        "-c",
        'file=$1; shift; printf "\\033[?25l"; "$@" "$file"; rm -f "$file"; exec sleep infinity',
        "hop-preview",
        "/run/preview-1",
        "kitten",
        "icat",
        "--transfer-mode=stream",
        "--align=left",
        "--place=1x1@0x0",
    ]


def test_write_preview_file_writes_an_image_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    data = _png(1, 1) + b"\xff\x00"

    path = write_preview_file(RenderedPreview(content=data, lines=1, columns=1))

    assert path.read_bytes() == data


def test_write_preview_file_writes_into_the_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    path = write_preview_file(RenderedPreview(content="日本 x", lines=1, columns=7))

    assert path.parent == tmp_path / "hop"
    assert path.read_text(encoding="utf-8") == "日本 x"


# --- images ------------------------------------------------------------------


def _png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"


def _jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 6) + b"JFIF"
    huffman = b"\xff\xc4" + struct.pack(">H", 3) + b"\x00"
    frame = b"\xff\xc2" + struct.pack(">HBHH", 17, 8, height, width)
    return b"\xff\xd8" + app0 + huffman + frame + b"\x03" + b"\x00" * 9


def _webp(chunk: bytes, payload: bytes) -> bytes:
    return (
        b"RIFF" + struct.pack("<I", 4 + 8 + len(payload)) + b"WEBP" + chunk + struct.pack("<I", len(payload)) + payload
    )


def test_image_size_reads_png_and_gif_headers() -> None:
    assert image_size(_png(640, 480)) == (640, 480)
    assert image_size(b"GIF89a" + struct.pack("<HH", 32, 16) + b"\x00") == (32, 16)
    assert image_size(b"GIF87a" + struct.pack("<HH", 1, 2) + b"\x00") == (1, 2)


def test_image_size_reads_the_jpeg_frame_past_other_segments() -> None:
    assert image_size(_jpeg(1920, 1080)) == (1920, 1080)


def test_image_size_is_none_for_a_jpeg_without_a_frame() -> None:
    assert image_size(b"\xff\xd8\xff\xe0" + struct.pack(">H", 4) + b"JF" + b"\x00" * 9) is None


def test_image_size_reads_each_webp_flavour() -> None:
    lossy = _webp(b"VP8 ", b"\x00" * 6 + struct.pack("<HH", 0x4000 | 300, 200) + b"\x00\x00")
    lossless_bits = (300 - 1) | ((200 - 1) << 14)
    lossless = _webp(b"VP8L", b"\x2f" + lossless_bits.to_bytes(4, "little"))
    extended = _webp(b"VP8X", b"\x00" * 4 + (300 - 1).to_bytes(3, "little") + (200 - 1).to_bytes(3, "little"))

    assert image_size(lossy) == (300, 200)
    assert image_size(lossless) == (300, 200)
    assert image_size(extended) == (300, 200)


def test_image_size_is_none_for_other_formats() -> None:
    assert image_size(_webp(b"ALPH", b"\x00" * 16)) is None
    assert image_size(b"BM" + b"\x00" * 30) is None


def test_render_image_preview_keeps_a_small_image_at_its_own_size() -> None:
    rendered = render_image_preview(
        ImagePreview(Path("a.png"), _png(35, 30)),
        kitten="/k",
        max_lines=10,
        max_columns=40,
        cell_width=10,
        cell_height=20,
    )

    assert rendered is not None
    assert (rendered.columns, rendered.lines) == (4, 2)
    assert rendered.command == ("/k", "icat", "--transfer-mode=stream", "--align=left", "--place=4x2@0x0")
    assert rendered.content == _png(35, 30)


def test_render_image_preview_scales_a_large_image_down_to_the_room() -> None:
    rendered = render_image_preview(
        ImagePreview(Path("a.png"), _png(1000, 1000)),
        kitten="/k",
        max_lines=10,
        max_columns=40,
        cell_width=10,
        cell_height=20,
    )

    assert rendered is not None
    assert (rendered.columns, rendered.lines) == (20, 10)


def test_render_image_preview_is_none_for_an_image_of_unknown_size() -> None:
    rendered = render_image_preview(
        ImagePreview(Path("a.bmp"), b"BM" + b"\x00" * 30),
        kitten="/k",
        max_lines=10,
        max_columns=40,
        cell_width=10,
        cell_height=20,
    )

    assert rendered is None
