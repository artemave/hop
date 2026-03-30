from pathlib import Path

from hop.commands.browser import focus_browser
from hop.session import ProjectSession


class StubSwayAdapter:
    def __init__(self) -> None:
        self.switched_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, Path]] = []

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        self.calls.append((session.session_name, url, session.project_root))


def test_focus_browser_switches_to_workspace_and_routes_url(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    sway = StubSwayAdapter()
    browser = StubBrowserAdapter()

    session = focus_browser(
        nested_directory,
        sway=sway,
        browser=browser,
        url="https://example.com",
    )

    assert session.session_name == "src"
    assert sway.switched_workspaces == [f"p:{nested_directory.name}"]
    assert browser.calls == [("src", "https://example.com", nested_directory)]
