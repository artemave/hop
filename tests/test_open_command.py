from pathlib import Path
from typing import Sequence

import pytest

from hop.commands.open import open_target_in_session
from hop.errors import HopError
from hop.session import ProjectSession


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.opened_targets: list[tuple[str, str]] = []

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


def test_file_target_dispatches_to_shared_editor(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    neovim = StubNeovimAdapter()

    session = open_target_in_session(
        session_root,
        target="app/models/user.rb",
        neovim=neovim,
        browser=StubBrowserAdapter(),
    )

    # CLI passes the path through as typed; nvim resolves it against its own
    # cwd in the session's backend (which the host can't address).
    assert session.session_name == "demo"
    assert neovim.opened_targets == [("demo", "app/models/user.rb")]


def test_file_with_line_target_keeps_line_suffix(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    neovim = StubNeovimAdapter()

    open_target_in_session(
        session_root,
        target="app/models/user.rb:42",
        neovim=neovim,
        browser=StubBrowserAdapter(),
    )

    assert neovim.opened_targets == [("demo", "app/models/user.rb:42")]


def test_rails_controller_action_target_translates_to_path_with_def_line(tmp_path: Path) -> None:
    """``hop open UsersController#index`` derives the controller path AND
    looks up the line where ``def index`` is defined via the session
    backend's ``read_file``, so the editor jumps straight to the action."""
    session_root = tmp_path / "demo"
    (session_root / "app/controllers").mkdir(parents=True)
    (session_root / "app/controllers/users_controller.rb").write_text(
        "class UsersController < ApplicationController\n  def index\n  end\nend\n"
    )

    neovim = StubNeovimAdapter()

    open_target_in_session(
        session_root,
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
    session_root = tmp_path / "demo"
    (session_root / "app/controllers").mkdir(parents=True)
    (session_root / "app/controllers/users_controller.rb").write_text(
        "class UsersController < ApplicationController\n  def show\n  end\nend\n"
    )

    with pytest.raises(HopError, match="could not resolve target"):
        open_target_in_session(
            session_root,
            target="UsersController#index",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )


def test_rails_controller_action_target_raises_when_controller_file_missing(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    with pytest.raises(HopError, match="could not resolve target"):
        open_target_in_session(
            session_root,
            target="UsersController#index",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )


def test_url_target_dispatches_to_session_browser(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    browser = StubBrowserAdapter()

    open_target_in_session(
        session_root,
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
    session_root = tmp_path / "demo"
    session_root.mkdir()

    browser = StubBrowserAdapter()
    backend = StubBackend(url_translation={"http://localhost:3000/": "http://localhost:35231/"})

    open_target_in_session(
        session_root,
        target="http://localhost:3000/",
        neovim=StubNeovimAdapter(),
        browser=browser,
        session_backend_for=lambda _session: backend,  # type: ignore[arg-type]
    )

    assert backend.translate_calls == ["http://localhost:3000/"]
    assert browser.calls == [("demo", "http://localhost:35231/")]


def test_unparseable_target_raises_hop_error(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    with pytest.raises(HopError, match="could not parse"):
        open_target_in_session(
            session_root,
            target="   ",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )


def test_nested_directories_are_distinct_sessions(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    nested_directory = session_root / "src"
    nested_directory.mkdir(parents=True)

    neovim = StubNeovimAdapter()

    open_target_in_session(session_root, target="lib/a.rb", neovim=neovim, browser=StubBrowserAdapter())
    open_target_in_session(nested_directory, target="lib/b.rb", neovim=neovim, browser=StubBrowserAdapter())

    assert neovim.opened_targets == [("demo", "lib/a.rb"), ("src", "lib/b.rb")]


# ─── open_handlers: binary files route through the system handler ─────────────


class RecordingHandlerRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run(self, session: ProjectSession, backend: object, *, command: str) -> None:
        del backend
        self.calls.append((session.session_name, command))


_DEFAULT_HANDLERS: tuple[tuple[str, str], ...] = (
    ("*.png", "xdg-open {path}"),
    ("*.pdf", "xdg-open {path}"),
    ("*.tar.gz", "xdg-open {path}"),
)


@pytest.mark.parametrize(
    "filename",
    [
        "config.json",
        "docker-compose.yaml",
        "Cargo.toml",
        "app/models/user.rb",
        "Makefile",
        "Dockerfile",
        "notes.md",
        "README",
        "icon.svg",
        "src/main.rs",
        "weird.unknownextension",
    ],
)
def test_text_and_source_files_dispatch_to_nvim(tmp_path: Path, filename: str) -> None:
    """The classifier is an allowlist of known-binary extensions. JSON,
    YAML, TOML, Markdown, SVG, source code, and files without an
    extension all fall through to the editor — this is the guarantee
    users rely on for normal editing flow."""
    session_root = tmp_path / "demo"
    session_root.mkdir()

    neovim = StubNeovimAdapter()
    runner = RecordingHandlerRunner()

    open_target_in_session(
        session_root,
        target=filename,
        neovim=neovim,
        browser=StubBrowserAdapter(),
        handlers_for_session=lambda _session: _DEFAULT_HANDLERS,
        handler_runner=runner,
    )

    assert neovim.opened_targets == [("demo", filename)]
    assert runner.calls == []


def test_png_file_dispatches_to_open_handler(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    neovim = StubNeovimAdapter()
    runner = RecordingHandlerRunner()

    open_target_in_session(
        session_root,
        target="public/email-logo.png",
        neovim=neovim,
        browser=StubBrowserAdapter(),
        handlers_for_session=lambda _session: _DEFAULT_HANDLERS,
        handler_runner=runner,
    )

    assert neovim.opened_targets == []
    assert runner.calls == [("demo", "xdg-open public/email-logo.png")]


def test_compound_extension_archive_matches(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    runner = RecordingHandlerRunner()
    open_target_in_session(
        session_root,
        target="dist/release.tar.gz",
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        handlers_for_session=lambda _session: _DEFAULT_HANDLERS,
        handler_runner=runner,
    )

    assert runner.calls == [("demo", "xdg-open dist/release.tar.gz")]


def test_user_can_override_default_handler(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    overridden = (("*.png", "feh {path}"),)
    runner = RecordingHandlerRunner()

    open_target_in_session(
        session_root,
        target="logo.png",
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        handlers_for_session=lambda _session: overridden,
        handler_runner=runner,
    )

    assert runner.calls == [("demo", "feh logo.png")]


def test_empty_template_opts_a_default_back_to_nvim(tmp_path: Path) -> None:
    """``*.png = ""`` removes png from the handler set. Useful when a user
    actively wants to edit pixel data in nvim (rare) or to disable the
    default for a project that ships a different viewer."""
    session_root = tmp_path / "demo"
    session_root.mkdir()

    opt_out = (("*.png", ""),)
    neovim = StubNeovimAdapter()
    runner = RecordingHandlerRunner()

    open_target_in_session(
        session_root,
        target="logo.png",
        neovim=neovim,
        browser=StubBrowserAdapter(),
        handlers_for_session=lambda _session: opt_out,
        handler_runner=runner,
    )

    assert neovim.opened_targets == [("demo", "logo.png")]
    assert runner.calls == []


def test_handler_command_shell_quotes_path(tmp_path: Path) -> None:
    """Paths with spaces or shell metacharacters must be shell-quoted into
    the template, otherwise the launched viewer sees garbage arguments."""
    session_root = tmp_path / "demo"
    session_root.mkdir()

    runner = RecordingHandlerRunner()
    open_target_in_session(
        session_root,
        target="assets/weird name.png",
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        handlers_for_session=lambda _session: _DEFAULT_HANDLERS,
        handler_runner=runner,
    )

    assert runner.calls == [("demo", "xdg-open 'assets/weird name.png'")]


def test_subprocess_runner_wraps_command_through_backend_inline(tmp_path: Path) -> None:
    """The default runner sends the handler through ``backend.inline``, so
    a non-host backend (devcontainer / ssh) executes the viewer inside the
    backend's exec context rather than on the host. Exercise the real
    Popen path with a command whose side effect we can observe."""
    import time

    from hop.backends import CommandBackend
    from hop.commands.open import SubprocessOpenHandlerRunner
    from hop.session import resolve_project_session

    session_root = tmp_path / "demo"
    session_root.mkdir()
    marker = tmp_path / "marker"
    # interactive_prefix wraps each command — we want the inline-wrapped
    # form to write a marker that includes the prefix's effect, proving the
    # prefix actually ran. `env FOO=bar` prepended in front means the
    # command that runs is `env FOO=bar sh -c '... write marker ...'`,
    # and the marker captures `$FOO` so we know the prefix took effect.
    backend = CommandBackend(
        name="prefixed",
        interactive_prefix="env HOP_OPEN_HANDLER_TEST=ran",
        noninteractive_prefix="env HOP_OPEN_HANDLER_TEST=ran",
    )
    session = resolve_project_session(session_root)

    runner = SubprocessOpenHandlerRunner()
    runner.run(session, backend, command=f"sh -c 'printf %s \"$HOP_OPEN_HANDLER_TEST\" > {marker}'")

    # Popen is fire-and-forget; give the child a moment to actually write.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.02)
    assert marker.read_text() == "ran"
