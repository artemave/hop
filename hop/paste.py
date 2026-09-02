"""Clipboard-image paste orchestration.

The testable core behind the paste kitten (``hop/kitten/paste/main.py``): read
the host clipboard, and when it holds an image, materialize it into the focused
window's filesystem namespace via ``backend.write_file`` and paste the path.
No kitty / ``boss`` imports so it can be exercised with plain fakes.
"""

from __future__ import annotations

from enum import Enum, auto
from pathlib import Path
from typing import Callable

from hop import clipboard
from hop.backends import SessionBackend
from hop.clipboard import ClipboardContent, ClipboardImage
from hop.session import ProjectSession


class PasteOutcome(Enum):
    IMAGE = auto()
    PASSTHROUGH = auto()


def paste_clipboard_image(
    *,
    session: ProjectSession,
    backend: SessionBackend,
    write_path: str,
    send_text: Callable[[str], None],
    clipboard_read: Callable[[], ClipboardContent | None] = clipboard.read,
) -> PasteOutcome:
    """Materialize a clipboard image at ``write_path`` and paste the path.

    Returns ``PasteOutcome.IMAGE`` when the clipboard held an image (the file
    was written and ``send_text`` called with its path). Any other clipboard
    state — text, empty, unsupported — writes nothing and returns
    ``PasteOutcome.PASSTHROUGH`` for the caller to handle with kitty's native
    ``paste_from_clipboard``.
    """

    content = clipboard_read()
    if not isinstance(content, ClipboardImage):
        return PasteOutcome.PASSTHROUGH
    backend.write_file(session, Path(write_path), content.data)
    send_text(write_path)
    return PasteOutcome.IMAGE
