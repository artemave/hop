from pathlib import Path

from hop.commands.term import focus_terminal


class StubSwayAdapter:
    def __init__(self) -> None:
        self.switched_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)


class StubKittyAdapter:
    def __init__(self) -> None:
        self.ensured: list[tuple[str, str, Path]] = []

    def ensure_terminal(self, session, *, role: str) -> None:
        self.ensured.append((session.session_name, role, session.project_root))


def test_focus_terminal_switches_to_workspace_and_routes_by_role(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    sway = StubSwayAdapter()
    kitty = StubKittyAdapter()

    session = focus_terminal(
        nested_directory,
        sway=sway,
        terminals=kitty,
        role="test",
    )

    assert session.session_name == "src"
    assert sway.switched_workspaces == ["p:src"]
    assert kitty.ensured == [("src", "test", nested_directory)]
