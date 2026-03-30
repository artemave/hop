from pathlib import Path

from hop.commands.edit import edit_in_session
from hop.session import ProjectSession


class StubSwayAdapter:
    def __init__(self) -> None:
        self.switched_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.focused_sessions: list[str] = []
        self.opened_targets: list[tuple[str, str]] = []

    def focus(self, session: ProjectSession) -> None:
        self.focused_sessions.append(session.session_name)

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        self.opened_targets.append((session.session_name, target))


def test_edit_in_session_switches_to_workspace_and_focuses_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    sway = StubSwayAdapter()
    neovim = StubNeovimAdapter()

    session = edit_in_session(nested_directory, sway=sway, neovim=neovim)

    assert session.session_name == "src"
    assert sway.switched_workspaces == [f"p:{nested_directory.name}"]
    assert neovim.focused_sessions == ["src"]


def test_edit_in_session_routes_targets_to_shared_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    sway = StubSwayAdapter()
    neovim = StubNeovimAdapter()

    edit_in_session(
        nested_directory,
        sway=sway,
        neovim=neovim,
        target="app/models/user.rb:42",
    )

    assert sway.switched_workspaces == [f"p:{nested_directory.name}"]
    assert neovim.opened_targets == [("src", "app/models/user.rb:42")]


def test_edit_in_session_treats_nested_directories_as_distinct_sessions(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    first_session_root = project_root / "src"
    second_session_root = first_session_root / "models"
    second_session_root.mkdir(parents=True)

    sway = StubSwayAdapter()
    neovim = StubNeovimAdapter()

    edit_in_session(first_session_root, sway=sway, neovim=neovim)
    edit_in_session(second_session_root, sway=sway, neovim=neovim)

    assert sway.switched_workspaces == [f"p:{first_session_root.name}", f"p:{second_session_root.name}"]
    assert neovim.focused_sessions == ["src", "models"]
