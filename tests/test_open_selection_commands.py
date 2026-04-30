import logging
from pathlib import Path

import pytest

from hop.commands.open_selection import open_selection_in_window
from hop.kitty import session_socket_address
from hop.session import ProjectSession
from hop.state import SessionState


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

    neovim = StubNeovimAdapter()
    browser = StubBrowserAdapter()

    session = open_selection_in_window(
        "app/models/user.rb:7",
        source_cwd=terminal_cwd.resolve(),
        listen_on=session_socket_address("demo"),
        neovim=neovim,
        browser=browser,
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
    )

    assert session is not None
    assert session.session_name == "demo"
    assert neovim.opened_targets == [("demo", f"{selected_file.resolve()}:7")]
    assert browser.urls == []


def test_open_selection_in_window_routes_urls_to_session_browser(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    terminal_cwd.mkdir(parents=True)

    neovim = StubNeovimAdapter()
    browser = StubBrowserAdapter()

    session = open_selection_in_window(
        "https://example.com",
        source_cwd=terminal_cwd.resolve(),
        listen_on=session_socket_address("demo"),
        neovim=neovim,
        browser=browser,
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
    )

    assert session is not None
    assert browser.urls == [("demo", "https://example.com")]
    assert neovim.opened_targets == []


def test_open_selection_in_window_ignores_unresolvable_matches(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    terminal_cwd.mkdir(parents=True)

    neovim = StubNeovimAdapter()
    browser = StubBrowserAdapter()

    session = open_selection_in_window(
        "missing/file.rb:4",
        source_cwd=terminal_cwd.resolve(),
        listen_on=session_socket_address("demo"),
        neovim=neovim,
        browser=browser,
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
    )

    assert session is None
    assert neovim.opened_targets == []
    assert browser.urls == []


def test_open_selection_in_window_logs_when_listen_on_is_not_a_hop_socket(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="hop.open_selection"):
        result = open_selection_in_window(
            "app/models/user.rb",
            source_cwd=tmp_path.resolve(),
            listen_on="unix:@something-else",  # not a hop filesystem socket
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {},
        )

    assert result is None
    assert any("not a hop session socket" in record.message for record in caplog.records)


def test_open_selection_in_window_logs_when_listen_on_is_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="hop.open_selection"):
        result = open_selection_in_window(
            "app/models/user.rb",
            source_cwd=tmp_path.resolve(),
            listen_on=None,
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {},
        )

    assert result is None
    assert any("not a hop session socket" in record.message for record in caplog.records)


def test_open_selection_in_window_logs_when_source_cwd_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir(parents=True)

    with caplog.at_level(logging.INFO, logger="hop.open_selection"):
        result = open_selection_in_window(
            "app/models/user.rb",
            source_cwd=None,
            listen_on=session_socket_address("demo"),
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {
                "demo": SessionState(name="demo", project_root=project_root.resolve()),
            },
        )

    assert result is None
    assert any("source window has no cwd" in record.message for record in caplog.records)


def test_open_selection_in_window_logs_when_target_does_not_resolve(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    terminal_cwd.mkdir(parents=True)

    with caplog.at_level(logging.INFO, logger="hop.open_selection"):
        result = open_selection_in_window(
            "missing/file.rb:4",
            source_cwd=terminal_cwd.resolve(),
            listen_on=session_socket_address("demo"),
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {
                "demo": SessionState(name="demo", project_root=project_root.resolve()),
            },
        )

    assert result is None
    assert any("could not resolve" in record.message for record in caplog.records)


def test_open_selection_in_window_translates_terminal_cwd_via_base(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    selected_file = project_root / "src" / "lib" / "foo.py"
    selected_file.parent.mkdir(parents=True)
    selected_file.write_text("print('hello')\n")

    neovim = StubNeovimAdapter()
    browser = StubBrowserAdapter()

    class FakeBackend:
        def translate_terminal_cwd(self, _session: ProjectSession, cwd: Path) -> Path:
            # Container path /workspace/src maps to <project_root>/src.
            return project_root.resolve() / "src"

    session = open_selection_in_window(
        "lib/foo.py",
        source_cwd=Path("/workspace/src"),
        listen_on=session_socket_address("demo"),
        neovim=neovim,
        browser=browser,
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
    )

    assert session is not None
    assert neovim.opened_targets == [("demo", str(selected_file.resolve()))]


def test_open_selection_in_window_logs_dispatch_on_success(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    selected_file = terminal_cwd / "app/models/user.rb"
    selected_file.parent.mkdir(parents=True)
    selected_file.write_text("class User\nend\n")

    with caplog.at_level(logging.INFO, logger="hop.open_selection"):
        result = open_selection_in_window(
            "app/models/user.rb:7",
            source_cwd=terminal_cwd.resolve(),
            listen_on=session_socket_address("demo"),
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {
                "demo": SessionState(name="demo", project_root=project_root.resolve()),
            },
        )

    assert result is not None
    assert any("dispatching file" in record.message for record in caplog.records)
