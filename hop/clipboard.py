"""Host clipboard reads.

hop's controlling process runs inside the host's Sway session, so it reads
the system clipboard directly with ``wl-paste``. Wayland only — a Sway host
has no X11 path to fall back to.

Read-only: the editor's *copy* direction stays on OSC 52 (kitty allows
``write-clipboard`` by default), so hop never needs to write the host
clipboard here.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

from hop.errors import HopError

# The MIME type hop asks ``wl-paste`` for when the clipboard holds an image.
# hop only materializes PNGs; a clipboard image on Wayland is virtually always
# offered as ``image/png``.
_IMAGE_MIME = "image/png"

_MISSING_TOOL_HINT = "wl-paste not found — install wl-clipboard to paste from the clipboard"


@dataclass(frozen=True, slots=True)
class ClipboardImage:
    """Raw image bytes read from the clipboard."""

    data: bytes


@dataclass(frozen=True, slots=True)
class ClipboardText:
    """Decoded text read from the clipboard."""

    text: str


ClipboardContent = ClipboardImage | ClipboardText

# A single ``wl-paste`` invocation. Injected in tests; production uses
# ``default_runner`` (a thin ``subprocess.run`` wrapper).
Runner = Callable[[list[str]], "subprocess.CompletedProcess[bytes]"]


def default_runner(argv: list[str]) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(argv, capture_output=True, check=False)


def _wl_paste(runner: Runner, *args: str) -> "subprocess.CompletedProcess[bytes]":
    try:
        return runner(["wl-paste", *args])
    except FileNotFoundError as exc:
        raise HopError(_MISSING_TOOL_HINT) from exc


def read(*, runner: Runner = default_runner) -> ClipboardContent | None:
    """Return the current clipboard contents, or ``None`` when it is empty.

    An ``image/*`` type on the clipboard yields ``ClipboardImage``; anything
    else is read as text. ``wl-paste`` missing raises ``HopError`` — no
    silent degrade.
    """

    listing = _wl_paste(runner, "--list-types")
    types = (listing.stdout or b"").decode("utf-8", "replace").split()
    if any(mime.startswith("image/") for mime in types):
        result = _wl_paste(runner, "--type", _IMAGE_MIME, "--no-newline")
        data = result.stdout or b""
        return ClipboardImage(data) if data else None
    result = _wl_paste(runner, "--no-newline")
    text = (result.stdout or b"").decode("utf-8", "replace")
    return ClipboardText(text) if text else None
