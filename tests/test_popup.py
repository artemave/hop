import json
import os
import sys
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
    _hold_shell,  # pyright: ignore[reportPrivateUsage]
    _lifecycle_spec,  # pyright: ignore[reportPrivateUsage]
    run_popup_lifecycle,
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


def _write_spec(
    tmp_path: Path,
    *,
    steps: list[dict[str, object]],
    kind: str = "prepare",
    verb: str = "Preparing demo",
) -> tuple[Path, Path]:
    """Write a lifecycle spec JSON (as ``KittyHopPopup`` would) and return the
    spec path plus the log path it points at."""
    log = tmp_path / "popup.log"
    spec = {"kind": kind, "verb": verb, "cwd": str(tmp_path), "log_path": str(log), "steps": steps}
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    return spec_path, log


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


def test_run_prepare_launches_python_lifecycle_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    launcher = _RecordingLauncher(exit_code=0)
    popup = KittyHopPopup(launcher=launcher)
    session = _make_session(tmp_path)

    popup.run_prepare(session, _devcontainer_backend())

    assert len(launcher.calls) == 1
    argv = launcher.calls[0]
    # Plain `kitty` (NOT `kitten panel`) so the window is a regular xdg-shell
    # toplevel; sway floats and centers it via a `for_window` rule installed
    # before launch. `--class` sets the Wayland app_id the rule matches on.
    assert argv[0] == "kitty"
    assert "panel" not in argv  # not a layer-shell overlay
    assert argv[argv.index("--class") + 1] == POPUP_APP_ID
    # After `--` the window runs the lifecycle through hop itself (so it reuses
    # the spinner) via `<python> -m hop __run-lifecycle <spec>`.
    command = argv[argv.index("--") + 1 :]
    assert command[0] == sys.executable
    assert command[1:4] == ("-m", "hop", "__run-lifecycle")
    spec = json.loads(Path(command[4]).read_text())
    assert spec["verb"] == f"Preparing {session.session_name}"
    assert spec["steps"][0]["display"] == "compose up -d devcontainer"


def test_run_prepare_installs_for_window_rules_before_launching_kitty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
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


def test_show_error_installs_same_for_window_rules() -> None:
    """``show_error`` shares the same ``hop:popup`` app_id, so the same
    ``for_window`` rules cover it — no workspace pinning is needed (errors
    have no session in scope and float on the user's current workspace)."""
    events: list[tuple[str, object]] = []
    sway = _OrderedSway(events=events)
    launcher = _OrderedLauncher(exit_code=0, events=events)
    popup = KittyHopPopup(sway=sway, launcher=launcher)

    popup.show_error(HopError("boom"))

    assert len(sway.commands) == 3
    assert all(cmd.startswith(f'for_window [app_id="{POPUP_APP_ID}"] ') for cmd in sway.commands)
    assert [kind for kind, _payload in events] == ["sway", "sway", "sway", "launch"]


def test_run_prepare_works_without_sway_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests / non-sway contexts can omit the sway adapter; the popup still
    launches kitty and waits, just without the ``for_window`` install."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    launcher = _RecordingLauncher(exit_code=0)
    popup = KittyHopPopup(launcher=launcher)  # sway omitted

    popup.run_prepare(_make_session(tmp_path), _devcontainer_backend())

    assert len(launcher.calls) == 1


def test_run_teardown_writes_teardown_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    launcher = _RecordingLauncher(exit_code=0)
    popup = KittyHopPopup(launcher=launcher)
    session = _make_session(tmp_path)

    popup.run_teardown(session, _devcontainer_backend())

    assert len(launcher.calls) == 1
    spec = json.loads(Path(launcher.calls[0][-1]).read_text())
    assert spec["kind"] == "teardown"
    assert spec["verb"] == f"Tearing down {session.session_name}"
    assert spec["steps"][0]["display"] == "compose down"


def test_run_prepare_raises_session_backend_error_on_non_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
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


def test_run_teardown_raises_session_backend_error_on_non_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
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
    assert argv[argv.index("--class") + 1] == POPUP_APP_ID
    # The error panel is still a bash one-liner (no lifecycle to run).
    assert argv[-3:-1] == ("bash", "-c")
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


# --- lifecycle spec ------------------------------------------------------


def test_lifecycle_spec_prepare_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = _make_session(tmp_path)

    spec = json.loads(
        _lifecycle_spec(session, ("compose up -d devcontainer",), kind="prepare", backend=_host_backend())
    )

    assert spec["kind"] == "prepare"
    assert spec["verb"] == f"Preparing {session.session_name}"
    assert spec["cwd"] == str(session.session_root)
    assert spec["log_path"].endswith(f"popup-{session.session_name}-prepare.log")
    assert len(spec["steps"]) == 1
    step = spec["steps"][0]
    assert step["display"] == "compose up -d devcontainer"
    # The composed argv is flock-guarded and carries the verbatim command.
    assert step["argv"][0] == "flock"
    assert "compose up -d devcontainer" in step["argv"]


def test_lifecycle_spec_lists_steps_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = _make_session(tmp_path)

    spec = json.loads(
        _lifecycle_spec(
            session, ("compose up", "install hop", "install kitten"), kind="prepare", backend=_host_backend()
        )
    )

    assert [step["display"] for step in spec["steps"]] == ["compose up", "install hop", "install kitten"]


def test_lifecycle_spec_teardown_verb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = _make_session(tmp_path)

    spec = json.loads(_lifecycle_spec(session, ("compose down",), kind="teardown", backend=_host_backend()))

    assert spec["kind"] == "teardown"
    assert spec["verb"] == f"Tearing down {session.session_name}"
    assert spec["steps"][0]["display"] == "compose down"


def test_lifecycle_spec_routes_remote_session_through_ssh_and_home_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For a remote session the popup must cd locally to the host home and run
    each step over the ssh transport — never `cd <remote project root>` (which
    only exists on the remote)."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
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

    spec = json.loads(_lifecycle_spec(session, ("podman-compose up -d",), kind="prepare", backend=backend))

    # The popup's own cwd is the local home, not the remote project root.
    assert spec["cwd"] == str(Path.home())
    argv = spec["steps"][0]["argv"]
    assert argv[0] == "flock"
    assert "ssh" in argv  # the step is transported over ssh


# --- run_popup_lifecycle (the popup-side executor) -----------------------


def test_run_popup_lifecycle_runs_steps_and_writes_clean_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path, log = _write_spec(
        tmp_path,
        steps=[
            {"display": "step one", "argv": ["sh", "-c", "printf 'ONE\\n'"]},
            {"display": "step two", "argv": ["sh", "-c", "printf 'TWO\\n'"]},
        ],
    )

    code = run_popup_lifecycle(spec_path)

    assert code == 0
    contents = log.read_text()
    assert "Preparing demo" in contents
    assert "$ step one" in contents
    assert "ONE" in contents
    assert "TWO" in contents
    # The tee'd log stays free of the spinner's cursor-control codes.
    assert "\x1b" not in contents
    assert "\r" not in contents
    # The output was streamed to the terminal (stderr) too.
    assert "ONE" in capsys.readouterr().err


def test_run_popup_lifecycle_holds_shell_and_returns_code_on_failure(tmp_path: Path) -> None:
    spec_path, log = _write_spec(
        tmp_path,
        steps=[{"display": "boom", "argv": ["sh", "-c", "printf 'DETAIL\\n' >&2; exit 3"]}],
    )
    held: list[bool] = []

    code = run_popup_lifecycle(spec_path, hold=lambda: held.append(True))

    assert code == 3
    assert held == [True]  # the held shell ran so the user can read the error
    contents = log.read_text()
    assert "DETAIL" in contents  # the failing step's output is logged
    assert "prepare failed (exit 3)" in contents


def test_run_popup_lifecycle_short_circuits_after_a_failing_step(tmp_path: Path) -> None:
    marker = tmp_path / "third-ran"
    spec_path, _log = _write_spec(
        tmp_path,
        steps=[
            {"display": "ok", "argv": ["sh", "-c", "true"]},
            {"display": "fail", "argv": ["sh", "-c", "exit 2"]},
            {"display": "third", "argv": ["sh", "-c", f"touch {marker}"]},
        ],
    )

    code = run_popup_lifecycle(spec_path, hold=lambda: None)

    assert code == 2
    assert not marker.exists()  # the step after the failure never ran


def test_run_popup_lifecycle_overwrites_log_each_run(tmp_path: Path) -> None:
    spec_first, log = _write_spec(tmp_path, steps=[{"display": "a", "argv": ["sh", "-c", "printf 'FIRST\\n'"]}])
    run_popup_lifecycle(spec_first)

    spec_second, log_again = _write_spec(tmp_path, steps=[{"display": "b", "argv": ["sh", "-c", "printf 'SECOND\\n'"]}])
    run_popup_lifecycle(spec_second)

    assert log == log_again  # same (session, kind) → same log path
    contents = log.read_text()
    assert "FIRST" not in contents  # truncated, not appended
    assert "SECOND" in contents


def test_hold_shell_drops_into_a_shell_that_exits_on_eof() -> None:
    """`_hold_shell` execs an interactive `sh` inheriting stdio. Point fd 0 at
    /dev/null so the shell reads EOF and returns immediately (what the user's
    Ctrl-D does), and run it in-process so coverage sees it — no mocks."""
    devnull = os.open(os.devnull, os.O_RDONLY)
    saved_stdin = os.dup(0)
    try:
        os.dup2(devnull, 0)
        _hold_shell()
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(devnull)


# --- error panel ---------------------------------------------------------


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
