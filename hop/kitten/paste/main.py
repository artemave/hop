# The `kittens.tui.handler` / `kitty.*` imports only resolve inside kitty's
# bundled Python, where this file actually runs; pyright has no stubs for them.
# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUntypedFunctionDecorator=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from kittens.tui.handler import result_handler

LOGGER_NAME = "hop.paste"
_PASTE_PATH_TEMPLATE = "/tmp/hop-paste-{ns}.png"
# hop only materializes PNGs, and a Wayland clipboard image is virtually always
# offered as ``image/png``; accept any ``image/*`` and prefer png when on offer.
_PREFERRED_IMAGE_MIME = "image/png"


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


def _log_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "hop" / "paste.log"


def _log() -> logging.Logger:
    log = logging.getLogger(LOGGER_NAME)
    if any(getattr(h, "_hop_kitten", False) for h in log.handlers):
        return log
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=128 * 1024, backupCount=2)
    handler._hop_kitten = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


def _first_image_mime(mime_types: tuple[str, ...]) -> str | None:
    images = [mime for mime in mime_types if mime.startswith("image/")]
    if not images:
        return None
    return _PREFERRED_IMAGE_MIME if _PREFERRED_IMAGE_MIME in images else images[0]


def main(args: list[str]) -> str:
    return ""


@result_handler(no_ui=True)
def handle_result(args: list[str], answer: str, target_window_id: int, boss: Any) -> None:
    log = _log()
    window = boss.window_id_map.get(target_window_id)
    if window is None:
        log.info("no window for id %s; nothing to do", target_window_id)
        return
    match = f"--match=id:{window.id}"

    def passthrough() -> None:
        boss.call_remote_control(window, ("action", match, "paste_from_clipboard"))

    try:
        _ensure_hop_importable(args[0])
        # Only the MIME *list* is read in-process here — a safe, non-blocking
        # kitty call. The image bytes are read by the background helper with
        # ``wl-paste`` (a boss-side ``Clipboard.get_mime`` data read silently
        # no-ops outside kitty's OSC-52 server path).
        from kitty.clipboard import Clipboard, ClipboardType

        clipboard = Clipboard(ClipboardType.clipboard)
        mimes = tuple(clipboard.get_available_mime_types_for_paste())
        image_mime = _first_image_mime(mimes)
        log.info("clipboard mimes=%r -> image_mime=%r", mimes, image_mime)
        if image_mime is None:
            passthrough()
            return

        from hop.focused import focused_session_and_backend

        resolved = focused_session_and_backend()
        if resolved is None:
            log.info("no focused hop session; passing through")
            passthrough()
            return
        session_name = resolved[0].session_name

        write_path = _PASTE_PATH_TEMPLATE.format(ns=time.time_ns())
        _launch_helper(boss, window, match, session_name, write_path, image_mime)
    except Exception:
        log.error("paste failed:\n%s", traceback.format_exc())
        try:
            passthrough()
        except Exception:
            log.error("passthrough failed:\n%s", traceback.format_exc())


def _launch_helper(
    boss: Any,
    window: Any,
    match: str,
    session_name: str,
    write_path: str,
    mime: str,
) -> None:
    """Run ``hop __paste-image`` as a kitty-supervised background process.

    The helper reads the clipboard image (``wl-paste``) and writes it into the
    session backend. kitty's ``ChildMonitor`` reaps it (no zombies) and
    ``notify_on_death`` fires back on the boss thread with the exit status, so
    a failed or timed-out step surfaces in the window rather than being
    dropped.
    """

    log = _log()

    def send_text(text: str) -> None:
        boss.call_remote_control(window, ("send-text", match, "--bracketed-paste=auto", text))

    def on_death(exit_code: int, exc: Exception | None) -> None:
        if exc is None and exit_code == 0:
            log.info("paste helper ok; sending path %s", write_path)
            send_text(write_path)
            return
        log.error("paste helper failed: exit=%s exc=%r", exit_code, exc)
        send_text(f"[hop: clipboard image paste failed — see {_log_path()}]")

    hop_exe = shutil.which("hop") or "hop"
    cmd = [hop_exe, "__paste-image", session_name, write_path, mime]
    log.info("launching helper: %r", cmd)
    try:
        boss.run_background_process(cmd, notify_on_death=on_death)
    except Exception:
        log.error("failed to launch paste helper:\n%s", traceback.format_exc())
        send_text(f"[hop: clipboard image paste failed — see {_log_path()}]")
