from __future__ import annotations

from pathlib import Path
from typing import Protocol

from hop.session import ProjectSession, resolve_project_session


class SessionBrowserAdapter(Protocol):
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None: ...


def focus_browser(
    cwd: Path | str,
    *,
    browser: SessionBrowserAdapter,
    url: str | None = None,
) -> ProjectSession:
    session = resolve_project_session(cwd)
    browser.ensure_browser(session, url=url)
    return session
