from pathlib import Path

from hop.session import (
    derive_project_root,
    derive_session_name,
    derive_workspace_name,
    resolve_project_session,
)


def test_derive_project_root_returns_the_provided_directory(tmp_path: Path) -> None:
    session_root = tmp_path / "demo-project" / "app" / "models"
    session_root.mkdir(parents=True)

    assert derive_project_root(session_root) == session_root


def test_derive_session_name_uses_project_directory_basename(tmp_path: Path) -> None:
    project_root = tmp_path / "demo-project"
    project_root.mkdir()

    assert derive_session_name(project_root) == "demo-project"


def test_derive_workspace_name_uses_directory_basename(tmp_path: Path) -> None:
    project_root = tmp_path / "demo-project"
    project_root.mkdir()

    assert derive_workspace_name(project_root) == "p:demo-project"


def test_resolve_project_session_builds_complete_session_identity(tmp_path: Path) -> None:
    session_root = tmp_path / "demo-project" / "pkg"
    session_root.mkdir(parents=True)

    assert resolve_project_session(session_root).project_root == session_root
    assert resolve_project_session(session_root).session_name == "pkg"
    assert resolve_project_session(session_root).workspace_name == "p:pkg"


def test_resolve_project_session_treats_nested_directories_as_distinct_sessions(tmp_path: Path) -> None:
    project_root = tmp_path / "demo-project"
    nested_directory = project_root / "pkg"
    project_root.mkdir()
    nested_directory.mkdir()

    assert resolve_project_session(project_root).session_name == "demo-project"
    assert resolve_project_session(nested_directory).session_name == "pkg"
