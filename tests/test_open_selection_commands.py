from pathlib import Path

from hop.commands.open_selection import open_selection_in_window
from hop.kitty import KittyWindowContext
from hop.session import ProjectSession
from hop.state import SessionState


class StubKittyAdapter:
    def __init__(self, context: KittyWindowContext | None) -> None:
        self.context = context
        self.inspected_window_ids: list[int] = []

    def inspect_window(self, window_id: int) -> KittyWindowContext | None:
        self.inspected_window_ids.append(window_id)
        return self.context


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.opened_targets: list[tuple[str, str]] = []

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        self.opened_targets.append((session.session_name, target))


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.urls: list[tuple[str, str | None]] = []

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        self.urls.append((session.session_name, url))


def test_open_selection_in_window_routes_files_to_shared_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    selected_file = terminal_cwd / "app/models/user.rb"
    selected_file.parent.mkdir(parents=True)
    selected_file.write_text("class User\nend\n")

    kitty = StubKittyAdapter(KittyWindowContext(id=17, role="shell", cwd=terminal_cwd.resolve()))
    neovim = StubNeovimAdapter()
    browser = StubBrowserAdapter()

    session = open_selection_in_window(
        "app/models/user.rb:7",
        source_window_id=17,
        kitty=kitty,
        neovim=neovim,
        browser=browser,
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
        listen_on_env="unix:@hop-demo",
    )

    assert session is not None
    assert session.session_name == "demo"
    assert kitty.inspected_window_ids == [17]
    assert neovim.opened_targets == [("demo", f"{selected_file.resolve()}:7")]
    assert browser.urls == []


def test_open_selection_in_window_routes_urls_to_session_browser(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    terminal_cwd.mkdir(parents=True)

    kitty = StubKittyAdapter(KittyWindowContext(id=17, role="shell", cwd=terminal_cwd.resolve()))
    neovim = StubNeovimAdapter()
    browser = StubBrowserAdapter()

    session = open_selection_in_window(
        "https://example.com",
        source_window_id=17,
        kitty=kitty,
        neovim=neovim,
        browser=browser,
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
        listen_on_env="unix:@hop-demo",
    )

    assert session is not None
    assert browser.urls == [("demo", "https://example.com")]
    assert neovim.opened_targets == []


def test_open_selection_in_window_ignores_unresolvable_matches(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    terminal_cwd.mkdir(parents=True)

    kitty = StubKittyAdapter(KittyWindowContext(id=17, role="shell", cwd=terminal_cwd.resolve()))
    neovim = StubNeovimAdapter()
    browser = StubBrowserAdapter()

    session = open_selection_in_window(
        "missing/file.rb:4",
        source_window_id=17,
        kitty=kitty,
        neovim=neovim,
        browser=browser,
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
        listen_on_env="unix:@hop-demo",
    )

    assert session is None
    assert neovim.opened_targets == []
    assert browser.urls == []
