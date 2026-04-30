# pyright: reportPrivateUsage=false

import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest
from hop.editor import (
    NeovimCommandError,
    SharedNeovimEditorAdapter,
    _remove_stale_socket,
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
        self.marked: list[tuple[int, str]] = []

    def list_windows(self) -> Sequence[SwayWindow]:
        return tuple(self._windows)

    def focus_window(self, window_id: int) -> None:
        self.focused.append(window_id)

    def mark_window(self, window_id: int, mark: str) -> None:
        self.marked.append((window_id, mark))
        self._windows = [
            SwayWindow(
                id=window.id,
                workspace_name=window.workspace_name,
                app_id=window.app_id,
                window_class=window.window_class,
                marks=window.marks + (mark,),
                focused=window.focused,
            )
            if window.id == window_id
            else window
            for window in self._windows
        ]


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name=f"p:{project_root}",
    )


def _marked_editor(window_id: int) -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name=build_session().workspace_name,
        app_id="hop:editor",
        window_class=None,
        marks=("_hop_editor:demo",),
    )


def _unmarked_editor(window_id: int, *, app_id: str = "hop:editor") -> SwayWindow:
    return SwayWindow(
        id=window_id,
        workspace_name=build_session().workspace_name,
        app_id=app_id,
        window_class=None,
    )


def test_focus_raises_when_sway_has_no_editor_window(tmp_path: Path) -> None:
    # Gate poll (rc=1): server not alive → fall through to launch.
    # _wait_for_server reads v:vim_did_enter; rc=0 stdout="1" → ready.
    # Launch transport doesn't add a Sway window, so _focus_editor_window raises.
    runner = StubProcessRunner(
        [
            subprocess.CompletedProcess(("nvim",), 1, "", ""),
            subprocess.CompletedProcess(("nvim",), 0, "1", ""),
        ]
    )
    transport = StubKittyTransport([])
    adapter = SharedNeovimEditorAdapter(
        sway=StubSwayAdapter([]),
        kitty_transport=transport,
        process_runner=runner,
    )

    with pytest.raises(NeovimCommandError, match="no editor window"):
        adapter.focus(build_session())


def test_focus_picks_lowest_id_when_multiple_marked_editor_windows_exist(tmp_path: Path) -> None:
    runner = StubProcessRunner([subprocess.CompletedProcess(("nvim",), 0, "", "")])
    transport = StubKittyTransport([])
    sway = StubSwayAdapter([_marked_editor(31), _marked_editor(29), _marked_editor(30)])
    adapter = SharedNeovimEditorAdapter(
        sway=sway,
        kitty_transport=transport,
        process_runner=runner,
    )

    adapter.focus(build_session())

    assert sway.focused == [29]
    assert sway.marked == []


def test_focus_marks_unmarked_editor_on_first_sighting(tmp_path: Path) -> None:
    runner = StubProcessRunner([subprocess.CompletedProcess(("nvim",), 0, "", "")])
    transport = StubKittyTransport([])
    sway = StubSwayAdapter([_unmarked_editor(42)])
    adapter = SharedNeovimEditorAdapter(
        sway=sway,
        kitty_transport=transport,
        process_runner=runner,
    )

    adapter.focus(build_session())

    assert sway.marked == [(42, "_hop_editor:demo")]
    assert sway.focused == [42]


def test_focus_skips_unmarked_editor_belonging_to_a_different_session(tmp_path: Path) -> None:
    # Server not alive at gate → relaunch path → _wait_for_server (vim_did_enter)
    # succeeds → _focus_editor_window doesn't accept the foreign-marked window
    # for this session and raises.
    runner = StubProcessRunner(
        [
            subprocess.CompletedProcess(("nvim",), 1, "", ""),
            subprocess.CompletedProcess(("nvim",), 0, "1", ""),
        ]
    )
    transport = StubKittyTransport([])
    foreign_editor = SwayWindow(
        id=42,
        workspace_name=build_session().workspace_name,
        app_id="hop:editor",
        window_class=None,
        marks=("_hop_editor:other",),
    )
    sway = StubSwayAdapter([foreign_editor])
    adapter = SharedNeovimEditorAdapter(
        sway=sway,
        kitty_transport=transport,
        process_runner=runner,
    )

    with pytest.raises(NeovimCommandError, match="no editor window"):
        adapter.focus(build_session())


def test_focus_matches_xwayland_editor_via_window_class(tmp_path: Path) -> None:
    runner = StubProcessRunner([subprocess.CompletedProcess(("nvim",), 0, "", "")])
    transport = StubKittyTransport([])
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=42,
                workspace_name=build_session().workspace_name,
                app_id=None,
                window_class="hop:editor",
            )
        ]
    )
    adapter = SharedNeovimEditorAdapter(
        sway=sway,
        kitty_transport=transport,
        process_runner=runner,
    )

    adapter.focus(build_session())

    assert sway.marked == [(42, "_hop_editor:demo")]
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


def test_open_target_raises_stderr_when_remote_send_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runner = StubProcessRunner(
        [
            subprocess.CompletedProcess(("nvim",), 0, "", ""),
            subprocess.CompletedProcess(("nvim",), 1, "", "permission denied\n"),
        ]
    )
    transport = StubKittyTransport([])
    adapter = SharedNeovimEditorAdapter(
        sway=StubSwayAdapter([_marked_editor(31)]),
        kitty_transport=transport,
        process_runner=runner,
    )

    with pytest.raises(NeovimCommandError, match="permission denied"):
        adapter.open_target(build_session(), target="README.md")


def test_open_target_retries_remote_expr_on_connection_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Right after launch, the in-backend nvim's listener can briefly flap
    (compose-exec startup race, UI attach init); the first --remote-expr
    lands in the gap with `connection refused`. Adapter should wait for the
    listener to recover and retry once."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runner = StubProcessRunner(
        [
            # _ensure_editor → _server_is_running gate: alive.
            subprocess.CompletedProcess(("nvim",), 0, "", ""),
            # First open-expr: connection refused (listener flapped).
            subprocess.CompletedProcess(
                ("nvim",), 1, "", "E247: Failed to connect to '...': connection refused. Send failed.\n"
            ),
            # _wait_for_server readiness poll after the refusal: ready.
            subprocess.CompletedProcess(("nvim",), 0, "1", ""),
            # Retry open-expr: success.
            subprocess.CompletedProcess(("nvim",), 0, "", ""),
        ]
    )
    transport = StubKittyTransport([])
    adapter = SharedNeovimEditorAdapter(
        sway=StubSwayAdapter([_marked_editor(31)]),
        kitty_transport=transport,
        process_runner=runner,
    )

    adapter.open_target(build_session(), target="README.md")

    # 4 nvim calls total: gate poll + first open-expr (refused) + recovery
    # poll + retry open-expr. All --remote-expr; none use --remote-send.
    assert len(runner.commands) == 4
    assert all("--remote-expr" in cmd for cmd in runner.commands)
    assert not any("--remote-send" in cmd for cmd in runner.commands)


def test_open_target_raises_when_retry_after_recovery_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the recovery wait succeeds but the retry still fails (for a different
    reason), the second failure is what surfaces to the caller."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runner = StubProcessRunner(
        [
            subprocess.CompletedProcess(("nvim",), 0, "", ""),
            subprocess.CompletedProcess(
                ("nvim",),
                1,
                "",
                "E247: Failed to connect: connection refused. Send failed.\n",
            ),
            subprocess.CompletedProcess(("nvim",), 0, "1", ""),
            subprocess.CompletedProcess(("nvim",), 1, "", "permission denied\n"),
        ]
    )
    transport = StubKittyTransport([])
    adapter = SharedNeovimEditorAdapter(
        sway=StubSwayAdapter([_marked_editor(31)]),
        kitty_transport=transport,
        process_runner=runner,
    )

    with pytest.raises(NeovimCommandError, match="permission denied"):
        adapter.open_target(build_session(), target="README.md")


def test_open_target_raises_original_error_when_recovery_wait_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the post-launch listener flap (`connection refused`) does not
    recover before `_wait_for_server` times out, the recovery is abandoned and
    the original send error is surfaced to the caller."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runner = StubProcessRunner(
        [
            # _ensure_editor → _server_is_running gate: alive.
            subprocess.CompletedProcess(("nvim",), 0, "", ""),
            # First open-expr: connection refused.
            subprocess.CompletedProcess(
                ("nvim",),
                1,
                "",
                "E247: Failed to connect: connection refused. Send failed.\n",
            ),
            # Recovery readiness poll: never ready.
            subprocess.CompletedProcess(("nvim",), 1, "", ""),
        ]
    )
    transport = StubKittyTransport([])
    adapter = SharedNeovimEditorAdapter(
        sway=StubSwayAdapter([_marked_editor(31)]),
        kitty_transport=transport,
        process_runner=runner,
        ready_timeout_seconds=0.001,
        ready_poll_interval_seconds=0.0,
    )
    monotonic_values = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr("hop.editor.time.monotonic", lambda: next(monotonic_values))

    with pytest.raises(NeovimCommandError, match="connection refused"):
        adapter.open_target(build_session(), target="README.md")


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
