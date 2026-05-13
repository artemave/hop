import logging
from pathlib import Path
from typing import Sequence

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


def test_open_selection_in_window_ignores_files_that_do_not_exist(tmp_path: Path) -> None:
    """Default ``session_backend_for`` returns hop's built-in host backend
    which runs the existence check locally — a file that doesn't exist on
    disk is filtered out and the dispatch is suppressed."""
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


def test_open_selection_in_window_resolves_against_backend_workspace_path(tmp_path: Path) -> None:
    """When the session's persisted backend record carries a ``workspace_path``
    (the cached ``<noninteractive_prefix> pwd`` from bootstrap), relatives
    resolve against it rather than ``source_cwd`` — because for container/ssh
    backends, ``source_cwd`` is kitty's host-side launch directory, not the
    in-backend cwd, and resolving against it would hand the backend a path
    it can't see."""
    from hop.state import CommandBackendRecord

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    selected_file = workspace_path / "app/models/user.rb"
    selected_file.parent.mkdir(parents=True)
    selected_file.write_text("")

    # source_cwd here is the host launch path — should be ignored because
    # the backend has its own workspace_path.
    misleading_source_cwd = tmp_path / "demo"
    misleading_source_cwd.mkdir()

    neovim = StubNeovimAdapter()
    result = open_selection_in_window(
        "app/models/user.rb",
        source_cwd=misleading_source_cwd.resolve(),
        listen_on=session_socket_address("demo"),
        neovim=neovim,
        browser=StubBrowserAdapter(),
        sessions_loader=lambda: {
            "demo": SessionState(
                name="demo",
                project_root=misleading_source_cwd.resolve(),
                backend=CommandBackendRecord(
                    name="devcontainer",
                    interactive_prefix="",
                    noninteractive_prefix="",
                    workspace_path=str(workspace_path.resolve()),
                ),
            ),
        },
    )

    assert result is not None
    assert neovim.opened_targets == [("demo", str(selected_file.resolve()))]


def test_open_selection_in_window_resolves_against_project_root_when_source_cwd_missing(
    tmp_path: Path,
) -> None:
    """No ``source_cwd`` and no backend ``workspace_path`` (host backend) is
    still a usable input: resolve relatives against the session's project
    root rather than refusing. The kitten doesn't always know the in-shell
    cwd, and forcing it to back out would mean clicking marks does nothing."""
    project_root = tmp_path / "demo"
    project_root.mkdir(parents=True)
    (project_root / "app").mkdir()
    (project_root / "app" / "user.rb").write_text("")
    neovim = StubNeovimAdapter()

    result = open_selection_in_window(
        "app/user.rb",
        source_cwd=None,
        listen_on=session_socket_address("demo"),
        neovim=neovim,
        browser=StubBrowserAdapter(),
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
    )

    assert result is not None
    assert neovim.opened_targets == [(result.session_name, str((project_root / "app" / "user.rb").resolve()))]


def test_open_selection_in_window_logs_when_selection_does_not_parse(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    terminal_cwd.mkdir(parents=True)

    with caplog.at_level(logging.INFO, logger="hop.open_selection"):
        result = open_selection_in_window(
            "   ",  # whitespace-only selection — resolve_visible_output_target returns None
            source_cwd=terminal_cwd.resolve(),
            listen_on=session_socket_address("demo"),
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {
                "demo": SessionState(name="demo", project_root=project_root.resolve()),
            },
        )

    assert result is None
    assert any("could not parse" in record.message for record in caplog.records)


def test_open_selection_in_window_logs_when_file_does_not_exist_on_backend(
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
    assert any("does not exist in backend" in record.message for record in caplog.records)


def test_open_selection_in_window_dispatches_path_unchanged_to_nvim(tmp_path: Path) -> None:
    """The kitten resolves candidates against the source window's in-shell
    cwd before they get here; this command must hand the resolved path to
    nvim without further translation (no host-path↔backend-path rewrite)."""
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    selected_file = terminal_cwd / "app/models/user.rb"
    selected_file.parent.mkdir(parents=True)
    selected_file.write_text("class User\nend\n")

    neovim = StubNeovimAdapter()

    class FakeBackend:
        def paths_exist(self, _session: ProjectSession, paths: Sequence[Path]) -> set[Path]:
            return {p for p in paths if p.exists()}

        def translate_localhost_url(self, _session: ProjectSession, url: str) -> str:
            return url

    session = open_selection_in_window(
        "app/models/user.rb",
        source_cwd=terminal_cwd.resolve(),
        listen_on=session_socket_address("demo"),
        neovim=neovim,
        browser=StubBrowserAdapter(),
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
    )

    assert session is not None
    assert neovim.opened_targets == [("demo", str(selected_file.resolve()))]


def test_open_selection_in_window_translates_localhost_url_via_backend(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir(parents=True)

    browser = StubBrowserAdapter()

    class FakeBackend:
        def paths_exist(self, _session: ProjectSession, paths: Sequence[Path]) -> set[Path]:
            return set()

        def translate_localhost_url(self, _session: ProjectSession, url: str) -> str:
            assert url == "http://localhost:3000/foo"
            return "http://localhost:35231/foo"

    session = open_selection_in_window(
        "http://localhost:3000/foo",
        source_cwd=project_root.resolve(),
        listen_on=session_socket_address("demo"),
        neovim=StubNeovimAdapter(),
        browser=browser,
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
        session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
    )

    assert session is not None
    assert browser.urls == [("demo", "http://localhost:35231/foo")]


def test_open_selection_in_window_logs_translated_url_on_dispatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir(parents=True)

    class FakeBackend:
        def paths_exist(self, _session: ProjectSession, paths: Sequence[Path]) -> set[Path]:
            return set()

        def translate_localhost_url(self, _session: ProjectSession, _url: str) -> str:
            return "http://localhost:35231/"

    with caplog.at_level(logging.INFO, logger="hop.open_selection"):
        open_selection_in_window(
            "http://localhost:3000/",
            source_cwd=project_root.resolve(),
            listen_on=session_socket_address("demo"),
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {
                "demo": SessionState(name="demo", project_root=project_root.resolve()),
            },
            session_backend_for=lambda _session: FakeBackend(),  # type: ignore[arg-type]
        )

    # Dispatch log should report the translated URL (post-backend), not the original.
    assert any("'http://localhost:35231/'" in record.message for record in caplog.records)
    assert not any("'http://localhost:3000/'" in record.message for record in caplog.records)


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
