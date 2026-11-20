from __future__ import annotations

import os
from pathlib import Path

import pytest

from hop.hover_preview import image_size
from hop.pdf import PDF_PAGE_PIXELS, first_page_png, is_pdf


def test_is_pdf_reads_the_header(pdf_document: bytes) -> None:
    assert is_pdf(pdf_document)
    assert not is_pdf(b"\x89PNG\r\n\x1a\n")


def test_first_page_png_renders_the_first_page(pdf_document: bytes) -> None:
    png = first_page_png(pdf_document)

    assert png is not None
    assert image_size(png) == (PDF_PAGE_PIXELS, PDF_PAGE_PIXELS // 2)


def test_first_page_png_is_none_for_a_broken_pdf() -> None:
    assert first_page_png(b"%PDF-1.4\nnot really\n") is None


def test_first_page_png_is_none_without_pdftoppm(pdf_document: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")

    assert first_page_png(pdf_document) is None


def test_first_page_png_is_none_when_pdftoppm_hangs(
    pdf_document: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdftoppm = tmp_path / "pdftoppm"
    pdftoppm.write_text("#!/bin/sh\nexec sleep 5\n")
    pdftoppm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr("hop.pdf.PDF_RENDER_TIMEOUT_SECONDS", 0.1)

    assert first_page_png(pdf_document) is None
