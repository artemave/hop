from __future__ import annotations

import json
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from hop import cli
from hop.link_preview import (
    GITHUB_BODY_CHARACTERS,
    MAX_IMAGE_BYTES,
    LinkCard,
    PageMeta,
    _github_card,  # pyright: ignore[reportPrivateUsage]
    fetch_link_card,
    page_meta,
    run_link_preview_helper,
)
from hop.pdf import first_page_png
from hop.session import ProjectSession
from hop.state import record_session


def _png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height) + b"\x00" * 8


Routes = dict[str, tuple[str, bytes]]


@pytest.fixture
def site() -> Iterator[tuple[str, Routes]]:
    routes: Routes = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in routes:
                self.send_error(404)
                return
            content_type, body = routes[self.path]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", routes
    server.shutdown()
    server.server_close()


def _html(head: str) -> tuple[str, bytes]:
    return "text/html; charset=utf-8", f"<html><head>{head}</head><body><p>body</p></body></html>".encode()


# --- page_meta ---------------------------------------------------------------


def test_page_meta_prefers_opengraph() -> None:
    meta = page_meta(
        "<head><title>Plain</title>"
        '<meta name="description" content="plain description">'
        '<meta property="og:title" content="OG &amp; title">'
        '<meta property="og:description" content="  og\n  description ">'
        '<meta property="og:site_name" content="Example">'
        '<meta property="og:image" content="/card.png"></head>'
    )

    assert meta == PageMeta(
        title="OG & title", description="og description", site_name="Example", image_url="/card.png"
    )


def test_page_meta_falls_back_to_twitter_tags_then_the_plain_head() -> None:
    assert page_meta(
        '<meta name="twitter:title" content="Tweet title"><meta name="twitter:image" content="t.png">'
    ) == PageMeta(title="Tweet title", description=None, site_name=None, image_url="t.png")
    assert page_meta(
        '<title>\n  Plain title\n</title><meta name="Description" content="Plain description">'
    ) == PageMeta(title="Plain title", description="Plain description", site_name=None, image_url=None)


def test_page_meta_skips_empty_values_and_the_body() -> None:
    meta = page_meta(
        '<meta property="og:title" content="  "><meta property="og:description">'
        "<title></title><body><title>Not it</title>"
        '<meta property="og:image" content="body.png"></body>'
    )

    assert meta == PageMeta(title=None, description=None, site_name=None, image_url=None)


# --- fetch_link_card ---------------------------------------------------------


def test_fetch_link_card_reads_the_page_and_its_image(site: tuple[str, Routes]) -> None:
    base, routes = site
    routes["/page"] = _html(
        '<meta property="og:title" content="Title"><meta property="og:description" content="Desc">'
        '<meta property="og:image" content="/img.png">'
    )
    routes["/img.png"] = ("image/png", _png(4, 2))

    assert fetch_link_card(f"{base}/page") == LinkCard(
        site="127.0.0.1", title="Title", description="Desc", image=_png(4, 2)
    )


def test_fetch_link_card_names_the_site_and_keeps_a_page_whose_image_is_gone(site: tuple[str, Routes]) -> None:
    base, routes = site
    routes["/page"] = _html(
        '<meta property="og:site_name" content="Example"><title>Title</title>'
        '<meta property="og:image" content="gone.png">'
    )

    assert fetch_link_card(f"{base}/page") == LinkCard(site="Example", title="Title")


@pytest.mark.parametrize(
    "image",
    [b"not an image", _png(1, 1) + b"\x00" * MAX_IMAGE_BYTES],
)
def test_fetch_link_card_drops_an_unreadable_or_oversized_image(site: tuple[str, Routes], image: bytes) -> None:
    base, routes = site
    routes["/page"] = _html('<title>Title</title><meta property="og:image" content="img">')
    routes["/img"] = ("image/png", image)

    assert fetch_link_card(f"{base}/page") == LinkCard(site="127.0.0.1", title="Title")


def test_fetch_link_card_shows_an_image_url_as_its_image(site: tuple[str, Routes]) -> None:
    base, routes = site
    routes["/img.png"] = ("image/png", _png(3, 3))
    routes["/bad.png"] = ("image/png", b"nope")

    assert fetch_link_card(f"{base}/img.png") == LinkCard(site="127.0.0.1", image=_png(3, 3))
    assert fetch_link_card(f"{base}/bad.png") is None


def test_fetch_link_card_is_none_for_a_page_with_nothing_to_show_or_other_content(site: tuple[str, Routes]) -> None:
    base, routes = site
    routes["/empty"] = _html("")
    routes["/data"] = ("application/json", b"{}")

    assert fetch_link_card(f"{base}/empty") is None
    assert fetch_link_card(f"{base}/data") is None


def test_fetch_link_card_shows_a_pdfs_first_page(
    site: tuple[str, Routes], pdf_document: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, routes = site
    routes["/docs/user%20guide.pdf"] = ("application/pdf", pdf_document)
    routes["/broken.pdf"] = ("application/pdf", b"%PDF-1.4\nnot really\n")

    assert fetch_link_card(f"{base}/docs/user%20guide.pdf") == LinkCard(
        site="127.0.0.1", title="user guide.pdf", image=first_page_png(pdf_document)
    )
    assert fetch_link_card(f"{base}/broken.pdf") is None
    monkeypatch.setattr("hop.link_preview.MAX_PDF_BYTES", len(pdf_document) - 1)
    assert fetch_link_card(f"{base}/docs/user%20guide.pdf") is None


def test_link_card_round_trips_through_json() -> None:
    for card in (LinkCard(site="s", title="t", description="d", image=_png(1, 1)), LinkCard(site="s")):
        assert LinkCard.from_json(card.to_json()) == card


# --- the helper --------------------------------------------------------------


def _record_demo_session(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    record_session(ProjectSession(session_root=project, session_name="demo", workspace_name="s:demo"))


def test_hop_link_preview_writes_the_card(tmp_path: Path, site: tuple[str, Routes]) -> None:
    base, routes = site
    routes["/page"] = _html("<title>Title</title>")
    _record_demo_session(tmp_path)
    write_path = tmp_path / "card.json"

    assert cli.main(["__link-preview", "demo", f"{base}/page", str(write_path)]) == 0

    assert LinkCard.from_json(write_path.read_text()) == LinkCard(site="127.0.0.1", title="Title")


def test_run_link_preview_helper_fails_without_a_card(tmp_path: Path, site: tuple[str, Routes]) -> None:
    base, routes = site
    routes["/empty"] = _html("")
    _record_demo_session(tmp_path)
    write_path = tmp_path / "card.json"

    assert run_link_preview_helper(session_name="demo", url=f"{base}/empty", write_path=str(write_path)) == 1
    assert not write_path.exists()


def test_run_link_preview_helper_reports_a_failed_fetch(
    tmp_path: Path, site: tuple[str, Routes], capsys: pytest.CaptureFixture[str]
) -> None:
    base, _routes = site
    _record_demo_session(tmp_path)

    assert run_link_preview_helper(session_name="demo", url=f"{base}/missing", write_path=str(tmp_path / "x")) == 1
    assert f"hop link preview: {base}/missing: HTTP Error 404" in capsys.readouterr().err


# --- GitHub issues and pull requests -----------------------------------------


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``gh`` on PATH that records its arguments and prints ``gh-out``,
    exiting with the status in ``gh-status``."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(
        f'#!/bin/sh\necho "$@" > {tmp_path}/gh-args\ncat {tmp_path}/gh-out\nexit $(cat {tmp_path}/gh-status)\n'
    )
    script.chmod(0o755)
    (tmp_path / "gh-status").write_text("0")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path


def _issue(**fields: object) -> str:
    issue: dict[str, object] = {
        "title": " Fix  the thing ",
        "state": "open",
        "user": {"login": "octocat"},
        "labels": [],
        "body": None,
    }
    return json.dumps(issue | fields)


def test_fetch_link_card_reads_a_github_issue_with_gh(gh: Path) -> None:
    (gh / "gh-out").write_text(
        _issue(state="closed", labels=[{"name": "bug"}, {"name": "ui"}], body="<!-- template -->\nSteps:\r\n 1. run")
    )

    card = fetch_link_card("https://github.com/acme/private-repo/issues/42#issuecomment-1")

    assert card == LinkCard(
        site="acme/private-repo #42 · Closed · @octocat · bug · ui", title="Fix the thing", description="Steps: 1. run"
    )
    assert (gh / "gh-args").read_text() == "api repos/acme/private-repo/issues/42\n"


@pytest.mark.parametrize(
    ("fields", "state"),
    [
        ({"pull_request": {"merged_at": "2026-11-01T00:00:00Z"}}, "Merged"),
        ({"pull_request": {"merged_at": None}, "draft": True}, "Draft"),
        ({"pull_request": {"merged_at": None}, "draft": False}, "Open"),
    ],
)
def test_fetch_link_card_reads_a_pull_requests_state(gh: Path, fields: dict[str, object], state: str) -> None:
    (gh / "gh-out").write_text(_issue(**fields))

    card = fetch_link_card("https://github.com/acme/app/pull/7/files")

    assert card == LinkCard(site=f"acme/app #7 · {state} · @octocat", title="Fix the thing")


def test_fetch_link_card_cuts_a_long_github_body(gh: Path) -> None:
    (gh / "gh-out").write_text(_issue(body="word " * 200))

    card = fetch_link_card("https://github.com/acme/app/issues/1")

    assert card is not None and card.description is not None
    assert len(card.description) == GITHUB_BODY_CHARACTERS
    assert card.description.endswith("word…")


def test_github_card_is_none_when_gh_cant_read_it(gh: Path) -> None:
    (gh / "gh-out").write_text("")
    (gh / "gh-status").write_text("1")

    assert _github_card("https://github.com/acme/app/pull/7", timeout=5) is None


def test_github_card_is_none_when_gh_hangs(gh: Path) -> None:
    (gh / "gh-out").write_text(_issue())
    (gh / "bin" / "gh").write_text("#!/bin/sh\nexec sleep 5\n")

    assert _github_card("https://github.com/acme/app/pull/7", timeout=0.1) is None


def test_github_card_is_none_for_other_urls_and_without_gh(gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (gh / "gh-out").write_text(_issue())

    assert _github_card("https://github.com/acme/app", timeout=5) is None
    assert _github_card("https://github.com/acme/app/pull/7x", timeout=5) is None
    monkeypatch.setenv("PATH", "")
    assert _github_card("https://github.com/acme/app/pull/7", timeout=5) is None
