import io
import os
import re
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import hop.cli
import pytest
from hop.app import HopServices
from hop.kitty import KittyWindow, KittyWindowContext, KittyWindowState
from hop.session import ProjectSession
from hop.sway import SwayWindow


class StubSwayAdapter:
    def __init__(self) -> None:
        self.switched_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return ()

    def list_windows(self) -> Sequence[SwayWindow]:
        return ()

    def close_window(self, window_id: int) -> None:
        raise AssertionError("close_window should not be called in this test")

    def remove_workspace(self, workspace_name: str) -> None:
        raise AssertionError("remove_workspace should not be called in this test")


class StubKittyAdapter:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str, str, Path]] = []

    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
        raise AssertionError("ensure_terminal should not be called for hop run")

    def run_in_terminal(self, session: ProjectSession, *, role: str, command: str) -> int:
        self.runs.append((session.session_name, role, command, session.project_root))
        return 99

    def inspect_window(self, window_id: int) -> KittyWindowContext | None:
        return None

    def list_session_windows(self, session: ProjectSession) -> Sequence[KittyWindow]:
        return ()

    def close_window(self, window_id: int) -> None:
        raise AssertionError("close_window should not be called in this test")

    def get_window_state(self, window_id: int) -> KittyWindowState:
        raise AssertionError("get_window_state should not be called for hop run")

    def get_last_cmd_output(self, window_id: int) -> str:
        raise AssertionError("get_last_cmd_output should not be called for hop run")


class StubNeovimAdapter:
    def focus(self, session: ProjectSession) -> None:
        raise AssertionError("Neovim should not be called in this test")

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        raise AssertionError("Neovim should not be called in this test")


class StubBrowserAdapter:
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        raise AssertionError("Browser should not be called in this test")


@dataclass
class StubHopServices:
    sway: StubSwayAdapter
    kitty: StubKittyAdapter
    neovim: StubNeovimAdapter
    browser: StubBrowserAdapter

    def as_services(self) -> HopServices:
        return HopServices(sway=self.sway, kitty=self.kitty, neovim=self.neovim, browser=self.browser)


def build_services() -> StubHopServices:
    return StubHopServices(
        sway=StubSwayAdapter(),
        kitty=StubKittyAdapter(),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
    )


def test_main_smoke_routes_vigun_test_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    monkeypatch.setenv("HOP_RUNS_DIR", str(tmp_path / "runs"))

    services = build_services()
    command = "python3 -m pytest tests/test_run_commands.py -q"
    original_cwd = Path.cwd()
    original_build_default_services = hop.cli.build_default_services
    stdout = io.StringIO()

    try:
        os.chdir(nested_directory)
        hop.cli.build_default_services = lambda: services.as_services()

        with redirect_stdout(stdout):
            assert hop.cli.main(["run", "--role", "test", command]) == 0
    finally:
        os.chdir(original_cwd)
        hop.cli.build_default_services = original_build_default_services

    assert services.sway.switched_workspaces == [f"p:{nested_directory.name}"]
    assert services.kitty.runs == [("src", "test", command, nested_directory.resolve())]
    run_id = stdout.getvalue().strip()
    assert re.fullmatch(r"[0-9a-f]{32}", run_id)
    assert (tmp_path / "runs" / f"{run_id}.json").is_file()
