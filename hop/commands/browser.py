from __future__ import annotations

from pathlib import Path
from typing import Protocol

from hop.session import ProjectSession, resolve_project_session


class BrowserSwayAdapter(Protocol):
    def switch_to_workspace(self, workspace_name: str) -> None: ...


class SessionBrowserAdapter(Protocol):
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None: ...


def focus_browser(
    cwd: Path | str,
    *,
    sway: BrowserSwayAdapter,
    browser: SessionBrowserAdapter,
    url: str | None = None,
) -> ProjectSession:
    session = resolve_project_session(cwd)
    sway.switch_to_workspace(session.workspace_name)
    browser.ensure_browser(session, url=url)
    return session
