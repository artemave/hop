from __future__ import annotations

import subprocess

import pytest

from hop.clipboard import ClipboardImage, ClipboardText, default_runner, read
from hop.errors import HopError


class FakeRunner:
    """Maps a ``wl-paste`` argv tuple to a scripted ``CompletedProcess``."""

    def __init__(self, responses: dict[tuple[str, ...], bytes], *, missing: bool = False) -> None:
        self._responses = responses
        self._missing = missing
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> "subprocess.CompletedProcess[bytes]":
        self.calls.append(argv)
        if self._missing:
            raise FileNotFoundError(argv[0])
        stdout = self._responses.get(tuple(argv[1:]), b"")
        returncode = 0 if stdout else 1
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=b"")


def test_read_returns_image_when_clipboard_has_image_type() -> None:
    runner = FakeRunner(
        {
            ("--list-types",): b"image/png\n",
            ("--type", "image/png", "--no-newline"): b"\x89PNG\r\n\x1a\n....",
        }
    )

    result = read(runner=runner)

    assert result == ClipboardImage(b"\x89PNG\r\n\x1a\n....")
    assert ["wl-paste", "--type", "image/png", "--no-newline"] in runner.calls


def test_read_returns_text_when_no_image_type_present() -> None:
    runner = FakeRunner(
        {
            ("--list-types",): b"text/plain\ntext/plain;charset=utf-8\n",
            ("--no-newline",): b"hello world",
        }
    )

    assert read(runner=runner) == ClipboardText("hello world")


def test_read_returns_none_when_image_present_but_empty() -> None:
    runner = FakeRunner({("--list-types",): b"image/png\n"})

    assert read(runner=runner) is None


def test_read_returns_none_when_clipboard_empty() -> None:
    runner = FakeRunner({})

    assert read(runner=runner) is None


def test_read_raises_hoperror_with_install_hint_when_wl_paste_missing() -> None:
    runner = FakeRunner({}, missing=True)

    with pytest.raises(HopError, match="install wl-clipboard"):
        read(runner=runner)


def test_default_runner_shells_out_and_captures() -> None:
    result = default_runner(["printf", "hi"])

    assert result.returncode == 0
    assert result.stdout == b"hi"
