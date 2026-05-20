from pathlib import Path
from typing import Callable, Sequence

import pytest

from hop.backends import CommandBackend, SessionBackendError
from hop.errors import HopError
from hop.popup import (
    POPUP_APP_ID,
    HopPopup,
    KittyHopPopup,
    _error_script,  # pyright: ignore[reportPrivateUsage]
    _lifecycle_script,  # pyright: ignore[reportPrivateUsage]
)
from hop.session import ProjectSession
from hop.sway import SwayWindow


def _make_session(tmp_path: Path) -> ProjectSession:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    return ProjectSession(
        project_root=project_root,
        session_name=project_root.name,
        workspace_name=f"p:{project_root.name}",
    )


def _devcontainer_backend() -> CommandBackend:
    return CommandBackend(
        name="devcontainer",
        interactive_prefix="compose exec devcontainer",
        noninteractive_prefix="compose exec -T devcontainer",
        prepare_command=("compose up -d devcontainer",),
        teardown_command=("compose down",),
    )


def _host_backend() -> CommandBackend:
    return CommandBackend(name="host", interactive_prefix="", noninteractive_prefix="")


class _FakePopen:
    """Minimal stand-in for `subprocess.Popen` — only `wait()` is exercised
    by KittyHopPopup. Exposes a `pid` so callers can pretend to track it."""

    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code
        self.pid = 42

    def wait(self) -> int:
        return self._exit_code


class _RecordingLauncher:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> _FakePopen:
        self.calls.append(tuple(argv))
        return _FakePopen(self.exit_code)


class _RecordingSway:
    """Simulates sway's view of the popup window. The launcher is wired to
    insert a new window with `app_id="hop:popup"` into `windows` when called,
    so `KittyHopPopup`'s polling loop discovers it on the first iteration."""

    def __init__(self, *, windows: tuple[SwayWindow, ...] = ()) -> None:
        self.windows: tuple[SwayWindow, ...] = windows
        self.commands: list[str] = []

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return self.windows

    def run_command(self, command: str) -> None:
        self.commands.append(command)


def _popup_window(con_id: int) -> SwayWindow:
    return SwayWindow(id=con_id, workspace_name="p:demo", app_id=POPUP_APP_ID, window_class=None)


class _LauncherWithSpawnHook:
    """Like `_RecordingLauncher` but invokes `on_spawn` *before* returning
    the fake Popen — used to simulate kitty registering its sway window
    between Popen and wait."""

    def __init__(self, *, exit_code: int = 0, on_spawn: Callable[[], None] | None = None) -> None:
        self.exit_code = exit_code
        self.calls: list[tuple[str, ...]] = []
        self._on_spawn = on_spawn

    def __call__(self, argv: Sequence[str]) -> _FakePopen:
        self.calls.append(tuple(argv))
        if self._on_spawn is not None:
            self._on_spawn()
        return _FakePopen(self.exit_code)


def test_kitty_hop_popup_is_interactive_keys_on_injected_stderr_isatty() -> None:
    popup_yes = KittyHopPopup(stderr_isatty=lambda: True)
    popup_no = KittyHopPopup(stderr_isatty=lambda: False)

    assert popup_yes.is_interactive() is True
    assert popup_no.is_interactive() is False


def test_kitty_hop_popup_satisfies_protocol() -> None:
    """`HopPopup` is a structural Protocol; `KittyHopPopup` is the production
    implementer. Test the structural fit so accidental signature drift
    surfaces at the test layer."""
    popup: HopPopup = KittyHopPopup()  # pyright: ignore[reportUnusedVariable]
    del popup


def test_run_prepare_noop_when_backend_has_no_prepare_command(tmp_path: Path) -> None:
    launcher = _RecordingLauncher()
    popup = KittyHopPopup(launcher=launcher)

    popup.run_prepare(_make_session(tmp_path), _host_backend())

    assert launcher.calls == []


def test_run_teardown_noop_when_backend_has_no_teardown_command(tmp_path: Path) -> None:
    launcher = _RecordingLauncher()
    popup = KittyHopPopup(launcher=launcher)

    popup.run_teardown(_make_session(tmp_path), _host_backend())

    assert launcher.calls == []


def test_run_prepare_launches_plain_kitty_with_popup_app_id(tmp_path: Path) -> None:
    launcher = _RecordingLauncher(exit_code=0)
    popup = KittyHopPopup(launcher=launcher)
    session = _make_session(tmp_path)

    popup.run_prepare(session, _devcontainer_backend())

    assert len(launcher.calls) == 1
    argv = launcher.calls[0]
    # Plain `kitty` (NOT `kitten panel`) so the window is a regular
    # xdg-shell toplevel that sway can place on a specific workspace; the
    # popup floats via sway IPC issued after the window registers (see the
    # sway-side test). `--class` sets the Wayland app_id.
    assert argv[0] == "kitty"
    assert "panel" not in argv  # not a layer-shell overlay
    assert "--class" in argv
    assert argv[argv.index("--class") + 1] == POPUP_APP_ID
    # Cap the launch size so the popup is usable even when the sway-IPC
    # resize loses the race against kitty's window registration. Cells, not
    # pixels, so it scales with the user's font.
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
    assert any(o.startswith("initial_window_width=") and o.endswith("c") for o in overrides)
    assert any(o.startswith("initial_window_height=") and o.endswith("c") for o in overrides)
    # Script is the last arg after `-- sh -c`.
    assert argv[-3:-1] == ("sh", "-c")
    script = argv[-1]
    assert f"cd {session.project_root}" in script
    assert f"Preparing {session.session_name}" in script
    assert "flock -o" in script
    assert "compose up -d devcontainer" in script


def test_run_prepare_moves_floats_resizes_and_centers_the_popup_window_via_sway(tmp_path: Path) -> None:
    """After kitty spawns the popup window, hop must pin it to the session's
    workspace *first*, then float / resize / center — so the ``ppt`` resize is
    measured against the session's output rather than whichever monitor was
    focused when kitty happened to register the window. Without the move step,
    focusing away from ``p:<session>`` during ``prepare`` left the popup on
    the wrong display."""
    sway = _RecordingSway()

    def appear() -> None:
        # Simulate kitty registering its window with sway between Popen and
        # the first poll iteration.
        sway.windows = (_popup_window(con_id=77),)

    launcher = _LauncherWithSpawnHook(exit_code=0, on_spawn=appear)
    popup = KittyHopPopup(sway=sway, launcher=launcher, sleep=lambda _s: None)
    session = _make_session(tmp_path)

    popup.run_prepare(session, _devcontainer_backend())

    assert len(sway.commands) == 1
    cmd = sway.commands[0]
    assert cmd.startswith("[con_id=77] ")
    # Order is load-bearing: workspace move must precede the resize so the
    # percentage-based resize uses the session's output dimensions.
    move_idx = cmd.index(f"move container to workspace {session.workspace_name}")
    resize_idx = cmd.index("resize set")
    floating_idx = cmd.index("floating enable")
    assert move_idx < floating_idx < resize_idx
    assert "ppt" in cmd  # percentage of the workspace's output, not raw px
    assert cmd.endswith("move position center")


def test_show_error_floats_without_workspace_pin(tmp_path: Path) -> None:
    """``show_error`` has no session in scope — the popup floats on whichever
    workspace it registered on, no move-to-workspace step."""
    del tmp_path  # unused
    sway = _RecordingSway()

    def appear() -> None:
        sway.windows = (_popup_window(con_id=99),)

    launcher = _LauncherWithSpawnHook(exit_code=0, on_spawn=appear)
    popup = KittyHopPopup(sway=sway, launcher=launcher, sleep=lambda _s: None)

    popup.show_error(HopError("boom"))

    assert len(sway.commands) == 1
    cmd = sway.commands[0]
    assert "move container to workspace" not in cmd
    assert "floating enable" in cmd
    assert cmd.endswith("move position center")


def test_run_prepare_ignores_pre_existing_popup_windows(tmp_path: Path) -> None:
    """A stale popup window left over from an earlier invocation must not be
    re-floated; only the window that appears between Popen and the poll."""
    stale = _popup_window(con_id=11)
    sway = _RecordingSway(windows=(stale,))

    def appear() -> None:
        sway.windows = (stale, _popup_window(con_id=99))

    launcher = _LauncherWithSpawnHook(exit_code=0, on_spawn=appear)
    popup = KittyHopPopup(sway=sway, launcher=launcher, sleep=lambda _s: None)

    popup.run_prepare(_make_session(tmp_path), _devcontainer_backend())

    assert len(sway.commands) == 1
    assert sway.commands[0].startswith("[con_id=99] ")


def test_run_prepare_skips_floating_when_kitty_never_registers_a_window(tmp_path: Path) -> None:
    """If kitty fails to bring up a window (binary missing, crash on
    startup), the polling loop should time out gracefully — the outer
    `proc.wait()` exit code is the failure signal."""
    sway = _RecordingSway()
    # Launcher returns a fake Popen whose wait() reports failure; the spawn
    # hook is omitted so `sway.windows` stays empty for the whole poll.
    launcher = _LauncherWithSpawnHook(exit_code=1)

    elapsed = [0.0]

    def fake_clock() -> float:
        return elapsed[0]

    def fake_sleep(seconds: float) -> None:
        elapsed[0] += seconds

    popup = KittyHopPopup(sway=sway, launcher=launcher, clock=fake_clock, sleep=fake_sleep)

    with pytest.raises(SessionBackendError):
        popup.run_prepare(_make_session(tmp_path), _devcontainer_backend())

    # No sway commands issued (no window to target), but the poll did exit
    # promptly via the timeout — the elapsed clock is bounded.
    assert sway.commands == []
    assert elapsed[0] >= 2.0  # _FLOAT_POLL_TIMEOUT_SECONDS


def test_run_teardown_announces_tearing_down_in_script(tmp_path: Path) -> None:
    launcher = _RecordingLauncher(exit_code=0)
    popup = KittyHopPopup(launcher=launcher)
    session = _make_session(tmp_path)

    popup.run_teardown(session, _devcontainer_backend())

    assert len(launcher.calls) == 1
    argv = launcher.calls[0]
    # Per-kind UI signal lives inside the script's announcement printf, not on
    # the kitty command line.
    script = argv[-1]
    assert f"Tearing down {session.session_name}" in script
    assert "compose down" in script


def test_run_prepare_raises_session_backend_error_on_non_zero_exit(tmp_path: Path) -> None:
    launcher = _RecordingLauncher(exit_code=1)
    popup = KittyHopPopup(launcher=launcher)
    session = _make_session(tmp_path)

    with pytest.raises(SessionBackendError) as excinfo:
        popup.run_prepare(session, _devcontainer_backend())

    assert "prepare" in str(excinfo.value)
    assert session.session_name in str(excinfo.value)
    # Marker on the error: cli.main's catch-all popup must skip showing
    # a second panel for an error the lifecycle popup already surfaced.
    assert excinfo.value.surfaced_by_popup is True


def test_run_teardown_raises_session_backend_error_on_non_zero_exit(tmp_path: Path) -> None:
    launcher = _RecordingLauncher(exit_code=130)
    popup = KittyHopPopup(launcher=launcher)
    session = _make_session(tmp_path)

    with pytest.raises(SessionBackendError) as excinfo:
        popup.run_teardown(session, _devcontainer_backend())

    assert "teardown" in str(excinfo.value)
    assert excinfo.value.surfaced_by_popup is True


def test_show_error_launches_kitty_window_with_error_text() -> None:
    launcher = _RecordingLauncher(exit_code=0)
    popup = KittyHopPopup(launcher=launcher)

    popup.show_error(HopError("backend probe failed"))

    assert len(launcher.calls) == 1
    argv = launcher.calls[0]
    assert argv[0] == "kitty"
    assert "--class" in argv
    assert argv[argv.index("--class") + 1] == POPUP_APP_ID
    script = argv[-1]
    assert "hop:" in script
    # The type name surfaces in the printed message so the user sees both
    # the kind of failure and the human-readable text.
    assert "HopError" in script
    assert "backend probe failed" in script
    assert "Press Ctrl-D to close" in script
    assert "exec sh" in script


def test_show_error_does_not_raise_when_launcher_exits_non_zero() -> None:
    """The user dismissing the popup IS the success signal — there's no
    failure mode for `show_error`. A non-zero exit must not propagate as
    an exception (that would mask the very error we tried to display)."""
    launcher = _RecordingLauncher(exit_code=1)
    popup = KittyHopPopup(launcher=launcher)

    # No raise.
    popup.show_error(HopError("boom"))


def test_lifecycle_script_prepare_shape(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    script = _lifecycle_script(session, ("compose up -d devcontainer",), kind="prepare")

    assert f"cd {session.project_root}" in script
    assert f"Preparing {session.session_name}" in script
    assert "$ %s\\n\\n" in script  # template literal — escaped \n for the printf format string
    assert "flock -o" in script
    assert "compose up -d devcontainer" in script
    # `exit 0` shortcut on success.
    assert "exit 0" in script
    # `prepare failed (exit %d)` and a held-open shell on failure. Using a
    # non-`exec` `sh` lets us run `exit "$status"` afterwards — `exec sh`
    # would clobber the captured status when the user dismisses (sh exits 0)
    # and the parent hop process would interpret the panel's exit-0 as
    # "prepare succeeded".
    assert "prepare" in script
    assert "exec sh" not in script
    assert "    sh" in script
    assert 'exit "$status"' in script


def test_lifecycle_script_multi_step_emits_per_step_blocks(tmp_path: Path) -> None:
    """Each step renders its own announcement + flock + per-step failure guard.
    The held-open ``sh`` fires *only* for the failing step; later steps don't
    execute. Successful steps fall through to the next without dropping the
    user into a shell."""
    session = _make_session(tmp_path)
    script = _lifecycle_script(session, ("compose up", "install hop", "install kitten"), kind="prepare")

    assert "compose up" in script
    assert "install hop" in script
    assert "install kitten" in script
    # One per-step failure guard per step (three total).
    assert script.count('if [ "$status" -ne 0 ]; then') == 3
    # The terminal `exit 0` only fires if every step succeeded.
    assert script.rstrip().endswith("exit 0")


def test_lifecycle_script_preserves_failure_status_through_held_shell(tmp_path: Path) -> None:
    """End-to-end: the generated script, run through a real `sh`, propagates
    the inner command's non-zero exit code even after the held shell is
    dismissed. Simulates the user pressing Ctrl-D by feeding EOF on stdin."""
    import subprocess

    session = _make_session(tmp_path)
    # Substitute a command that always fails (the `flock` invocation runs
    # `sh -c "false"`).
    script = _lifecycle_script(session, ("false",), kind="prepare")

    result = subprocess.run(
        ["sh", "-c", script],
        input="",  # EOF immediately — simulates Ctrl-D on the held shell
        capture_output=True,
        text=True,
        check=False,
    )

    # Original prepare exit code propagates through the panel.
    assert result.returncode != 0
    # User-visible announcement still rendered before the held shell.
    assert f"Preparing {session.session_name}" in result.stdout
    assert "prepare failed (exit 1)" in result.stdout


def test_lifecycle_script_success_does_not_hold_shell(tmp_path: Path) -> None:
    """On success the script must `exit 0` immediately — no held shell.
    Otherwise the popup would linger and the user has to dismiss it manually."""
    import subprocess

    session = _make_session(tmp_path)
    script = _lifecycle_script(session, ("true",), kind="prepare")

    result = subprocess.run(
        ["sh", "-c", script],
        input="",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    # Failure-path banner must NOT appear when prepare succeeded.
    assert "failed" not in result.stdout
    assert "Press Ctrl-D" not in result.stdout


def test_lifecycle_script_teardown_shape(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    script = _lifecycle_script(session, ("compose down",), kind="teardown")

    assert f"Tearing down {session.session_name}" in script
    assert "compose down" in script
    assert "teardown" in script


def test_lifecycle_script_quotes_shell_metacharacters(tmp_path: Path) -> None:
    """`shlex.quote` keeps the announcement lines safe when the prepare
    command contains shell metacharacters; the command itself runs verbatim
    inside the `flock -o ... sh -c '<cmd>'` invocation. We verify semantics
    by piping the generated script (minus the held-open `exec sh`) through a
    real `sh -c` and capturing what it prints."""
    import subprocess

    session = _make_session(tmp_path)
    nasty = "echo $(date) > /tmp/'foo bar'.log && true"

    script = _lifecycle_script(session, (nasty,), kind="prepare")

    # Slice out everything up to (and including) the announcement printfs —
    # before `flock` actually runs the user command. Running the full script
    # would invoke `flock` and the user's prepare command for real.
    announcement = script.split("flock -o", 1)[0]
    # cd would otherwise leave the test in a different dir; drop it.
    runnable = "\n".join(line for line in announcement.splitlines() if not line.startswith("cd "))

    result = subprocess.run(["sh", "-c", runnable], capture_output=True, text=True, check=True)

    # The user-visible announcement contains the verbatim command despite
    # the embedded `'`, `$(...)`, `&&`.
    assert nasty in result.stdout


def test_error_script_includes_class_name_and_message() -> None:
    class DevcontainerError(HopError):
        pass

    error = DevcontainerError("compose: image not found")
    script = _error_script(error)

    assert "hop:" in script
    assert "DevcontainerError" in script
    assert "compose: image not found" in script
    assert "Press Ctrl-D to close" in script
    assert "exec sh" in script


def test_error_script_quotes_newlines_and_quotes_in_message() -> None:
    error = HopError("oops\nwith 'quotes' and $vars")
    script = _error_script(error)

    # The message renders verbatim through `shlex.quote`; the printf format
    # string is a literal so newlines and quotes in the user-visible text
    # don't break the shell wrapper.
    assert "oops" in script
    assert "quotes" in script
    assert "$vars" in script


def test_default_factories_bind_to_subprocess_and_sys_stderr_isatty() -> None:
    """Smoke: the no-arg constructor produces an object whose `is_interactive`
    is wired to `sys.stderr.isatty` (we just check the type), and whose
    launcher delegates to subprocess.run (we don't actually invoke kitten in
    tests — just confirm the field is set)."""
    popup = KittyHopPopup()

    # Should return a bool either way; we don't pin which.
    assert isinstance(popup.is_interactive(), bool)


def test_default_launcher_spawns_subprocess_and_returns_popen() -> None:
    """The default launcher delegates to `subprocess.Popen` and returns the
    handle; callers `wait()` on it. Exercised with a trivial `sh -c` so the
    test doesn't actually invoke kitty (which would require a Wayland session)."""
    from hop.popup import _default_launcher  # pyright: ignore[reportPrivateUsage]

    proc_ok = _default_launcher(["sh", "-c", "exit 0"])
    assert proc_ok.wait() == 0

    proc_err = _default_launcher(["sh", "-c", "exit 7"])
    assert proc_err.wait() == 7
