"""Hover preview: a text file's contents in a floating kitty panel.

With ``[keys].preview`` set, the hover-links watcher previews the file behind
the hover link under the mouse while the mouse moves with that modifier held.
The panel is a layer-shell ``os-panel`` placed next to the link: hop finds the
kitty OS window's content area through sway and adds the link's cell offset.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from hop.config import PreviewSettings
from hop.focused import FilePreview
from hop.hover_links import OPEN_ACTION, parse_hop_url
from hop.session import ProjectSession
from hop.state import SessionState, load_sessions, session_from_state
from hop.sway import SwayWindowPlacement
from hop.targets import SyntacticFileTarget, SyntacticRailsRefTarget, parse_visible_output_target

TAB_WIDTH = 4

_REVERSE = "\x1b[7m"
_REVERSE_OFF = "\x1b[27m"
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
# bat right-aligns line numbers to at least this many digits, then a space.
_BAT_MIN_NUMBER_WIDTH = 4
# The panel's child hides the cursor, turns line wrap off so the terminal cuts
# long lines at the panel's edge, prints the preview file with the preview's
# command, deletes the file and stays alive: the panel closes with its last
# process. The output goes through ``$(…)`` to drop bat's final newline, which
# would scroll the first line out of a panel exactly as tall as the preview.
_PANEL_SCRIPT = (
    'file=$1; shift; printf "\\033[?25l\\033[?7l"; out=$("$@" "$file"); printf %s "$out"; '
    'rm -f "$file"; exec sleep infinity'
)


def session_preview_settings(
    session_name: str,
    *,
    sessions_loader: Callable[[], dict[str, SessionState]] = load_sessions,
    settings_for: Callable[[ProjectSession], PreviewSettings | None] | None = None,
) -> PreviewSettings | None:
    """The session's hover preview settings, or ``None`` when the preview
    is off or the session's state is gone."""

    state = sessions_loader().get(session_name)
    if state is None:
        return None
    if settings_for is None:
        from hop.app import SessionBackendRegistry

        settings_for = SessionBackendRegistry(sessions_loader=sessions_loader).preview_settings_for
    return settings_for(session_from_state(state))


def preview_selection(url: str) -> str | None:
    """The file reference a hover link opens, or ``None`` for any other link."""

    parsed = parse_hop_url(url)
    if parsed is None or parsed[0] != OPEN_ACTION:
        return None
    selection = parsed[1]
    if not isinstance(parse_visible_output_target(selection), SyntacticFileTarget | SyntacticRailsRefTarget):
        return None
    return selection


def link_columns(hyperlink_ids: Sequence[int], x: int) -> tuple[int, int] | None:
    """The first and last column of the hyperlink covering column ``x``."""

    hyperlink_id = hyperlink_ids[x]
    if not hyperlink_id:
        return None
    first = x
    while first > 0 and hyperlink_ids[first - 1] == hyperlink_id:
        first -= 1
    last = x
    while last < len(hyperlink_ids) - 1 and hyperlink_ids[last + 1] == hyperlink_id:
        last += 1
    return first, last


def find_bat() -> str | None:
    """bat's path, under Debian's ``batcat`` name too, or ``None`` without it."""

    return shutil.which("bat") or shutil.which("batcat")


@dataclass(frozen=True, slots=True)
class RenderedPreview:
    """What the panel shows: ``command`` prints the file holding ``text``
    into a ``columns`` × ``lines`` panel."""

    text: str
    lines: int
    columns: int
    command: tuple[str, ...] = ("cat",)


def render_preview(
    preview: FilePreview,
    *,
    bat: str | None,
    max_lines: int,
    max_columns: int,
) -> RenderedPreview:
    """Up to ``max_lines`` numbered lines of the file, the previewed line a
    third of the way down and highlighted.

    With ``bat``, bat prints them syntax-highlighted from the whole file, so
    a construct opened above the excerpt still colors it right. Without it,
    they're plain text with the line in reverse video.
    """

    file_lines = preview.text.splitlines() or [""]
    target = None if preview.line_number is None else max(1, min(preview.line_number, len(file_lines)))
    first = 1 if target is None else max(1, min(target - max_lines // 3, len(file_lines) - max_lines + 1))
    last = min(len(file_lines), first + max_lines - 1)
    if bat is not None:
        gutter = max(_BAT_MIN_NUMBER_WIDTH, len(str(last))) + 1
        widest = max(_width(_printable(line)) for line in file_lines[first - 1 : last])
        columns = min(max_columns, gutter + widest)
        command = (
            bat,
            "--color=always",
            "--decorations=always",
            "--paging=never",
            "--wrap=never",
            "--style=numbers",
            f"--tabs={TAB_WIDTH}",
            f"--terminal-width={columns}",
            f"--line-range={first}:{last}",
            *(() if target is None else (f"--highlight-line={target}",)),
            f"--file-name={preview.path.name}",
            "--",
        )
        return RenderedPreview(text=preview.text, lines=last - first + 1, columns=columns, command=command)
    gutter = len(str(last))
    rows = {
        number: _fit(f"{number:>{gutter}} {_printable(file_lines[number - 1])}", max_columns)
        for number in range(first, last + 1)
    }
    columns = max(_width(row) for row in rows.values())
    text = "\n".join(
        f"{_REVERSE}{row}{' ' * (columns - _width(row))}{_REVERSE_OFF}" if number == target else row
        for number, row in rows.items()
    )
    return RenderedPreview(text=text, lines=len(rows), columns=columns)


def _printable(line: str) -> str:
    return _CONTROL_CHARACTERS.sub("", line.expandtabs(TAB_WIDTH))


def _character_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in "WF" else 1


def _width(text: str) -> int:
    return sum(_character_width(character) for character in text)


def _fit(text: str, columns: int) -> str:
    width = 0
    for index, character in enumerate(text):
        width += _character_width(character)
        if width > columns:
            return text[:index]
    return text


@dataclass(frozen=True, slots=True)
class KittyCells:
    """Where a kitty window's cells sit inside its OS window, in framebuffer
    pixels, and how many sway logical pixels one framebuffer pixel is."""

    left: int
    top: int
    cell_width: int
    cell_height: int
    scale: float


@dataclass(frozen=True, slots=True)
class PanelPlacement:
    output_name: str
    left: int
    top: int


@dataclass(frozen=True, slots=True)
class PreviewRoom:
    below: bool
    lines: int


def preview_room(row: int, window_lines: int) -> PreviewRoom:
    """The side of ``row`` with more of the window's rows, and how many."""

    below = window_lines - row - 1
    return PreviewRoom(below=True, lines=max(below, 1)) if below >= row else PreviewRoom(below=False, lines=row)


def panel_placement(
    window: SwayWindowPlacement,
    cells: KittyCells,
    *,
    column: int,
    row: int,
    columns: int,
    lines: int,
    below: bool,
    window_columns: int,
) -> PanelPlacement:
    """Put a ``columns`` × ``lines`` panel just below or just above the
    cell at ``column``, ``row``, moved left as far as it takes to end inside
    the ``window_columns`` wide window, and kept on the output."""

    cell_width = cells.cell_width * cells.scale
    cell_height = cells.cell_height * cells.scale
    width = columns * cell_width
    height = lines * cell_height
    link_left = window.x + (cells.left + column * cells.cell_width) * cells.scale
    row_top = window.y + (cells.top + row * cells.cell_height) * cells.scale
    row_bottom = row_top + cell_height
    top = max(0, min(row_bottom if below else row_top - height, window.output_height - height))
    window_right = window.x + (cells.left + window_columns * cells.cell_width) * cells.scale
    left = max(0, min(link_left, window_right - width, window.output_width - width))
    return PanelPlacement(output_name=window.output_name, left=round(left), top=round(top))


def panel_background(rgb: int, *, shift: float) -> str:
    """The panel's background: the window's ``0xRRGGBB`` background moved
    ``shift`` percent toward white when it's dark and toward black when it's
    light."""

    channels = [(rgb >> shift) & 0xFF for shift in (16, 8, 0)]
    red, green, blue = channels
    target = 255 if 0.2126 * red + 0.7152 * green + 0.0722 * blue < 128 else 0
    shifted = [round(channel + (target - channel) * shift / 100) for channel in channels]
    return "#" + "".join(f"{channel:02x}" for channel in shifted)


def panel_launch_args(
    placement: PanelPlacement, preview: RenderedPreview, preview_path: Path, *, background: str
) -> list[str]:
    """kitty ``launch`` arguments for the panel showing ``preview_path``."""

    settings = (
        "edge=none",
        "layer=overlay",
        "focus-policy=not-allowed",
        f"output-name={placement.output_name}",
        f"margin-left={placement.left}",
        f"margin-top={placement.top}",
        f"columns={preview.columns}",
        f"lines={preview.lines}",
    )
    options = [argument for setting in settings for argument in ("--os-panel", setting)]
    return [
        "--type=os-panel",
        *options,
        "--color",
        f"background={background}",
        "sh",
        "-c",
        _PANEL_SCRIPT,
        "hop-preview",
        str(preview_path),
        *preview.command,
    ]


def write_preview_file(preview: RenderedPreview) -> Path:
    """A file holding the preview for the panel to print; the panel deletes it."""

    base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "hop"
    base.mkdir(parents=True, exist_ok=True)
    descriptor, path = tempfile.mkstemp(prefix="preview-", suffix=".txt", dir=base)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(preview.text)
    return Path(path)
