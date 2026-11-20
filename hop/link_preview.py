"""Link card for a hovered web URL: the page's title, description and image.

The hover-links watcher runs on kitty's main thread, so it doesn't fetch:
it launches ``hop __link-preview <session> <url> <write-path>`` with
``boss.run_background_process`` and reads the card that helper writes once
kitty reports it exited.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen

from hop.hover_preview import image_size
from hop.pdf import first_page_png
from hop.state import load_sessions, session_from_state

FETCH_TIMEOUT_SECONDS = 5.0
MAX_PAGE_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (compatible; hop link preview)"
GITHUB_BODY_CHARACTERS = 400

_GITHUB_ISSUE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/(?:pull|issues)/(\d+)(?:[/?#]|$)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True, slots=True)
class LinkCard:
    site: str
    title: str | None = None
    description: str | None = None
    image: bytes | None = None

    def to_json(self) -> str:
        image = None if self.image is None else base64.b64encode(self.image).decode()
        return json.dumps({"site": self.site, "title": self.title, "description": self.description, "image": image})

    @classmethod
    def from_json(cls, text: str) -> LinkCard:
        fields = json.loads(text)
        image = fields["image"]
        return cls(
            site=fields["site"],
            title=fields["title"],
            description=fields["description"],
            image=None if image is None else base64.b64decode(image),
        )


@dataclass(frozen=True, slots=True)
class PageMeta:
    title: str | None
    description: str | None
    site_name: str | None
    image_url: str | None


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.title: str | None = None
        self._title_parts: list[str] | None = None
        self.in_body = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.in_body:
            return
        if tag == "body":
            self.in_body = True
        elif tag == "title" and self.title is None:
            self._title_parts = []
        elif tag == "meta":
            values = dict(attrs)
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content:
                self.meta.setdefault(key.lower(), content)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._title_parts is not None:
            self.title = "".join(self._title_parts)
            self._title_parts = None

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)


def page_meta(html: str) -> PageMeta:
    """What a page's ``<head>`` says about it: OpenGraph first, then Twitter
    card tags, then the plain ``<title>`` and description."""

    parser = _HeadParser()
    parser.feed(html)
    meta = parser.meta

    def first(*keys: str) -> str | None:
        return next((_clean(meta[key]) for key in keys if key in meta and _clean(meta[key])), None)

    return PageMeta(
        title=first("og:title", "twitter:title") or (parser.title and _clean(parser.title)) or None,
        description=first("og:description", "twitter:description", "description"),
        site_name=first("og:site_name"),
        image_url=first("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"),
    )


def _clean(text: str) -> str:
    return " ".join(text.split())


def fetch_link_card(url: str, *, timeout: float = FETCH_TIMEOUT_SECONDS) -> LinkCard | None:
    """The card for ``url``: a GitHub issue or pull request read with ``gh``,
    a page's title, description and image, an image URL's image, or a PDF's
    first page; ``None`` when there's nothing to show."""

    github_card = _github_card(url, timeout=timeout)
    if github_card is not None:
        return github_card
    with urlopen(_request(url, accept="text/html,*/*"), timeout=timeout) as response:
        page_url: str = response.geturl()
        site = urlsplit(page_url).hostname or url
        content_type = response.headers.get_content_type()
        if content_type.startswith("image/"):
            image = _image(response.read(MAX_IMAGE_BYTES + 1))
            return None if image is None else LinkCard(site=site, image=image)
        if content_type == "application/pdf":
            return _pdf_card(response.read(MAX_PDF_BYTES + 1), site=site, page_url=page_url)
        if content_type not in ("text/html", "application/xhtml+xml"):
            return None
        charset = response.headers.get_content_charset() or "utf-8"
        meta = page_meta(response.read(MAX_PAGE_BYTES).decode(charset, errors="replace"))
    image = None if meta.image_url is None else _fetch_image(urljoin(page_url, meta.image_url), timeout=timeout)
    if meta.title is None and meta.description is None and image is None:
        return None
    return LinkCard(site=meta.site_name or site, title=meta.title, description=meta.description, image=image)


def _github_card(url: str, *, timeout: float) -> LinkCard | None:
    """An issue or pull request read with the user's ``gh`` login, so a
    private repo's previews too; ``None`` for another URL, without ``gh``, or
    when ``gh`` can't read it."""

    match = _GITHUB_ISSUE.match(url)
    gh = shutil.which("gh")
    if match is None or gh is None:
        return None
    owner, repo, number = match.groups()
    try:
        result = subprocess.run(
            [gh, "api", f"repos/{owner}/{repo}/issues/{number}"], capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    issue = json.loads(result.stdout)
    labels = [label["name"] for label in issue["labels"]]
    site = " · ".join([f"{owner}/{repo} #{number}", _github_state(issue), f"@{issue['user']['login']}", *labels])
    body = _clean(_HTML_COMMENT.sub("", issue["body"] or ""))
    if len(body) > GITHUB_BODY_CHARACTERS:
        body = body[: GITHUB_BODY_CHARACTERS - 1].rstrip() + "…"
    return LinkCard(site=site, title=_clean(issue["title"]), description=body or None)


def _github_state(issue: dict[str, Any]) -> str:
    pull_request = issue.get("pull_request")
    if pull_request is not None and pull_request["merged_at"] is not None:
        return "Merged"
    if issue.get("draft"):
        return "Draft"
    return issue["state"].capitalize()


def _pdf_card(data: bytes, *, site: str, page_url: str) -> LinkCard | None:
    page = None if len(data) > MAX_PDF_BYTES else first_page_png(data)
    if page is None:
        return None
    return LinkCard(site=site, title=unquote(urlsplit(page_url).path.rsplit("/", 1)[-1]) or None, image=page)


def _request(url: str, *, accept: str) -> Request:
    return Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})


def _fetch_image(url: str, *, timeout: float) -> bytes | None:
    # A page whose image is gone still has its title and description.
    try:
        with urlopen(_request(url, accept="image/*"), timeout=timeout) as response:
            return _image(response.read(MAX_IMAGE_BYTES + 1))
    except OSError:
        return None


def _image(data: bytes) -> bytes | None:
    if len(data) > MAX_IMAGE_BYTES or image_size(data) is None:
        return None
    return data


def _translated(session_name: str, url: str) -> str:
    from hop.app import backend_from_record

    state = load_sessions()[session_name]
    backend = backend_from_record(state.backend, session_root=state.session_root)
    return backend.translate_localhost_url(session_from_state(state), url)


def run_link_preview_helper(
    *,
    session_name: str,
    url: str,
    write_path: str,
) -> int:
    """Write the card for ``url`` at ``write_path``, a localhost URL reached
    the way a click on it reaches the session browser. Exits ``1`` when
    there's no card or the page can't be fetched."""

    try:
        card = fetch_link_card(_translated(session_name, url))
    except OSError as error:
        print(f"hop link preview: {url}: {error}", file=sys.stderr)
        return 1
    if card is None:
        return 1
    Path(write_path).write_text(card.to_json(), encoding="utf-8")
    return 0
