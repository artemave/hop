from __future__ import annotations

import shutil
import subprocess

PDF_PAGE_PIXELS = 1200
PDF_RENDER_TIMEOUT_SECONDS = 5.0


def is_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def first_page_png(data: bytes) -> bytes | None:
    """The PDF's first page as a PNG, its longer side ``PDF_PAGE_PIXELS``;
    ``None`` without ``pdftoppm`` or when it can't render the page."""

    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm is None:
        return None
    # The exit status isn't trusted: inside kitty, its child monitor can reap
    # pdftoppm first. A failed render prints no PNG, which the preview drops.
    try:
        result = subprocess.run(
            [pdftoppm, "-png", "-f", "1", "-l", "1", "-singlefile", "-scale-to", str(PDF_PAGE_PIXELS), "-"],
            input=data,
            capture_output=True,
            timeout=PDF_RENDER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    return result.stdout or None
