from pathlib import Path

from hop.session import (
    derive_project_root,
    derive_session_name,
    derive_workspace_name,
    resolve_project_session,
)


def test_derive_project_root_uses_nearest_marker(tmp_path: Path) -> None:
    project_root = tmp_path / "demo-project"
    nested_directory = project_root / "app" / "models"
    nested_directory.mkdir(parents=True)
    (project_root / ".git").mkdir()

    assert derive_project_root(nested_directory) == project_root


def test_derive_session_name_uses_project_directory_basename(tmp_path: Path) -> None:
    project_root = tmp_path / "demo-project"
    project_root.mkdir()

    assert derive_session_name(project_root) == "demo-project"


def test_derive_workspace_name_prefixes_session_name() -> None:
    assert derive_workspace_name("demo-project") == "p:demo-project"


def test_resolve_project_session_builds_complete_session_identity(tmp_path: Path) -> None:
    project_root = tmp_path / "demo-project"
    nested_directory = project_root / "pkg"
    nested_directory.mkdir(parents=True)
    (project_root / ".dust").mkdir()

    assert resolve_project_session(nested_directory).project_root == project_root
    assert resolve_project_session(nested_directory).session_name == "demo-project"
    assert resolve_project_session(nested_directory).workspace_name == "p:demo-project"
