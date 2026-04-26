from pathlib import Path

from hop.commands.browser import focus_browser
from hop.session import ProjectSession


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, Path]] = []

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        self.calls.append((session.session_name, url, session.project_root))


def test_focus_browser_routes_url_to_session_browser(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    browser = StubBrowserAdapter()

    session = focus_browser(
        nested_directory,
        browser=browser,
        url="https://example.com",
    )

    assert session.session_name == "src"
    assert browser.calls == [("src", "https://example.com", nested_directory)]
