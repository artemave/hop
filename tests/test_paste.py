from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from hop.backends import CommandBackend, SessionBackendError
from hop.clipboard import ClipboardContent, ClipboardImage, ClipboardText
from hop.paste import PasteOutcome, paste_clipboard_image
from hop.session import ProjectSession


def build_session(root: Path) -> ProjectSession:
    return ProjectSession(session_root=root, session_name="demo", workspace_name="p:demo")


class RecordingBackend:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.writes: list[tuple[Path, bytes]] = []
        self._raises = raises

    def write_file(self, session: ProjectSession, path: Path, data: bytes) -> None:
        if self._raises is not None:
            raise self._raises
        self.writes.append((path, data))


def make_reader(content: ClipboardContent | None) -> Callable[[], ClipboardContent | None]:
    return lambda: content


def test_image_writes_file_and_sends_path(tmp_path: Path) -> None:
    backend = RecordingBackend()
    sent: list[str] = []

    outcome = paste_clipboard_image(
        session=build_session(tmp_path),
        backend=backend,  # type: ignore[arg-type]
        write_path="/tmp/hop-paste-1.png",
        send_text=sent.append,
        clipboard_read=make_reader(ClipboardImage(b"PNGBYTES")),
    )

    assert outcome is PasteOutcome.IMAGE
    assert backend.writes == [(Path("/tmp/hop-paste-1.png"), b"PNGBYTES")]
    assert sent == ["/tmp/hop-paste-1.png"]


def test_text_clipboard_is_passthrough(tmp_path: Path) -> None:
    backend = RecordingBackend()
    sent: list[str] = []

    outcome = paste_clipboard_image(
        session=build_session(tmp_path),
        backend=backend,  # type: ignore[arg-type]
        write_path="/tmp/hop-paste-2.png",
        send_text=sent.append,
        clipboard_read=make_reader(ClipboardText("hi")),
    )

    assert outcome is PasteOutcome.PASSTHROUGH
    assert backend.writes == []
    assert sent == []


def test_empty_clipboard_is_passthrough(tmp_path: Path) -> None:
    backend = RecordingBackend()
    sent: list[str] = []

    outcome = paste_clipboard_image(
        session=build_session(tmp_path),
        backend=backend,  # type: ignore[arg-type]
        write_path="/tmp/hop-paste-3.png",
        send_text=sent.append,
        clipboard_read=make_reader(None),
    )

    assert outcome is PasteOutcome.PASSTHROUGH
    assert sent == []


def test_write_file_error_propagates(tmp_path: Path) -> None:
    backend = RecordingBackend(raises=SessionBackendError("boom"))

    with pytest.raises(SessionBackendError, match="boom"):
        paste_clipboard_image(
            session=build_session(tmp_path),
            backend=backend,  # type: ignore[arg-type]
            write_path="/tmp/hop-paste-4.png",
            send_text=lambda _: None,
            clipboard_read=make_reader(ClipboardImage(b"x")),
        )


def test_host_backend_writes_real_file(tmp_path: Path) -> None:
    """End-to-end through a real CommandBackend host backend: the bytes land
    on disk and the path is what ``send_text`` receives."""

    backend = CommandBackend(name="host", interactive_prefix="", noninteractive_prefix="")
    target = tmp_path / "nested" / "shot.png"
    sent: list[str] = []
    payload = bytes(range(256))

    outcome = paste_clipboard_image(
        session=build_session(tmp_path),
        backend=backend,
        write_path=str(target),
        send_text=sent.append,
        clipboard_read=make_reader(ClipboardImage(payload)),
    )

    assert outcome is PasteOutcome.IMAGE
    assert target.read_bytes() == payload
    assert sent == [str(target)]
