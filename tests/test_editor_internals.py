# pyright: reportPrivateUsage=false

import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest
from hop.editor import (
    NeovimCommandError,
    SharedNeovimEditorAdapter,
    _remove_stale_socket,
    _resolve_runtime_dir,
    _SubprocessRunner,
)
from hop.session import ProjectSession
from hop.sway import SwayWindow


class StubKittyTransport:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.commands: list[tuple[str, Mapping[str, object] | None]] = []

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self.commands.append((command_name, payload))
        if not self._responses:
            return {"ok": True}
        return self._responses.pop(0)


class StubProcessRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self._responses = list(responses)
        self.commands: list[tuple[str, ...]] = []

    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        self.commands.append(command)
        if not self._responses:
            raise AssertionError(f"Unexpected process command: {command}")
        return self._responses.pop(0)


class StubSwayAdapter:
    def __init__(self, windows: Sequence[SwayWindow] = ()) -> None:
        self._windows: list[SwayWindow] = list(windows)
        self.focused: list[int] = []

    def list_windows(self) -> Sequence[SwayWindow]:
        return tuple(self._windows)

    def focus_window(self, window_id: int) -> None:
        self.focused.append(window_id)


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name=f"p:{project_root}",
    )


def _editor_window(window_id: int, *, app_id: str = "hop:demo:editor") -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name="p:/tmp/demo",
        app_id=app_id,
        window_class=None,
    )


def test_focus_raises_when_sway_has_no_editor_window(tmp_path: Path) -> None:
    runner = StubProcessRunner([subprocess.CompletedProcess(("nvim",), 0, "", "")])
    transport = StubKittyTransport([])
    adapter = SharedNeovimEditorAdapter(
        sway=StubSwayAdapter([]),
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=tmp_path / "runtime",
    )

    with pytest.raises(NeovimCommandError, match="no editor window"):
        adapter.focus(build_session())


def test_focus_picks_lowest_id_when_multiple_editor_windows_exist(tmp_path: Path) -> None:
    runner = StubProcessRunner([subprocess.CompletedProcess(("nvim",), 0, "", "")])
    transport = StubKittyTransport([])
    sway = StubSwayAdapter(
        [
            _editor_window(31),
            _editor_window(29),
            _editor_window(30),
            SwayWindow(id=28, workspace_name="p:/tmp/demo", app_id="kitty", window_class=None),
        ]
    )
    adapter = SharedNeovimEditorAdapter(
        sway=sway,
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=tmp_path / "runtime",
    )

    adapter.focus(build_session())

    assert sway.focused == [29]


def test_focus_matches_xwayland_editor_via_window_class(tmp_path: Path) -> None:
    runner = StubProcessRunner([subprocess.CompletedProcess(("nvim",), 0, "", "")])
    transport = StubKittyTransport([])
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=42,
                workspace_name="p:/tmp/demo",
                app_id=None,
                window_class="hop:demo:editor",
            )
        ]
    )
    adapter = SharedNeovimEditorAdapter(
        sway=sway,
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=tmp_path / "runtime",
    )

    adapter.focus(build_session())

    assert sway.focused == [42]


def test_wait_for_server_times_out_when_neovim_never_becomes_ready(tmp_path: Path) -> None:
    runner = StubProcessRunner(
        [
            subprocess.CompletedProcess(("nvim",), 1, "", ""),
            subprocess.CompletedProcess(("nvim",), 1, "", ""),
        ]
    )
    transport = StubKittyTransport([{"ok": True}])
    adapter = SharedNeovimEditorAdapter(
        sway=StubSwayAdapter(),
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=tmp_path / "runtime",
        ready_timeout_seconds=0.001,
        ready_poll_interval_seconds=0.0,
    )
    monotonic_values = iter([0.0, 0.0, 1.0])
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("hop.editor.time.monotonic", lambda: next(monotonic_values))

    try:
        with pytest.raises(NeovimCommandError, match="did not become ready"):
            adapter.focus(build_session())
    finally:
        monkeypatch.undo()


def test_open_target_raises_stderr_when_remote_send_fails(tmp_path: Path) -> None:
    address = (tmp_path / "runtime" / "hop.sock").resolve()
    runner = StubProcessRunner(
        [
            subprocess.CompletedProcess(("nvim",), 0, "", ""),
            subprocess.CompletedProcess(("nvim",), 1, "", "permission denied\n"),
        ]
    )
    transport = StubKittyTransport([])
    adapter = SharedNeovimEditorAdapter(
        sway=StubSwayAdapter([_editor_window(31)]),
        kitty_transport=transport,
        process_runner=runner,
        runtime_dir=address.parent,
    )

    with pytest.raises(NeovimCommandError, match="permission denied"):
        adapter.open_target(build_session(), target="README.md")


def test_resolve_runtime_dir_prefers_xdg_runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    assert _resolve_runtime_dir(None) == (tmp_path / "hop").resolve()


def test_resolve_runtime_dir_falls_back_to_tempdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr("hop.editor.gettempdir", lambda: str(tmp_path))

    assert _resolve_runtime_dir(None) == (tmp_path / "hop").resolve()


def test_remove_stale_socket_ignores_missing_paths(tmp_path: Path) -> None:
    _remove_stale_socket(tmp_path / "missing.sock")


def test_subprocess_runner_delegates_to_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = subprocess.CompletedProcess(("nvim",), 0, "ok", "")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ["nvim"]
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        return expected

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _SubprocessRunner().run(("nvim",)) == expected
