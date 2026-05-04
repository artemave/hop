import json
from pathlib import Path

import pytest

from hop.commands.run import DEFAULT_RUN_ROLE, default_runs_dir, run_command
from hop.session import ProjectSession


class StubKittyAdapter:
    def __init__(self, *, window_id: int = 7) -> None:
        self._window_id = window_id
        self.runs: list[tuple[str, str, str, Path, bool]] = []

    def run_in_terminal(
        self,
        session: ProjectSession,
        *,
        role: str,
        command: str,
        focus: bool = False,
    ) -> int:
        self.runs.append((session.session_name, role, command, session.project_root, focus))
        return self._window_id


def test_run_command_routes_to_role_terminal(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    kitty = StubKittyAdapter(window_id=42)

    dispatch = run_command(
        nested_directory,
        terminals=kitty,
        role="server",
        command="bin/dev",
        runs_dir=tmp_path / "runs",
    )

    assert dispatch.session.session_name == "src"
    assert dispatch.window_id == 42
    assert dispatch.run_id
    assert kitty.runs == [("src", "server", "bin/dev", nested_directory, False)]

    state = json.loads((tmp_path / "runs" / f"{dispatch.run_id}.json").read_text())
    assert state["window_id"] == 42
    assert state["session"] == "src"
    assert state["role"] == "server"
    assert isinstance(state["dispatched_at"], (int, float))


def test_run_command_defaults_to_shell_role(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    kitty = StubKittyAdapter()

    dispatch = run_command(
        nested_directory,
        terminals=kitty,
        command="ls",
        runs_dir=tmp_path / "runs",
    )

    assert kitty.runs == [("src", DEFAULT_RUN_ROLE, "ls", nested_directory, False)]
    state = json.loads((tmp_path / "runs" / f"{dispatch.run_id}.json").read_text())
    assert state["role"] == DEFAULT_RUN_ROLE


def test_run_command_forwards_focus_to_kitty(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    kitty = StubKittyAdapter()

    run_command(
        nested_directory,
        terminals=kitty,
        command="ls",
        focus=True,
        runs_dir=tmp_path / "runs",
    )

    assert kitty.runs == [("src", DEFAULT_RUN_ROLE, "ls", nested_directory, True)]


def test_run_command_emits_unique_run_ids(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    kitty = StubKittyAdapter()
    runs_dir = tmp_path / "runs"

    first = run_command(nested_directory, terminals=kitty, command="ls", runs_dir=runs_dir)
    second = run_command(nested_directory, terminals=kitty, command="ls", runs_dir=runs_dir)

    assert first.run_id != second.run_id


def test_default_runs_dir_prefers_xdg_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOP_RUNS_DIR", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert default_runs_dir() == Path("/run/user/1000/hop/runs")


def test_default_runs_dir_falls_back_to_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOP_RUNS_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_runs_dir() == Path("/tmp/hop/runs")


def test_default_runs_dir_honors_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOP_RUNS_DIR", "/custom/runs")
    assert default_runs_dir() == Path("/custom/runs")
