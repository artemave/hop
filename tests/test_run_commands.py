from pathlib import Path

from hop.commands.run import DEFAULT_RUN_ROLE, run_command


class StubSwayAdapter:
    def __init__(self) -> None:
        self.switched_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)


class StubKittyAdapter:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str, str, Path]] = []

    def run_in_terminal(self, session, *, role: str, command: str) -> None:
        self.runs.append((session.session_name, role, command, session.project_root))


def test_run_command_switches_to_workspace_and_routes_to_role_terminal(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    (project_root / ".dust").mkdir()

    sway = StubSwayAdapter()
    kitty = StubKittyAdapter()

    session = run_command(
        nested_directory,
        sway=sway,
        terminals=kitty,
        role="server",
        command="bin/dev",
    )

    assert session.session_name == "demo"
    assert sway.switched_workspaces == ["p:demo"]
    assert kitty.runs == [("demo", "server", "bin/dev", project_root)]


def test_run_command_defaults_to_shell_role(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    (project_root / ".git").mkdir()

    sway = StubSwayAdapter()
    kitty = StubKittyAdapter()

    run_command(
        nested_directory,
        sway=sway,
        terminals=kitty,
        command="ls",
    )

    assert kitty.runs == [("demo", DEFAULT_RUN_ROLE, "ls", project_root)]
