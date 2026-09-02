# The `kittens.tui.handler` import only resolves inside kitty's bundled Python,
# where this file actually runs; pyright has no stubs for it.
# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUntypedFunctionDecorator=false

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from kittens.tui.handler import result_handler

LOGGER_NAME = "hop.paste"
_PASTE_PATH_TEMPLATE = "/tmp/hop-paste-{ns}.png"


def _ensure_hop_importable(kitten_path: str) -> None:
    """Put the directory holding the `hop` package on `sys.path`.

    kitty's bundled build ships a hermetic Python that never sees the
    site-packages `hop` was installed into, and its `map <key> kitten <path>`
    loader execs this module with no `__file__`. But it passes this file's
    absolute path as `args[0]` — and this file always lives at
    `hop/kitten/paste/main.py`, so the package parent is three levels up.
    """

    root = str(Path(kitten_path).resolve().parents[3])
    if root not in sys.path:
        sys.path.insert(0, root)
    # handle_result runs in the long-lived kitty boss, which caches `hop.*` in
    # sys.modules. Inside a kitty boss (C extension loaded), drop the cache so
    # source edits are picked up without a kitty restart.
    if "kitty.fast_data_types" in sys.modules:
        for name in [n for n in list(sys.modules) if n == "hop" or n.startswith("hop.")]:
            sys.modules.pop(name, None)


def _log() -> logging.Logger:
    log = logging.getLogger(LOGGER_NAME)
    if any(getattr(h, "_hop_kitten", False) for h in log.handlers):
        return log
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "hop" / "paste.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=128 * 1024, backupCount=2)
    handler._hop_kitten = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


def main(args: list[str]) -> str:
    return ""


@result_handler(no_ui=True)
def handle_result(args: list[str], answer: str, target_window_id: int, boss: Any) -> None:
    window = boss.window_id_map.get(target_window_id)
    if window is None:
        return
    match = f"--match=id:{window.id}"

    def passthrough() -> None:
        boss.call_remote_control(window, ("action", match, "paste_from_clipboard"))

    try:
        _ensure_hop_importable(args[0])
        from hop import clipboard
        from hop.focused import focused_session_and_backend
        from hop.paste import PasteOutcome, paste_clipboard_image

        resolved = focused_session_and_backend()
        if resolved is None:
            passthrough()
            return
        session, backend = resolved

        def send_text(text: str) -> None:
            boss.call_remote_control(window, ("send-text", match, "--bracketed-paste=auto", text))

        outcome = paste_clipboard_image(
            session=session,
            backend=backend,
            write_path=_PASTE_PATH_TEMPLATE.format(ns=time.time_ns()),
            send_text=send_text,
            clipboard_read=clipboard.read,
        )
        if outcome is PasteOutcome.PASSTHROUGH:
            passthrough()
    except Exception:
        _log().error("paste failed:\n%s", traceback.format_exc())
        try:
            passthrough()
        except Exception:
            _log().error("passthrough failed:\n%s", traceback.format_exc())
