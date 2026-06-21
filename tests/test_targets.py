from __future__ import annotations

from pathlib import Path
from typing import Sequence

from hop.backends import BackendFileNotFoundError, SessionBackend
from hop.session import ProjectSession
from hop.targets import (
    ResolvedFileTarget,
    ResolvedUrlTarget,
    SyntacticFileTarget,
    SyntacticRailsRefTarget,
    SyntacticUrlTarget,
    parse_visible_output_target,
    resolve_target,
)

# ---------------------------------------------------------------------------
# Parse phase — pure-string syntactic recognition, no I/O.
# ---------------------------------------------------------------------------


def test_parse_returns_none_for_empty_input() -> None:
    assert parse_visible_output_target("   ") is None


def test_parse_recognizes_url() -> None:
    assert parse_visible_output_target("https://example.com/docs") == SyntacticUrlTarget(url="https://example.com/docs")


def test_parse_url_with_no_netloc_is_not_a_url() -> None:
    """``https://`` has scheme but no netloc, so it falls through to file
    parsing instead of being classified as a URL."""
    parsed = parse_visible_output_target("https://")

    assert not isinstance(parsed, SyntacticUrlTarget)


def test_parse_recognizes_file_with_line_suffix() -> None:
    assert parse_visible_output_target("app/models/user.rb:42") == SyntacticFileTarget(
        path_text="app/models/user.rb", line_number=42
    )


def test_parse_recognizes_python_traceback_line_form() -> None:
    assert parse_visible_output_target('foo.py", line 42') == SyntacticFileTarget(path_text="foo.py", line_number=42)


def test_parse_recognizes_extensionless_and_dotfiles() -> None:
    assert parse_visible_output_target("Gemfile") == SyntacticFileTarget(path_text="Gemfile")
    assert parse_visible_output_target(".gitignore") == SyntacticFileTarget(path_text=".gitignore")
    assert parse_visible_output_target("app") == SyntacticFileTarget(path_text="app")


def test_parse_keeps_git_diff_prefix_in_path_text() -> None:
    """Stripping the git-diff prefix happens in the resolve phase (via
    ``resolve_file_candidate``), not in the parser."""
    assert parse_visible_output_target("b/app/models/user.rb:9") == SyntacticFileTarget(
        path_text="b/app/models/user.rb", line_number=9
    )


def test_parse_recognizes_processing_rails_ref() -> None:
    assert parse_visible_output_target("Processing UsersController#index") == SyntacticRailsRefTarget(
        controller="UsersController", action="index"
    )


def test_parse_recognizes_bare_rails_ref() -> None:
    assert parse_visible_output_target("UsersController#show") == SyntacticRailsRefTarget(
        controller="UsersController", action="show"
    )


# ---------------------------------------------------------------------------
# Resolve phase — turns syntactic targets into dispatchable ones. Stub backend
# stands in for the session backend so the file-shaped tests don't shell out.
# ---------------------------------------------------------------------------


class StubBackend:
    """Stand-in for ``SessionBackend`` that serves file content from a dict.

    Production callers go through ``CommandBackend.read_file`` (which shells
    out to ``cat`` inside the backend's namespace). The resolver only cares
    about the return value and the missing-file exception, so a dict-backed
    stub keeps the unit tests off-shell.
    """

    def __init__(self, files: dict[Path, str] | None = None) -> None:
        self._files: dict[Path, str] = files or {}
        self.read_calls: list[Path] = []

    def read_file(self, _session: ProjectSession, path: Path) -> str:
        self.read_calls.append(path)
        content = self._files.get(path)
        if content is None:
            raise BackendFileNotFoundError(f"stub: {path} not found")
        return content

    # The other SessionBackend methods aren't exercised by resolve_target,
    # but we stub them out as no-ops so the Protocol structural check fits.
    @property
    def name(self) -> str:
        return "stub"

    @property
    def interactive_prefix(self) -> str:
        return ""

    @property
    def workspace_path(self) -> str | None:
        return None

    @property
    def teardown_command(self) -> tuple[str, ...] | None:
        return None

    def prepare(self, _session: ProjectSession) -> None:
        return None

    def teardown(self, _session: ProjectSession) -> None:
        return None

    def wrap(self, command: str, _session: ProjectSession) -> Sequence[str]:
        return ("sh", "-c", command)

    def inline(self, command: str, _session: ProjectSession) -> str:
        return command

    def translate_localhost_url(self, _session: ProjectSession, url: str) -> str:
        return url

    def paths_exist(self, _session: ProjectSession, paths: Sequence[Path]) -> set[Path]:
        return {p for p in paths if p in self._files}


def _session(tmp_path: Path) -> ProjectSession:
    return ProjectSession(session_root=tmp_path, session_name="demo", workspace_name="p:demo")


def test_resolve_url_passes_through(tmp_path: Path) -> None:
    syn = SyntacticUrlTarget(url="https://example.com/docs")

    resolved = resolve_target(
        syn, session=_session(tmp_path), backend=_as_backend(StubBackend()), terminal_cwd=tmp_path
    )

    assert resolved == ResolvedUrlTarget(url="https://example.com/docs")


def test_resolve_file_absolutizes_against_terminal_cwd(tmp_path: Path) -> None:
    terminal_cwd = tmp_path / "src"
    terminal_cwd.mkdir(parents=True)
    syn = SyntacticFileTarget(path_text="app/models/user.rb", line_number=42)

    resolved = resolve_target(
        syn, session=_session(tmp_path), backend=_as_backend(StubBackend()), terminal_cwd=terminal_cwd
    )

    assert resolved == ResolvedFileTarget(path=(terminal_cwd / "app/models/user.rb").resolve(), line_number=42)


def test_resolve_file_strips_git_diff_prefix(tmp_path: Path) -> None:
    syn = SyntacticFileTarget(path_text="b/app/models/user.rb", line_number=9)

    resolved = resolve_target(
        syn, session=_session(tmp_path), backend=_as_backend(StubBackend()), terminal_cwd=tmp_path
    )

    assert resolved == ResolvedFileTarget(path=(tmp_path / "app/models/user.rb").resolve(), line_number=9)


def test_resolve_file_with_absolute_path_keeps_path_as_is(tmp_path: Path) -> None:
    """Absolute paths bypass the cwd-join step. Symlinks/`..` are normalized
    via ``Path.resolve``."""
    absolute_file = tmp_path / "README.md"
    absolute_file.write_text("ok\n")
    syn = SyntacticFileTarget(path_text=str(absolute_file))

    resolved = resolve_target(
        syn, session=_session(tmp_path), backend=_as_backend(StubBackend()), terminal_cwd=tmp_path
    )

    assert resolved == ResolvedFileTarget(path=absolute_file.resolve())


def test_resolve_file_with_terminal_cwd_none_keeps_relative_text(tmp_path: Path) -> None:
    """``terminal_cwd=None`` means no shared namespace; the editor resolves
    the path against its own cwd. The CLI uses this branch because hop runs
    on the host but nvim runs in the backend."""
    syn = SyntacticFileTarget(path_text="app/models/user.rb", line_number=42)

    resolved = resolve_target(syn, session=_session(tmp_path), backend=_as_backend(StubBackend()), terminal_cwd=None)

    assert resolved == ResolvedFileTarget(path=Path("app/models/user.rb"), line_number=42)


def test_resolve_rails_ref_finds_def_line(tmp_path: Path) -> None:
    controller_path = (tmp_path / "app/controllers/users_controller.rb").resolve()
    backend = StubBackend(
        {
            controller_path: (
                "class UsersController < ApplicationController\n  def index\n  end\n\n  def show\n  end\nend\n"
            )
        }
    )
    syn = SyntacticRailsRefTarget(controller="UsersController", action="show")

    resolved = resolve_target(syn, session=_session(tmp_path), backend=_as_backend(backend), terminal_cwd=tmp_path)

    assert resolved == ResolvedFileTarget(path=controller_path, line_number=5)
    assert backend.read_calls == [controller_path]


def test_resolve_rails_ref_returns_none_when_def_missing(tmp_path: Path) -> None:
    controller_path = (tmp_path / "app/controllers/users_controller.rb").resolve()
    backend = StubBackend(
        {
            controller_path: "class UsersController < ApplicationController\n  def index\n  end\nend\n",
        }
    )
    syn = SyntacticRailsRefTarget(controller="UsersController", action="destroy")

    assert resolve_target(syn, session=_session(tmp_path), backend=_as_backend(backend), terminal_cwd=tmp_path) is None


def test_resolve_rails_ref_returns_none_when_file_missing(tmp_path: Path) -> None:
    backend = StubBackend()  # no files configured → read_file raises BackendFileNotFoundError
    syn = SyntacticRailsRefTarget(controller="MissingController", action="index")

    assert resolve_target(syn, session=_session(tmp_path), backend=_as_backend(backend), terminal_cwd=tmp_path) is None


def test_resolve_rails_ref_distinguishes_def_prefix_collisions(tmp_path: Path) -> None:
    """``\\b`` after the action name keeps ``def index_action`` from
    matching the ``index`` lookup."""
    controller_path = (tmp_path / "app/controllers/users_controller.rb").resolve()
    backend = StubBackend(
        {
            controller_path: (
                "class UsersController < ApplicationController\n"
                "  def index_action\n"  # line 2 — shouldn't match `index`
                "  end\n"
                "  def index\n"  # line 4 — should match
                "  end\n"
                "end\n"
            )
        }
    )
    syn = SyntacticRailsRefTarget(controller="UsersController", action="index")

    resolved = resolve_target(syn, session=_session(tmp_path), backend=_as_backend(backend), terminal_cwd=tmp_path)

    assert resolved == ResolvedFileTarget(path=controller_path, line_number=4)


def test_resolve_namespaced_rails_controller(tmp_path: Path) -> None:
    controller_path = (tmp_path / "app/controllers/admin/users_controller.rb").resolve()
    backend = StubBackend({controller_path: "  def edit\n  end\n"})
    syn = SyntacticRailsRefTarget(controller="Admin::UsersController", action="edit")

    resolved = resolve_target(syn, session=_session(tmp_path), backend=_as_backend(backend), terminal_cwd=tmp_path)

    assert resolved == ResolvedFileTarget(path=controller_path, line_number=1)


def _as_backend(stub: StubBackend) -> SessionBackend:
    # The StubBackend matches the SessionBackend Protocol structurally; this
    # cast just lets pyright accept the assignment without complaining about
    # nominal type mismatch.
    return stub  # type: ignore[return-value]
