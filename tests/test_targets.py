from pathlib import Path

from hop.targets import ResolvedFileTarget, ResolvedUrlTarget, resolve_visible_output_target


def test_resolve_visible_output_target_keeps_urls_for_browser_dispatch() -> None:
    resolved = resolve_visible_output_target(
        "https://example.com/docs",
        terminal_cwd=Path("/tmp/session/src"),
    )

    assert resolved == ResolvedUrlTarget(url="https://example.com/docs")


def test_resolve_visible_output_target_resolves_relative_paths_against_terminal_cwd(tmp_path: Path) -> None:
    """Resolver only consults ``terminal_cwd`` for relative paths now. The
    file's existence is not its concern — callers (the kitten via
    ``hop.focused.paths_exist``) filter the resolved paths."""
    terminal_cwd = tmp_path / "src"
    terminal_cwd.mkdir(parents=True)

    resolved = resolve_visible_output_target(
        "app/models/user.rb:42",
        terminal_cwd=terminal_cwd,
    )

    assert resolved == ResolvedFileTarget(
        path=(terminal_cwd / "app/models/user.rb").resolve(),
        line_number=42,
    )


def test_resolve_visible_output_target_strips_git_diff_prefixes(tmp_path: Path) -> None:
    terminal_cwd = tmp_path / "src"
    terminal_cwd.mkdir(parents=True)

    resolved = resolve_visible_output_target(
        "b/app/models/user.rb:9",
        terminal_cwd=terminal_cwd,
    )

    assert resolved == ResolvedFileTarget(
        path=(terminal_cwd / "app/models/user.rb").resolve(),
        line_number=9,
    )


def test_resolve_visible_output_target_maps_rails_processing_references(tmp_path: Path) -> None:
    terminal_cwd = tmp_path / "src"
    terminal_cwd.mkdir(parents=True)

    resolved = resolve_visible_output_target(
        "Processing UsersController#index",
        terminal_cwd=terminal_cwd,
    )

    assert resolved == ResolvedFileTarget(
        path=(terminal_cwd / "app/controllers/users_controller.rb").resolve(),
    )


def test_resolve_visible_output_target_returns_path_even_when_file_missing(tmp_path: Path) -> None:
    """Existence filtering moved to ``hop.focused.paths_exist``; the resolver
    returns the shape of the target regardless of whether the file exists on
    disk."""
    terminal_cwd = tmp_path / "src"
    terminal_cwd.mkdir(parents=True)

    resolved = resolve_visible_output_target(
        "app/models/missing.rb:12",
        terminal_cwd=terminal_cwd,
    )

    assert resolved == ResolvedFileTarget(
        path=(terminal_cwd / "app/models/missing.rb").resolve(),
        line_number=12,
    )


def test_resolve_visible_output_target_resolves_bare_directory(tmp_path: Path) -> None:
    terminal_cwd = tmp_path
    resolved = resolve_visible_output_target("app", terminal_cwd=terminal_cwd)

    assert resolved == ResolvedFileTarget(path=(terminal_cwd / "app").resolve())


def test_resolve_visible_output_target_resolves_extensionless_file(tmp_path: Path) -> None:
    terminal_cwd = tmp_path
    resolved = resolve_visible_output_target("Gemfile", terminal_cwd=terminal_cwd)

    assert resolved == ResolvedFileTarget(path=(terminal_cwd / "Gemfile").resolve())


def test_resolve_visible_output_target_resolves_dotfile(tmp_path: Path) -> None:
    terminal_cwd = tmp_path
    resolved = resolve_visible_output_target(".gitignore", terminal_cwd=terminal_cwd)

    assert resolved == ResolvedFileTarget(path=(terminal_cwd / ".gitignore").resolve())


def test_resolve_visible_output_target_parses_python_traceback_line_form(tmp_path: Path) -> None:
    terminal_cwd = tmp_path
    resolved = resolve_visible_output_target('foo.py", line 42', terminal_cwd=terminal_cwd)

    assert resolved == ResolvedFileTarget(path=(terminal_cwd / "foo.py").resolve(), line_number=42)


def test_resolve_visible_output_target_falls_through_when_url_has_no_netloc(tmp_path: Path) -> None:
    """``https://`` parses as a URL with an empty netloc — not a real URL —
    so the function falls through to file resolution."""
    resolved = resolve_visible_output_target("https://", terminal_cwd=tmp_path)

    # No assertion that it returns None or a particular file path; the only
    # invariant is that the empty-netloc URL is not classified as a URL target.
    assert not (resolved is not None and resolved.__class__.__name__ == "ResolvedUrlTarget")
