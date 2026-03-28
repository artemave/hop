from __future__ import annotations

from hop.app import build_default_services
from hop.commands.open_selection import open_selection_in_window
from hop.targets import VISIBLE_OUTPUT_TARGET_PATTERN


def mark(text, args, Mark, extra_cli_args, *unused_args):  # noqa: ANN001
    for index, match in enumerate(VISIBLE_OUTPUT_TARGET_PATTERN.finditer(text)):
        start, end = match.span()
        selected_text = match.group(0).replace("\0", "").replace("\n", "")
        yield Mark(index, start, end, selected_text, {})


def handle_result(args, data, target_window_id, boss, extra_cli_args, *unused_args):  # noqa: ANN001
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
