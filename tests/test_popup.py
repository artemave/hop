import shlex
from pathlib import Path
from typing import Sequence

import pytest

from hop.backends import CommandBackend, SessionBackendError, SshTransport
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
    session_root = tmp_path / "demo"
    session_root.mkdir()
    return ProjectSession(
        session_root=session_root,
        session_name=session_root.name,
        workspace_name=f"p:{session_root.name}",
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


class _OrderedLauncher:
    """Like ``_RecordingLauncher`` but appends to a shared event log so a test
    can assert the ``for_window`` rule was installed *before* kitty launched —
    sway only honors ``for_window`` rules that are in place when the window
    registers, so the ordering is load-bearing."""

    def __init__(self, *, exit_code: int, events: list[tuple[str, object]]) -> None:
        self.exit_code = exit_code
        self.calls: list[tuple[str, ...]] = []
        self._events = events

    def __call__(self, argv: Sequence[str]) -> _FakePopen:
        self.calls.append(tuple(argv))
        self._events.append(("launch", tuple(argv)))
        return _FakePopen(self.exit_code)


class _OrderedSway(_RecordingSway):
    """``_RecordingSway`` + shared event log so tests can pin command ordering
    relative to ``_OrderedLauncher``."""

    def __init__(self, *, events: list[tuple[str, object]]) -> None:
        super().__init__()
        self._events = events

    def run_command(self, command: str) -> None:
        super().run_command(command)
        self._events.append(("sway", command))


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
    # xdg-shell toplevel; sway floats and centers it via a `for_window` rule
    # installed before launch (see the sway-side test). `--class` sets the
    # Wayland app_id the `for_window` rule matches on.
    assert argv[0] == "kitty"
    assert "panel" not in argv  # not a layer-shell overlay
    assert "--class" in argv
    assert argv[argv.index("--class") + 1] == POPUP_APP_ID
    # Script is the last arg after `-- bash -c`. ``bash`` (not ``sh``) so the
    # lifecycle script can use process substitution to tee output to a
    # per-session log file.
    assert argv[-3:-1] == ("bash", "-c")
    script = argv[-1]
    assert f"cd {session.session_root}" in script
    assert f"Preparing {session.session_name}" in script
    assert "flock -o" in script
    assert "compose up -d devcontainer" in script


def test_run_prepare_installs_for_window_rules_before_launching_kitty(tmp_path: Path) -> None:
    """The float / resize / center happens via sway ``for_window`` rules
    issued *before* kitty launches. Sway evaluates ``for_window`` as the
    window registers, so the float must already be in place at registration
    time — installing it afterwards (the old polling approach) raced kitty's
    cold-start and left the popup tiled at full workspace size when the
    user's ``workspace_layout`` was ``tabbed``.

    Three rules, not one comma-chained rule: sway's parser binds only the
    first command to the ``for_window`` rule and runs the rest immediately
    against the currently focused container — which errors for ``move
    position center`` because the focused container isn't floating."""
    events: list[tuple[str, object]] = []
    sway = _OrderedSway(events=events)
    launcher = _OrderedLauncher(exit_code=0, events=events)
    popup = KittyHopPopup(sway=sway, launcher=launcher)

    popup.run_prepare(_make_session(tmp_path), _devcontainer_backend())

    criteria = f'[app_id="{POPUP_APP_ID}"]'
    assert sway.commands == [
        f"for_window {criteria} floating enable",
        f"for_window {criteria} resize set 60 ppt 50 ppt",
        f"for_window {criteria} move position center",
    ]
    # Ordering: all rules must be installed before kitty launches.
    assert [kind for kind, _payload in events] == ["sway", "sway", "sway", "launch"]


def test_show_error_installs_same_for_window_rules(tmp_path: Path) -> None:
    """``show_error`` shares the same ``hop:popup`` app_id, so the same
    ``for_window`` rules cover it — no workspace pinning is needed (errors
    have no session in scope and float on the user's current workspace)."""
    del tmp_path  # unused
    events: list[tuple[str, object]] = []
    sway = _OrderedSway(events=events)
    launcher = _OrderedLauncher(exit_code=0, events=events)
    popup = KittyHopPopup(sway=sway, launcher=launcher)

    popup.show_error(HopError("boom"))

    assert len(sway.commands) == 3
    assert all(cmd.startswith(f'for_window [app_id="{POPUP_APP_ID}"] ') for cmd in sway.commands)
    assert [kind for kind, _payload in events] == ["sway", "sway", "sway", "launch"]


def test_run_prepare_works_without_sway_adapter(tmp_path: Path) -> None:
    """Tests / non-sway contexts can omit the sway adapter; the popup still
    launches kitty and waits, just without the ``for_window`` install."""
    launcher = _RecordingLauncher(exit_code=0)
    popup = KittyHopPopup(launcher=launcher)  # sway omitted

    popup.run_prepare(_make_session(tmp_path), _devcontainer_backend())

    assert len(launcher.calls) == 1


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
    script = _lifecycle_script(session, ("compose up -d devcontainer",), kind="prepare", backend=_host_backend())

    assert f"cd {session.session_root}" in script
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
    script = _lifecycle_script(
        session, ("compose up", "install hop", "install kitten"), kind="prepare", backend=_host_backend()
    )

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
    script = _lifecycle_script(session, ("false",), kind="prepare", backend=_host_backend())

    result = subprocess.run(
        ["bash", "-c", script],
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
    script = _lifecycle_script(session, ("true",), kind="prepare", backend=_host_backend())

    result = subprocess.run(
        ["bash", "-c", script],
        input="",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    # Failure-path banner must NOT appear when prepare succeeded.
    assert "failed" not in result.stdout
    assert "Press Ctrl-D" not in result.stdout


def test_lifecycle_script_writes_output_to_per_session_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running the lifecycle script produces a log file under
    ``$XDG_RUNTIME_DIR/hop/`` containing the user-visible output — so prepare
    failures can be diagnosed after the transient popup has closed."""
    import subprocess

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = _make_session(tmp_path)
    script = _lifecycle_script(session, ("printf 'hello-from-prepare\\n'",), kind="prepare", backend=_host_backend())

    subprocess.run(["bash", "-c", script], input="", capture_output=True, text=True, check=True)

    log_file = tmp_path / "hop" / f"popup-{session.session_name}-prepare.log"
    assert log_file.exists()
    contents = log_file.read_text()
    assert f"Preparing {session.session_name}" in contents
    assert "hello-from-prepare" in contents


def test_lifecycle_script_log_overwrites_on_each_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The log file is truncated on each popup run (``tee`` without ``-a``) so
    stale output from a prior invocation never masks the current one."""
    import subprocess

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = _make_session(tmp_path)

    first = _lifecycle_script(session, ("printf 'FIRST\\n'",), kind="prepare", backend=_host_backend())
    subprocess.run(["bash", "-c", first], input="", capture_output=True, text=True, check=True)

    second = _lifecycle_script(session, ("printf 'SECOND\\n'",), kind="prepare", backend=_host_backend())
    subprocess.run(["bash", "-c", second], input="", capture_output=True, text=True, check=True)

    contents = (tmp_path / "hop" / f"popup-{session.session_name}-prepare.log").read_text()
    assert "FIRST" not in contents
    assert "SECOND" in contents


def test_lifecycle_script_teardown_shape(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    script = _lifecycle_script(session, ("compose down",), kind="teardown", backend=_host_backend())

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

    script = _lifecycle_script(session, (nasty,), kind="prepare", backend=_host_backend())

    # Slice out everything up to (and including) the announcement printfs —
    # before `flock` actually runs the user command. Running the full script
    # would invoke `flock` and the user's prepare command for real. Drop `cd`
    # (would change the test process's dir) and the `mkdir -p`/`exec > >(tee
    # …)` log-redirect prelude (would create a stray log file in this test).
    announcement = script.split("flock -o", 1)[0]
    runnable = "\n".join(
        line for line in announcement.splitlines() if not line.startswith(("cd ", "mkdir ", "exec > >(tee"))
    )

    result = subprocess.run(["bash", "-c", runnable], capture_output=True, text=True, check=True)

    # The user-visible announcement contains the verbatim command despite
    # the embedded `'`, `$(...)`, `&&`.
    assert nasty in result.stdout


def test_lifecycle_script_routes_remote_session_through_ssh_transport() -> None:
    """For a remote session the popup must cd locally to the host home and run
    each step over the ssh transport — never `cd <remote project root>` (which
    only exists on the remote) followed by a local `sh -c`."""
    session = ProjectSession(
        session_root=Path("/remote/proj"),
        session_name="proj",
        workspace_name="p:proj",
        host="devbox",
    )
    backend = CommandBackend(
        name="dc",
        interactive_prefix="podman-compose exec dc",
        noninteractive_prefix="podman-compose exec -T dc",
        transport=SshTransport("devbox", "/remote/proj", interactive=True),
        noninteractive_transport=SshTransport("devbox", "/remote/proj", interactive=False),
        host="devbox",
    )

    script = _lifecycle_script(session, ("podman-compose up -d",), kind="prepare", backend=backend)

    # The popup's own cwd is the local home, not the remote project root.
    assert f"cd {shlex.quote(str(Path.home()))}" in script
    # The step runs over ssh under flock; the remote cd is inside the base64
    # payload, so the remote path never appears as a bare local `cd`.
    flock_line = next(line for line in script.splitlines() if line.startswith("flock -o"))
    assert " ssh " in f" {flock_line} "
    assert "cd /remote/proj &&" not in script


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
