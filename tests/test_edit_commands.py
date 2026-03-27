from pathlib import Path

from hop.commands.edit import edit_in_session


class StubSwayAdapter:
    def __init__(self) -> None:
        self.switched_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.focused_sessions: list[str] = []
        self.opened_targets: list[tuple[str, str]] = []

    def focus(self, session) -> None:
        self.focused_sessions.append(session.session_name)

    def open_target(self, session, *, target: str) -> None:
        self.opened_targets.append((session.session_name, target))


def test_edit_in_session_switches_to_workspace_and_focuses_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    (project_root / ".git").mkdir()

    sway = StubSwayAdapter()
    neovim = StubNeovimAdapter()

    session = edit_in_session(nested_directory, sway=sway, neovim=neovim)

    assert session.session_name == "demo"
    assert sway.switched_workspaces == ["p:demo"]
    assert neovim.focused_sessions == ["demo"]


def test_edit_in_session_routes_targets_to_shared_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    (project_root / ".dust").mkdir()

    sway = StubSwayAdapter()
    neovim = StubNeovimAdapter()

    edit_in_session(
        nested_directory,
        sway=sway,
        neovim=neovim,
        target="app/models/user.rb:42",
    )

    assert sway.switched_workspaces == ["p:demo"]
    assert neovim.opened_targets == [("demo", "app/models/user.rb:42")]
