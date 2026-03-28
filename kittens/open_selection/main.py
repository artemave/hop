from __future__ import annotations

from typing import Any

from hop.app import build_default_services
from hop.commands.open_selection import open_selection_in_window
from hop.targets import VISIBLE_OUTPUT_TARGET_PATTERN


def mark(text: Any, args: Any, Mark: Any, extra_cli_args: Any, *unused_args: Any) -> Any:
    for index, match in enumerate(VISIBLE_OUTPUT_TARGET_PATTERN.finditer(text)):
        start, end = match.span()
        selected_text = match.group(0).replace("\0", "").replace("\n", "")
        yield Mark(index, start, end, selected_text, {})


def handle_result(  # noqa: PLR0913
    args: Any,
    data: Any,
    target_window_id: Any,
    boss: Any,
    extra_cli_args: Any,
    *unused_args: Any,
) -> None:
    for matched_text in data.get("match", ()):
        if not matched_text:
            continue
        dispatch_selected_match(matched_text, source_window_id=target_window_id)


def dispatch_selected_match(selection: str, *, source_window_id: int) -> None:
    services = build_default_services()
    open_selection_in_window(
        selection,
        source_window_id=source_window_id,
        sway=services.sway,
        kitty=services.kitty,
        neovim=services.neovim,
        browser=services.browser,
    )
