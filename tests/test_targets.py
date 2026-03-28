from pathlib import Path

from hop.targets import ResolvedFileTarget, ResolvedUrlTarget, resolve_visible_output_target


def test_resolve_visible_output_target_keeps_urls_for_browser_dispatch() -> None:
    resolved = resolve_visible_output_target(
        "https://example.com/docs",
        terminal_cwd=Path("/tmp/session/src"),
        project_root=Path("/tmp/session"),
    )

    assert resolved == ResolvedUrlTarget(url="https://example.com/docs")


def test_resolve_visible_output_target_prefers_terminal_cwd_for_relative_paths(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    project_root_file = project_root / "app/models/user.rb"
    terminal_file = terminal_cwd / "app/models/user.rb"
    project_root_file.parent.mkdir(parents=True)
    terminal_file.parent.mkdir(parents=True)
    project_root_file.write_text("project\n")
    terminal_file.write_text("terminal\n")

    resolved = resolve_visible_output_target(
        "app/models/user.rb:42",
        terminal_cwd=terminal_cwd,
        project_root=project_root,
    )

    assert resolved == ResolvedFileTarget(path=terminal_file.resolve(), line_number=42)


def test_resolve_visible_output_target_falls_back_to_project_root(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    resolved_file = project_root / "app/models/user.rb"
    terminal_cwd.mkdir(parents=True)
    resolved_file.parent.mkdir(parents=True)
    resolved_file.write_text("project\n")

    resolved = resolve_visible_output_target(
        "app/models/user.rb",
        terminal_cwd=terminal_cwd,
        project_root=project_root,
    )

    assert resolved == ResolvedFileTarget(path=resolved_file.resolve())


def test_resolve_visible_output_target_strips_git_diff_prefixes(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    resolved_file = project_root / "app/models/user.rb"
    terminal_cwd.mkdir(parents=True)
    resolved_file.parent.mkdir(parents=True)
    resolved_file.write_text("project\n")

    resolved = resolve_visible_output_target(
        "b/app/models/user.rb:9",
        terminal_cwd=terminal_cwd,
        project_root=project_root,
    )

    assert resolved == ResolvedFileTarget(path=resolved_file.resolve(), line_number=9)


def test_resolve_visible_output_target_normalizes_git_diff_paths_before_relative_lookup(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    normalized_file = project_root / "app/models/user.rb"
    misleading_file = terminal_cwd / "b/app/models/user.rb"
    terminal_cwd.mkdir(parents=True)
    normalized_file.parent.mkdir(parents=True)
    misleading_file.parent.mkdir(parents=True)
    normalized_file.write_text("normalized\n")
    misleading_file.write_text("misleading\n")

    resolved = resolve_visible_output_target(
        "b/app/models/user.rb:9",
        terminal_cwd=terminal_cwd,
        project_root=project_root,
    )

    assert resolved == ResolvedFileTarget(path=normalized_file.resolve(), line_number=9)


def test_resolve_visible_output_target_maps_rails_processing_references(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    controller = project_root / "app/controllers/users_controller.rb"
    terminal_cwd.mkdir(parents=True)
    controller.parent.mkdir(parents=True)
    controller.write_text("class UsersController\nend\n")

    resolved = resolve_visible_output_target(
        "Processing UsersController#index",
        terminal_cwd=terminal_cwd,
        project_root=project_root,
    )

    assert resolved == ResolvedFileTarget(path=controller.resolve())


def test_resolve_visible_output_target_ignores_unresolvable_matches(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    terminal_cwd = project_root / "src"
    terminal_cwd.mkdir(parents=True)

    resolved = resolve_visible_output_target(
        "app/models/missing.rb:12",
        terminal_cwd=terminal_cwd,
        project_root=project_root,
    )

    assert resolved is None
