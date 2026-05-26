from pathlib import Path
from typing import Sequence

import pytest

from hop.commands.open import open_target_in_session
from hop.errors import HopError
from hop.session import ProjectSession


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.focused_sessions: list[str] = []
        self.opened_targets: list[tuple[str, str]] = []

    def focus(self, session: ProjectSession) -> None:
        self.focused_sessions.append(session.session_name)

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        self.opened_targets.append((session.session_name, target))


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        self.calls.append((session.session_name, url))


class StubBackend:
    def __init__(self, *, url_translation: dict[str, str] | None = None) -> None:
        self._url_translation = url_translation or {}
        self.translate_calls: list[str] = []

    def translate_localhost_url(self, _session: ProjectSession, url: str) -> str:
        self.translate_calls.append(url)
        return self._url_translation.get(url, url)

    def paths_exist(self, _session: ProjectSession, paths: Sequence[Path]) -> set[Path]:
        # CLI path doesn't call this — included so the stub fits the SessionBackend Protocol.
        return set()


def test_no_target_focuses_session_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    neovim = StubNeovimAdapter()
    browser = StubBrowserAdapter()

    session = open_target_in_session(project_root, target=None, neovim=neovim, browser=browser)

    assert session.session_name == "demo"
    assert neovim.focused_sessions == ["demo"]
    assert neovim.opened_targets == []
    assert browser.calls == []


def test_file_target_dispatches_to_shared_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    neovim = StubNeovimAdapter()

    open_target_in_session(
        project_root,
        target="app/models/user.rb",
        neovim=neovim,
        browser=StubBrowserAdapter(),
    )

    # CLI passes the path through as typed; nvim resolves it against its own
    # cwd in the session's backend (which the host can't address).
    assert neovim.opened_targets == [("demo", "app/models/user.rb")]
    assert neovim.focused_sessions == []


def test_file_with_line_target_keeps_line_suffix(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    neovim = StubNeovimAdapter()

    open_target_in_session(
        project_root,
        target="app/models/user.rb:42",
        neovim=neovim,
        browser=StubBrowserAdapter(),
    )

    assert neovim.opened_targets == [("demo", "app/models/user.rb:42")]


def test_rails_controller_action_target_translates_to_path_with_def_line(tmp_path: Path) -> None:
    """``hop open UsersController#index`` derives the controller path AND
    looks up the line where ``def index`` is defined via the session
    backend's ``read_file``, so the editor jumps straight to the action."""
    project_root = tmp_path / "demo"
    (project_root / "app/controllers").mkdir(parents=True)
    (project_root / "app/controllers/users_controller.rb").write_text(
        "class UsersController < ApplicationController\n  def index\n  end\nend\n"
    )

    neovim = StubNeovimAdapter()

    open_target_in_session(
        project_root,
        target="UsersController#index",
        neovim=neovim,
        browser=StubBrowserAdapter(),
    )

    # def index is on line 2 of the controller file. The editor target stays
    # relative so the editor (running in the session backend) resolves it
    # against its own cwd, matching how plain file paths flow through.
    assert neovim.opened_targets == [("demo", "app/controllers/users_controller.rb:2")]


def test_rails_controller_action_target_raises_when_def_not_in_file(tmp_path: Path) -> None:
    """If the action isn't defined in the controller, the CLI surfaces a
    clear ``HopError`` rather than silently opening the file at line 1
    (or some unrelated location)."""
    project_root = tmp_path / "demo"
    (project_root / "app/controllers").mkdir(parents=True)
    (project_root / "app/controllers/users_controller.rb").write_text(
        "class UsersController < ApplicationController\n  def show\n  end\nend\n"
    )

    with pytest.raises(HopError, match="could not resolve target"):
        open_target_in_session(
            project_root,
            target="UsersController#index",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )


def test_rails_controller_action_target_raises_when_controller_file_missing(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    with pytest.raises(HopError, match="could not resolve target"):
        open_target_in_session(
            project_root,
            target="UsersController#index",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )


def test_url_target_dispatches_to_session_browser(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    browser = StubBrowserAdapter()

    open_target_in_session(
        project_root,
        target="https://example.com/path",
        neovim=StubNeovimAdapter(),
        browser=browser,
    )

    assert browser.calls == [("demo", "https://example.com/path")]


def test_url_target_is_translated_through_backend(tmp_path: Path) -> None:
    """For container/ssh backends, a localhost URL needs `host_translate` /
    `port_translate` rewriting before it reaches the host browser. The CLI
    routes URLs through the same `backend.translate_localhost_url` the kitten
    uses, so `hop open http://localhost:3000` opens the translated URL."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    browser = StubBrowserAdapter()
    backend = StubBackend(url_translation={"http://localhost:3000/": "http://localhost:35231/"})

    open_target_in_session(
        project_root,
        target="http://localhost:3000/",
        neovim=StubNeovimAdapter(),
        browser=browser,
        session_backend_for=lambda _session: backend,  # type: ignore[arg-type]
    )

    assert backend.translate_calls == ["http://localhost:3000/"]
    assert browser.calls == [("demo", "http://localhost:35231/")]


def test_unparseable_target_raises_hop_error(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    with pytest.raises(HopError, match="could not parse"):
        open_target_in_session(
            project_root,
            target="   ",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )


def test_nested_directories_are_distinct_sessions(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    neovim = StubNeovimAdapter()

    open_target_in_session(project_root, target=None, neovim=neovim, browser=StubBrowserAdapter())
    open_target_in_session(nested_directory, target=None, neovim=neovim, browser=StubBrowserAdapter())

    assert neovim.focused_sessions == ["demo", "src"]
