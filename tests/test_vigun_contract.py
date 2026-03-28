import os
from pathlib import Path

import hop.cli
from hop.app import HopServices


class StubSwayAdapter:
    def __init__(self) -> None:
        self.switched_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return ()


class StubKittyAdapter:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str, str, Path]] = []

    def ensure_terminal(self, session, *, role: str) -> None:
        raise AssertionError("ensure_terminal should not be called for hop run")

    def run_in_terminal(self, session, *, role: str, command: str) -> None:
        self.runs.append((session.session_name, role, command, session.project_root))


class StubNeovimAdapter:
    def focus(self, session) -> None:
        raise AssertionError("Neovim should not be called in this test")

    def open_target(self, session, *, target: str) -> None:
        raise AssertionError("Neovim should not be called in this test")


class StubBrowserAdapter:
    def ensure_browser(self, session, *, url: str | None) -> None:
        raise AssertionError("Browser should not be called in this test")


def build_services() -> HopServices:
    return HopServices(
        sway=StubSwayAdapter(),
        kitty=StubKittyAdapter(),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
    )


def test_main_smoke_routes_vigun_test_command(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()
    command = "python3 -m pytest tests/test_run_commands.py -q"
    original_cwd = Path.cwd()
    original_build_default_services = hop.cli.build_default_services

    try:
        os.chdir(nested_directory)
        hop.cli.build_default_services = lambda: services

        assert hop.cli.main(["run", "--role", "test", command]) == 0
    finally:
        os.chdir(original_cwd)
        hop.cli.build_default_services = original_build_default_services

    assert services.sway.switched_workspaces == [f"p:{nested_directory.resolve()}"]
    assert services.kitty.runs == [("src", "test", command, nested_directory.resolve())]
