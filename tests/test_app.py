import io
import json
import subprocess
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from hop.app import HopServices, execute_command
from hop.backends import CommandBackend, SessionBackend, SessionBackendError
from hop.bridge import BRIDGE_SHIM, render_bridge_shim
from hop.commands import (
    BridgeShimCommand,
    BrowserCommand,
    EnterSessionCommand,
    KillCommand,
    ListSessionsCommand,
    MoveCommand,
    OpenCommand,
    PathCommand,
    RunCommand,
    SwitchSessionCommand,
    TailCommand,
    TermCommand,
)
from hop.config import BackendConfig, HopConfig
from hop.errors import HopError
from hop.kitty import KittyRemoteControlAdapter, KittyWindow, KittyWindowContext, KittyWindowState
from hop.session import ProjectSession
from hop.state import SessionState
from hop.sway import SwayWindow


def _host_backend() -> CommandBackend:
    return CommandBackend(name="host", interactive_prefix="", noninteractive_prefix="")


def _is_host_backend(backend: object) -> bool:
    return (
        isinstance(backend, CommandBackend)
        and backend.name == "host"
        and backend.interactive_prefix == ""
        and backend.noninteractive_prefix == ""
    )


class StubSwayAdapter:
    def __init__(
        self,
        workspaces: tuple[str, ...] = (),
        *,
        focused_workspace: str = "",
        windows: tuple[SwayWindow, ...] = (),
    ) -> None:
        self.workspaces = workspaces
        self.focused_workspace = focused_workspace
        self.windows = windows
        self.switched_workspaces: list[str] = []
        self.layout_calls: list[tuple[str, str]] = []
        self.focused_window_ids: list[int] = []
        self.closed_windows: list[int] = []
        self.removed_workspaces: list[str] = []
        self.moved_windows: list[tuple[int, str]] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)
        # Reflect the switch so subsequent `get_focused_workspace` checks see
        # the new workspace — `enter_project_session` reads it back to decide
        # whether to re-issue the switch.
        self.focused_workspace = workspace_name

    def set_workspace_layout(self, workspace_name: str, layout: str) -> None:
        self.layout_calls.append((workspace_name, layout))

    def focus_window(self, window_id: int) -> None:
        # Tests don't currently assert on shell-focus behavior; the real
        # _focus_shell_if_present pass against an empty `windows` list is a
        # no-op, so we just record the calls in case a future test needs them.
        self.focused_window_ids.append(window_id)

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return tuple(workspace for workspace in self.workspaces if workspace.startswith(prefix))

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return self.windows

    def close_window(self, window_id: int) -> None:
        self.closed_windows.append(window_id)
        self.windows = tuple(window for window in self.windows if window.id != window_id)

    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None:
        self.moved_windows.append((window_id, workspace_name))

    def remove_workspace(self, workspace_name: str) -> None:
        self.removed_workspaces.append(workspace_name)

    def get_focused_workspace(self) -> str:
        return self.focused_workspace


class StubKittyAdapter:
    def __init__(
        self,
        *,
        last_cmd_output: str = "",
        alive_session_names: tuple[str, ...] = (),
    ) -> None:
        self.ensured_roles: list[tuple[str, str, Path]] = []
        self.already_prepared_flags: list[bool] = []
        self.runs: list[tuple[str, str, str, Path, bool]] = []
        self.closed_windows: list[int] = []
        self._last_cmd_output = last_cmd_output
        self._state_calls = 0
        self._alive_session_names = frozenset(alive_session_names)

    def is_alive(self, session: ProjectSession) -> bool:
        return session.session_name in self._alive_session_names

    def ensure_terminal(self, session: ProjectSession, *, role: str, already_prepared: bool = False) -> None:
        self.ensured_roles.append((session.session_name, role, session.project_root))
        self.already_prepared_flags.append(already_prepared)

    def run_in_terminal(
        self,
        session: ProjectSession,
        *,
        role: str,
        command: str,
        focus: bool = False,
    ) -> int:
        self.runs.append((session.session_name, role, command, session.project_root, focus))
        return 0

    def inspect_window(self, window_id: int, *, listen_on: str | None = None) -> KittyWindowContext | None:
        return None

    def list_session_windows(self, session: ProjectSession) -> list[KittyWindow]:
        return []

    def close_window(self, session_name: str, window_id: int) -> None:
        self.closed_windows.append(window_id)

    def get_window_state(self, session_name: str, window_id: int) -> KittyWindowState:
        self._state_calls += 1
        return KittyWindowState(at_prompt=self._state_calls > 1, last_cmd_exit_status=0)

    def get_last_cmd_output(self, session_name: str, window_id: int) -> str:
        return self._last_cmd_output


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.ensured_sessions: list[tuple[str, Path]] = []
        self.focused_sessions: list[tuple[str, Path]] = []
        self.opened_targets: list[tuple[str, str, Path]] = []

    def ensure(self, session: ProjectSession, *, keep_focus: bool = True) -> None:
        self.ensured_sessions.append((session.session_name, session.project_root))

    def focus(self, session: ProjectSession) -> None:
        self.focused_sessions.append((session.session_name, session.project_root))

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        self.opened_targets.append((session.session_name, target, session.project_root))


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, str | None]] = []

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        self.calls.append((session.session_name, session.project_root, url))


class StubHopPopup:
    """Stub `HopPopup`. Defaults to interactive (popup never fires) so
    existing tests preserve their byte-for-byte behavior; tests that exercise
    the headless code paths construct with `is_interactive=False`.
    """

    def __init__(
        self,
        *,
        is_interactive: bool = True,
        prepare_raises: BaseException | None = None,
        teardown_raises: BaseException | None = None,
    ) -> None:
        self._is_interactive = is_interactive
        self._prepare_raises = prepare_raises
        self._teardown_raises = teardown_raises
        self.prepare_calls: list[tuple[str, str | None]] = []
        self.teardown_calls: list[tuple[str, str | None]] = []
        self.shown_errors: list[HopError] = []

    def is_interactive(self) -> bool:
        return self._is_interactive

    def run_prepare(self, session: ProjectSession, backend: SessionBackend) -> None:
        self.prepare_calls.append((session.session_name, getattr(backend, "prepare_command", None)))
        if self._prepare_raises is not None:
            raise self._prepare_raises

    def run_teardown(self, session: ProjectSession, backend: SessionBackend) -> None:
        self.teardown_calls.append((session.session_name, getattr(backend, "teardown_command", None)))
        if self._teardown_raises is not None:
            raise self._teardown_raises

    def show_error(self, error: HopError) -> None:
        self.shown_errors.append(error)


@dataclass
class StubHopServices:
    sway: StubSwayAdapter
    kitty: StubKittyAdapter
    neovim: StubNeovimAdapter
    browser: StubBrowserAdapter
    persisted_session_names: tuple[str, ...] = ()
    popup: StubHopPopup = field(default_factory=StubHopPopup)

    def as_services(self) -> HopServices:
        from hop.app import SessionBackendRegistry

        # Treat each name in persisted_session_names as a previously-recorded
        # session — relevant for backend memory (`for_session` /
        # `resolve_for_entry`); the first-entry vs re-entry decision is
        # gated separately on `kitty.is_alive` (see StubKittyAdapter).
        persisted: dict[str, SessionState] = {
            name: SessionState(name=name, project_root=Path("/tmp") / name) for name in self.persisted_session_names
        }
        return HopServices(
            sway=self.sway,
            kitty=self.kitty,
            neovim=self.neovim,
            browser=self.browser,
            session_backends=SessionBackendRegistry(
                global_config_loader=lambda: HopConfig(),
                sessions_loader=lambda: persisted,
            ),
            popup=self.popup,
        )


def build_services(
    *,
    workspaces: tuple[str, ...] = (),
    focused_workspace: str = "",
    last_cmd_output: str = "",
    sway_windows: tuple[SwayWindow, ...] = (),
    persisted_session_names: tuple[str, ...] = (),
    alive_session_names: tuple[str, ...] | None = None,
) -> StubHopServices:
    # Default: a session is "alive" iff it's also persisted. Tests that need
    # to express the stale-state-but-dead-kitty case (post-reboot, manual
    # window close) override `alive_session_names` to a subset of (or empty
    # set vs) persisted_session_names.
    if alive_session_names is None:
        alive_session_names = persisted_session_names
    return StubHopServices(
        sway=StubSwayAdapter(
            workspaces=workspaces,
            focused_workspace=focused_workspace,
            windows=sway_windows,
        ),
        kitty=StubKittyAdapter(
            last_cmd_output=last_cmd_output,
            alive_session_names=alive_session_names,
        ),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        persisted_session_names=persisted_session_names,
    )


class CapturingKittyFactory:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.commands: list[tuple[str | None, str, Mapping[str, object] | None]] = []

    def __call__(self, listen_on: str | None = None) -> "_CapturedTransport":
        return _CapturedTransport(listen_on, self)


class _CapturedTransport:
    def __init__(self, listen_on: str | None, factory: CapturingKittyFactory) -> None:
        self._listen_on = listen_on
        self._factory = factory

    def send_command(self, command_name: str, payload: Mapping[str, object] | None = None) -> object:
        self._factory.commands.append((self._listen_on, command_name, payload))
        if not self._factory.responses:
            return {"ok": True}
        return self._factory.responses.pop(0)


class _NoopLauncher:
    def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> None:
        return None


def test_hop_enter_session_passes_invocation_directory_as_kitty_launch_cwd(
    tmp_path: Path,
) -> None:
    """End-to-end: cli `hop` from a directory must produce a kitty launch payload
    whose cwd is that exact directory."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    from hop.app import SessionBackendRegistry

    # Three responses, in order: (1) is_alive's `ls` probe before the
    # first-entry decision, (2) ensure_terminal's window-lookup `ls`,
    # (3) the launch ack itself. is_alive sees an empty windows list and
    # treats kitty as alive (no exception); _find_window also sees
    # empty so the bootstrap launch fires.
    factory = CapturingKittyFactory(
        [
            {"ok": True, "data": []},
            {"ok": True, "data": []},
            {"ok": True},
        ]
    )
    services = HopServices(
        sway=StubSwayAdapter(),
        kitty=KittyRemoteControlAdapter(transport_factory=factory, launcher=_NoopLauncher()),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        session_backends=SessionBackendRegistry(
            global_config_loader=lambda: HopConfig(),
            sessions_loader=lambda: {},
        ),
        popup=StubHopPopup(),
    )

    assert execute_command(EnterSessionCommand(), cwd=project_root, services=services) == 0

    launches = [payload for _, name, payload in factory.commands if name == "launch"]
    assert len(launches) == 1
    payload = launches[0]
    assert payload is not None
    assert payload["cwd"] == str(project_root.resolve())


def test_execute_command_enters_project_session_and_bootstraps_shell(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert execute_command(EnterSessionCommand(), cwd=nested_directory, services=services.as_services()) == 0
    assert services.sway.switched_workspaces == [f"p:{nested_directory.name}"]
    assert services.kitty.ensured_roles == [("src", "shell", nested_directory.resolve())]


def test_execute_command_spawns_extra_shell_when_focused_on_session_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    # Sway reports we're already focused on this session's workspace, so bare
    # `hop` should spawn another shell rather than re-enter. The editor is
    # *not* implicitly resurrected — a closed editor stays closed until the
    # user explicitly runs `hop open` (or picks the vicinae `Hop editor`
    # entry). Kitty is alive — the dead-kitty branch in spawn_session_terminal
    # does not fire and no extra `shell` ensure is added before `shell-2`.
    services = build_services(focused_workspace="p:demo", alive_session_names=("demo",))

    assert (
        execute_command(
            EnterSessionCommand(),
            cwd=project_root,
            services=services.as_services(),
        )
        == 0
    )
    assert services.sway.switched_workspaces == []
    assert services.kitty.ensured_roles == [("demo", "shell-2", project_root.resolve())]
    assert services.neovim.ensured_sessions == []


def test_execute_command_first_entry_brings_up_both_editor_and_shell(tmp_path: Path) -> None:
    """No persisted session state → this is bootstrap. Editor and shell
    both come up; editor first so the shell wins focus afterwards."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    services = build_services(focused_workspace="p:other", persisted_session_names=())

    assert (
        execute_command(
            EnterSessionCommand(),
            cwd=project_root,
            services=services.as_services(),
        )
        == 0
    )
    assert services.sway.switched_workspaces == ["p:demo"]
    assert services.kitty.ensured_roles == [("demo", "shell", project_root.resolve())]
    assert services.neovim.ensured_sessions == [("demo", project_root.resolve())]


def test_execute_command_applies_workspace_layout_from_config_on_first_entry(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    # Shell window must be visible to sway by the end of the sweep — the
    # layout pass is gated on ``_focus_shell_if_present`` finding it.
    shell_window = SwayWindow(id=1, workspace_name="p:demo", app_id="hop:shell", window_class=None)
    services = StubHopServices(
        sway=StubSwayAdapter(focused_workspace="p:other", windows=(shell_window,)),
        kitty=StubKittyAdapter(),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        persisted_session_names=(),
    )

    from hop.app import SessionBackendRegistry

    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(workspace_layout="tabbed"),
        sessions_loader=lambda: {},
    )
    real_services = HopServices(
        sway=services.sway,
        kitty=services.kitty,
        neovim=services.neovim,
        browser=services.browser,
        session_backends=registry,
        popup=services.popup,
    )

    assert execute_command(EnterSessionCommand(), cwd=project_root, services=real_services) == 0

    assert services.sway.layout_calls == [("p:demo", "tabbed")]


def test_execute_command_skips_workspace_layout_on_re_entry(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    services = StubHopServices(
        sway=StubSwayAdapter(focused_workspace="p:other"),
        kitty=StubKittyAdapter(alive_session_names=("demo",)),  # session's kitty is up
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        persisted_session_names=("demo",),  # already entered before
    )

    from hop.app import SessionBackendRegistry

    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(workspace_layout="tabbed"),
        sessions_loader=lambda: {
            "demo": SessionState(name="demo", project_root=project_root.resolve()),
        },
    )
    real_services = HopServices(
        sway=services.sway,
        kitty=services.kitty,
        neovim=services.neovim,
        browser=services.browser,
        session_backends=registry,
        popup=services.popup,
    )

    assert execute_command(EnterSessionCommand(), cwd=project_root, services=real_services) == 0

    # Re-entry: layout is not re-applied, only the shell is ensured.
    assert services.sway.layout_calls == []


def test_execute_command_re_entry_does_not_resurrect_a_closed_editor(tmp_path: Path) -> None:
    """Kitty is alive → user is returning from another workspace.
    Don't second-guess a deliberately-closed editor; just switch workspace
    and ensure the shell."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    services = build_services(
        focused_workspace="p:other",
        persisted_session_names=("demo",),
    )

    assert (
        execute_command(
            EnterSessionCommand(),
            cwd=project_root,
            services=services.as_services(),
        )
        == 0
    )
    assert services.sway.switched_workspaces == ["p:demo"]
    assert services.kitty.ensured_roles == [("demo", "shell", project_root.resolve())]
    assert services.neovim.ensured_sessions == []


def test_execute_command_runs_full_activation_when_state_is_stale_and_kitty_dead(
    tmp_path: Path,
) -> None:
    """A stale state file (kitty died after the last `hop kill`-less close)
    must not skip the activation sweep on the next bootstrap. The first-entry
    gate keys on `kitty.is_alive`, so persisted state with an unreachable
    kitty is treated as a fresh cold start: shell + editor both come up."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    services = build_services(
        focused_workspace="p:other",
        persisted_session_names=("demo",),  # state file lingers on disk…
        alive_session_names=(),  # …but kitty isn't reachable.
    )

    assert (
        execute_command(
            EnterSessionCommand(),
            cwd=project_root,
            services=services.as_services(),
        )
        == 0
    )
    assert services.kitty.ensured_roles == [("demo", "shell", project_root.resolve())]
    assert services.neovim.ensured_sessions == [("demo", project_root.resolve())]


def test_execute_command_switches_to_named_session() -> None:
    services = build_services(workspaces=("p:demo",))

    result = execute_command(
        SwitchSessionCommand(session_name="demo"), cwd=Path("/tmp"), services=services.as_services()
    )
    assert result == 0
    assert services.sway.switched_workspaces == ["p:demo"]


def test_execute_command_moves_focused_window_to_named_session() -> None:
    focused_window = SwayWindow(
        id=42,
        workspace_name="2",
        app_id="bitwarden",
        window_class=None,
        focused=True,
    )
    services = build_services(workspaces=("p:demo",), sway_windows=(focused_window,))

    result = execute_command(MoveCommand(session_name="demo"), cwd=Path("/tmp"), services=services.as_services())

    assert result == 0
    assert services.sway.moved_windows == [(42, "p:demo")]


def test_execute_command_move_raises_for_unknown_session() -> None:
    focused_window = SwayWindow(
        id=42,
        workspace_name="2",
        app_id="bitwarden",
        window_class=None,
        focused=True,
    )
    services = build_services(workspaces=("p:demo",), sway_windows=(focused_window,))

    with pytest.raises(HopError, match="no session named 'ghost'"):
        execute_command(MoveCommand(session_name="ghost"), cwd=Path("/tmp"), services=services.as_services())
    assert services.sway.moved_windows == []


def test_execute_command_lists_sorted_session_names() -> None:
    services = build_services(workspaces=("p:zeta", "workspace", "p:alpha"))
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert execute_command(ListSessionsCommand(), cwd=Path("/tmp"), services=services.as_services()) == 0

    assert stdout.getvalue() == "alpha\nzeta\n"


def test_execute_command_lists_sessions_as_json_with_project_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOP_SESSIONS_DIR", str(tmp_path / "sessions"))
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "alpha.json").write_text(json.dumps({"name": "alpha", "project_root": "/projects/alpha"}))

    services = build_services(workspaces=("p:zeta", "workspace", "p:alpha"))
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert (
            execute_command(
                ListSessionsCommand(as_json=True),
                cwd=Path("/tmp"),
                services=services.as_services(),
            )
            == 0
        )

    payload = json.loads(stdout.getvalue())
    assert payload == [
        {"name": "alpha", "workspace": "p:alpha", "project_root": "/projects/alpha"},
        {"name": "zeta", "workspace": "p:zeta", "project_root": None},
    ]


def test_execute_command_lists_windows_for_current_session(tmp_path: Path) -> None:
    """`hop windows` prints the resolved windows for the session whose
    project root is the caller's cwd. Used by the vicinae launcher to
    enumerate options for the focused session workspace."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    services = StubHopServices(
        sway=StubSwayAdapter(),
        kitty=StubKittyAdapter(),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
    )

    from hop.app import SessionBackendRegistry
    from hop.commands import ListWindowsCommand
    from hop.config import LayoutConfig, WindowConfig

    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(
            layouts=(
                LayoutConfig(
                    name="rails",
                    activate="true",
                    windows=(WindowConfig(role="server", command="bin/dev"),),
                ),
            ),
            windows=(WindowConfig(role="worker", command="bin/jobs"),),
        ),
        sessions_loader=lambda: {},
    )
    real_services = HopServices(
        sway=services.sway,
        kitty=services.kitty,
        neovim=services.neovim,
        browser=services.browser,
        session_backends=registry,
        popup=services.popup,
    )
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert execute_command(ListWindowsCommand(), cwd=project_root, services=real_services) == 0

    # Shell first, editor second, then user-declared roles in declaration
    # order (layout's `server` then top-level `worker`); built-in browser
    # at the end since it wasn't user-declared.
    assert stdout.getvalue() == "shell\neditor\nserver\nworker\nbrowser\n"


def test_execute_command_focuses_terminal_role_in_current_session(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert execute_command(TermCommand(role="test"), cwd=nested_directory, services=services.as_services()) == 0
    assert services.sway.switched_workspaces == []
    assert services.kitty.ensured_roles == [("src", "test", nested_directory.resolve())]


def test_execute_command_routes_run_commands_to_role_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    monkeypatch.setenv("HOP_RUNS_DIR", str(tmp_path / "runs"))

    services = build_services()
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert (
            execute_command(
                RunCommand(role="server", command_text="bin/dev"),
                cwd=nested_directory,
                services=services.as_services(),
            )
            == 0
        )
    assert services.sway.switched_workspaces == []
    assert services.kitty.runs == [("src", "server", "bin/dev", nested_directory.resolve(), False)]
    run_id = stdout.getvalue().strip()
    assert run_id
    assert (tmp_path / "runs" / f"{run_id}.json").is_file()


def test_execute_command_run_with_focus_switches_to_session_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    monkeypatch.setenv("HOP_RUNS_DIR", str(tmp_path / "runs"))

    services = build_services(focused_workspace="p:other")
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert (
            execute_command(
                RunCommand(role="server", command_text="bin/dev", focus=True),
                cwd=nested_directory,
                services=services.as_services(),
            )
            == 0
        )

    assert services.kitty.runs == [("src", "server", "bin/dev", nested_directory.resolve(), True)]
    assert services.sway.switched_workspaces == ["p:src"]
    run_id = stdout.getvalue().strip()
    assert (tmp_path / "runs" / f"{run_id}.json").is_file()


def test_execute_command_run_with_focus_skips_workspace_switch_when_already_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)
    monkeypatch.setenv("HOP_RUNS_DIR", str(tmp_path / "runs"))

    services = build_services(focused_workspace="p:src")
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        execute_command(
            RunCommand(role="server", command_text="bin/dev", focus=True),
            cwd=nested_directory,
            services=services.as_services(),
        )

    # Sway's `workspace_auto_back_and_forth` flips off the focused
    # workspace when re-targeted; --focus must no-op the switch in that
    # case so it doesn't yank the operator out of the session.
    assert services.sway.switched_workspaces == []


def test_execute_command_tails_run_output_to_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "abc.json").write_text(
        json.dumps({"window_id": 1, "session": "demo", "role": "test", "dispatched_at": 0.0})
    )
    monkeypatch.setenv("HOP_RUNS_DIR", str(runs_dir))

    services = build_services(last_cmd_output="hello\n")
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        assert (
            execute_command(
                TailCommand(run_id="abc"),
                cwd=tmp_path,
                services=services.as_services(),
            )
            == 0
        )

    assert stdout.getvalue() == "hello\n"


def test_execute_command_focuses_shared_editor_in_current_session(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert execute_command(OpenCommand(), cwd=nested_directory, services=services.as_services()) == 0
    assert services.sway.switched_workspaces == []
    assert services.neovim.focused_sessions == [("src", nested_directory.resolve())]


def test_execute_command_routes_file_open_targets_to_shared_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert (
        execute_command(
            OpenCommand(target="app/models/user.rb:42"),
            cwd=nested_directory,
            services=services.as_services(),
        )
        == 0
    )
    assert services.sway.switched_workspaces == []
    # CLI dispatch passes the target through as typed so the editor resolves
    # it against its own cwd (in the session's backend), not the host CLI cwd.
    assert services.neovim.opened_targets == [("src", "app/models/user.rb:42", nested_directory.resolve())]


def test_execute_command_routes_url_open_targets_to_session_browser(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert (
        execute_command(
            OpenCommand(target="https://example.com/path"),
            cwd=nested_directory,
            services=services.as_services(),
        )
        == 0
    )
    assert services.browser.calls == [("src", nested_directory.resolve(), "https://example.com/path")]
    assert services.neovim.opened_targets == []


def test_execute_command_uses_invocation_directory_for_browser_sessions(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert (
        execute_command(
            BrowserCommand(url="https://example.com"),
            cwd=nested_directory,
            services=services.as_services(),
        )
        == 0
    )
    assert services.sway.switched_workspaces == []
    assert services.browser.calls == [("src", nested_directory.resolve(), "https://example.com")]


def test_execute_command_kills_every_window_on_session_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    workspace_name = f"p:{project_root.name}"

    session_window = SwayWindow(
        id=11,
        workspace_name=workspace_name,
        app_id="kitty",
        window_class=None,
    )
    drifted_browser = SwayWindow(
        id=12,
        workspace_name="p:other",
        app_id="firefox",
        window_class=None,
        marks=("_hop_browser:demo",),
    )
    services = build_services(
        workspaces=(workspace_name,),
        sway_windows=(session_window, drifted_browser),
    )

    assert execute_command(KillCommand(), cwd=project_root, services=services.as_services()) == 0
    assert sorted(services.sway.closed_windows) == [11, 12]


# --- Popup-routed lifecycle commands -----------------------------------------


def _devcontainer_session(tmp_path: Path) -> tuple[Path, BackendConfig]:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    (project_root / "docker-compose.dev.yml").write_text("")
    return project_root, BackendConfig(
        name="devcontainer",
        activate="test -f docker-compose.dev.yml",
        prepare=("compose up -d devcontainer",),
        teardown=("compose down",),
        interactive_prefix="compose exec devcontainer",
        noninteractive_prefix="compose exec -T devcontainer",
    )


def _no_subprocess_runner(
    args: Sequence[str], cwd: Path, *, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """For tests where the popup is supposed to run prepare/teardown — the
    `SessionBackendRegistry`'s runner must not be exercised for those commands.
    The runner is still hit for activate and workspace_path probes; both succeed
    with empty stdout, which is what real-config compose / podman backends
    typically return for `test -f` and `pwd`."""
    del cwd, stdin
    return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")


def test_create_headless_runs_prepare_in_popup_after_eager_workspace_switch(tmp_path: Path) -> None:
    project_root, backend_config = _devcontainer_session(tmp_path)

    from hop.app import SessionBackendRegistry

    runner_calls: list[tuple[str, ...]] = []

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        runner_calls.append(tuple(args))
        return _no_subprocess_runner(args, cwd, stdin=stdin)

    sway_events: list[tuple[str, str]] = []

    class TrackingSway(StubSwayAdapter):
        def switch_to_workspace(self, workspace_name: str) -> None:
            sway_events.append(("switch", workspace_name))
            super().switch_to_workspace(workspace_name)

    class TrackingPopup(StubHopPopup):
        def run_prepare(self, session: ProjectSession, backend: SessionBackend) -> None:
            sway_events.append(("prepare", session.session_name))
            super().run_prepare(session, backend)

    popup = TrackingPopup(is_interactive=False)
    kitty_stub = StubKittyAdapter()
    services = HopServices(
        sway=TrackingSway(focused_workspace="p:other"),
        kitty=kitty_stub,
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        session_backends=SessionBackendRegistry(
            global_config_loader=lambda: HopConfig(backends=(backend_config,)),
            sessions_loader=lambda: {},
            runner=runner,
        ),
        popup=popup,
    )

    assert execute_command(EnterSessionCommand(), cwd=project_root, services=services) == 0

    # The popup ran prepare exactly once with the backend's prepare command.
    assert popup.prepare_calls == [("demo", ("compose up -d devcontainer",))]
    # The eager workspace switch happened BEFORE the popup runs prepare.
    assert sway_events == [("switch", "p:demo"), ("prepare", "demo")]
    # SessionBackendRegistry's runner skipped the prepare invocation
    # (`flock -o ... sh -c 'compose up -d devcontainer'`) — only activate and
    # workspace_path probes ran.
    assert all("compose up -d devcontainer" not in " ".join(args) for args in runner_calls)
    # The shell role was bootstrapped after prepare succeeded.
    assert kitty_stub.ensured_roles == [("demo", "shell", project_root.resolve())]


def test_create_headless_failure_aborts_bootstrap(tmp_path: Path) -> None:
    project_root, backend_config = _devcontainer_session(tmp_path)

    from hop.app import SessionBackendRegistry

    popup = StubHopPopup(
        is_interactive=False,
        prepare_raises=SessionBackendError("prepare failed", surfaced_by_popup=True),
    )
    sway = StubSwayAdapter(focused_workspace="p:other")
    kitty_stub = StubKittyAdapter()
    neovim_stub = StubNeovimAdapter()
    services = HopServices(
        sway=sway,
        kitty=kitty_stub,
        neovim=neovim_stub,
        browser=StubBrowserAdapter(),
        session_backends=SessionBackendRegistry(
            global_config_loader=lambda: HopConfig(backends=(backend_config,)),
            sessions_loader=lambda: {},
            runner=_no_subprocess_runner,
        ),
        popup=popup,
    )

    with pytest.raises(SessionBackendError) as excinfo:
        execute_command(EnterSessionCommand(), cwd=project_root, services=services)

    # The error carries the marker so cli.main won't pop a second error popup.
    assert excinfo.value.surfaced_by_popup is True
    # Workspace was switched eagerly even though prepare failed — the user
    # is still on the new workspace with the popup visible.
    assert sway.switched_workspaces == ["p:demo"]
    # Bootstrap was aborted: no kitty / editor ensure calls.
    assert kitty_stub.ensured_roles == []
    assert neovim_stub.ensured_sessions == []


def test_create_interactive_runs_prepare_inline_not_in_popup(tmp_path: Path) -> None:
    project_root, backend_config = _devcontainer_session(tmp_path)

    from hop.app import SessionBackendRegistry

    runner_calls: list[tuple[str, ...]] = []

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        runner_calls.append(tuple(args))
        return _no_subprocess_runner(args, cwd, stdin=stdin)

    popup = StubHopPopup(is_interactive=True)
    sway = StubSwayAdapter(focused_workspace="p:other")
    services = HopServices(
        sway=sway,
        kitty=StubKittyAdapter(),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        session_backends=SessionBackendRegistry(
            global_config_loader=lambda: HopConfig(backends=(backend_config,)),
            sessions_loader=lambda: {},
            runner=runner,
        ),
        popup=popup,
    )

    assert execute_command(EnterSessionCommand(), cwd=project_root, services=services) == 0

    # Popup was never asked to run prepare.
    assert popup.prepare_calls == []
    # The inline path ran the prepare command through the registry's runner.
    flock_calls = [args for args in runner_calls if args[:1] == ("flock",)]
    assert flock_calls and any("compose up -d devcontainer" in " ".join(args) for args in flock_calls)


def test_create_headless_without_prepare_command_still_bootstraps(tmp_path: Path) -> None:
    """Headless first-entry against a backend that has no `prepare` command
    (e.g. host) still works: the popup adapter's `run_prepare` is a no-op
    when `prepare_command is None`, and bootstrap proceeds normally."""
    project_root = tmp_path / "demo"
    project_root.mkdir()

    services = build_services(
        focused_workspace="p:other",
        persisted_session_names=(),
    )
    services.popup = StubHopPopup(is_interactive=False)

    assert execute_command(EnterSessionCommand(), cwd=project_root, services=services.as_services()) == 0
    # host backend has no prepare_command — stub still receives the call but
    # the test doesn't drill into what the real KittyHopPopup would do
    # (covered in test_popup.py).
    assert services.popup.prepare_calls == [("demo", None)]
    assert services.kitty.ensured_roles == [("demo", "shell", project_root.resolve())]


def test_create_headless_reentry_does_not_run_popup(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    services = build_services(
        focused_workspace="p:other",
        persisted_session_names=("demo",),
        alive_session_names=("demo",),
    )
    services.popup = StubHopPopup(is_interactive=False)

    assert execute_command(EnterSessionCommand(), cwd=project_root, services=services.as_services()) == 0
    # Re-entry: kitty is alive, prepare doesn't run inline OR in the popup.
    assert services.popup.prepare_calls == []


def test_kill_headless_delegates_teardown_to_popup_after_window_close(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    workspace_name = f"p:{project_root.name}"

    session_window = SwayWindow(id=21, workspace_name=workspace_name, app_id="kitty", window_class=None)

    events: list[str] = []

    class TrackingSway(StubSwayAdapter):
        def close_window(self, window_id: int) -> None:
            events.append(f"close-{window_id}")
            super().close_window(window_id)

    sway = TrackingSway(workspaces=(workspace_name,), windows=(session_window,))

    class TrackingPopup(StubHopPopup):
        def run_teardown(self, session: ProjectSession, backend: SessionBackend) -> None:
            events.append(f"teardown-{session.session_name}")
            super().run_teardown(session, backend)

    popup = TrackingPopup(is_interactive=False)

    services = StubHopServices(
        sway=sway,
        kitty=StubKittyAdapter(),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        popup=popup,
    )

    assert execute_command(KillCommand(), cwd=project_root, services=services.as_services()) == 0
    # Window-close ordering preserved: close fires before teardown.
    assert events == ["close-21", "teardown-demo"]
    # The popup recorded the teardown call (host backend has no teardown_command).
    assert popup.teardown_calls == [("demo", None)]


def test_kill_headless_teardown_failure_skips_forget(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()
    workspace_name = f"p:{project_root.name}"
    session_window = SwayWindow(id=21, workspace_name=workspace_name, app_id="kitty", window_class=None)
    sway = StubSwayAdapter(workspaces=(workspace_name,), windows=(session_window,))

    popup = StubHopPopup(
        is_interactive=False,
        teardown_raises=SessionBackendError("compose down failed", surfaced_by_popup=True),
    )

    services = StubHopServices(
        sway=sway,
        kitty=StubKittyAdapter(),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        popup=popup,
    )

    with pytest.raises(SessionBackendError):
        execute_command(KillCommand(), cwd=project_root, services=services.as_services())
    # Windows were still closed (closing happens before teardown).
    assert sway.closed_windows == [21]
    # Teardown was attempted exactly once.
    assert popup.teardown_calls == [("demo", None)]


def test_kill_interactive_does_not_invoke_popup(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    services = build_services()
    services.popup = StubHopPopup(is_interactive=True)

    assert execute_command(KillCommand(), cwd=project_root, services=services.as_services()) == 0
    assert services.popup.teardown_calls == []


# --- SessionBackendRegistry --------------------------------------------------


def _devcontainer_config() -> BackendConfig:
    return BackendConfig(
        name="devcontainer",
        activate="test -f docker-compose.dev.yml",
        prepare=("compose up -d devcontainer",),
        teardown=("compose down",),
        interactive_prefix="compose exec devcontainer",
        noninteractive_prefix="compose exec -T devcontainer",
    )


def _make_session(project_root: Path) -> ProjectSession:
    return ProjectSession(
        project_root=project_root,
        session_name=project_root.name,
        workspace_name=f"p:{project_root.name}",
    )


def test_session_base_registry_falls_back_to_host_when_no_config(tmp_path: Path) -> None:
    from hop.app import SessionBackendRegistry

    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(),
        sessions_loader=lambda: {},
    )

    backend = registry.resolve_for_entry(_make_session(tmp_path), backend_name=None)

    assert _is_host_backend(backend)


def test_session_base_registry_backend_host_returns_host_base(tmp_path: Path) -> None:
    import subprocess

    from hop.app import SessionBackendRegistry

    (tmp_path / "docker-compose.dev.yml").write_text("")
    config = HopConfig(backends=(_devcontainer_config(),))

    calls: list[tuple[str, ...]] = []

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: config,
        sessions_loader=lambda: {},
        runner=runner,
    )

    backend = registry.resolve_for_entry(_make_session(tmp_path), backend_name="host")

    assert _is_host_backend(backend)
    # host has no prepare/activate to run when pinned explicitly.
    assert calls == []


def test_session_base_registry_runs_activate_then_prepare(
    tmp_path: Path,
) -> None:
    import subprocess

    from hop.app import SessionBackendRegistry

    (tmp_path / "docker-compose.dev.yml").write_text("")
    config = HopConfig(backends=(_devcontainer_config(),))

    calls: list[tuple[str, ...]] = []

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: config,
        sessions_loader=lambda: {},
        runner=runner,
    )

    session = _make_session(tmp_path)
    backend = registry.resolve_for_entry(session, backend_name=None)

    assert isinstance(backend, CommandBackend)
    assert backend.interactive_prefix == "compose exec devcontainer"
    assert backend.noninteractive_prefix == "compose exec -T devcontainer"
    flock_args = calls[1][:3]
    assert flock_args[0] == "flock"
    assert flock_args[1] == "-o"
    assert flock_args[2].endswith(f"backend-{session.session_name}.lock")
    assert calls == [
        ("sh", "-c", "test -f docker-compose.dev.yml"),  # activate probe
        flock_args + ("sh", "-c", "compose up -d devcontainer"),
        ("sh", "-c", "compose exec -T devcontainer pwd"),  # workspace_path probe
    ]


def test_session_base_registry_skip_prepare_omits_prepare_and_probe(tmp_path: Path) -> None:
    """The headless popup path runs prepare inside a kitten panel, so
    `resolve_for_entry` is called with `skip_prepare=True`. In that mode both
    the prepare subprocess AND the workspace_path probe must be skipped — the
    probe needs the container up, which only happens after the popup-driven
    prepare. The caller is expected to invoke ``probe_workspace_path``
    explicitly once the popup returns."""
    import subprocess

    from hop.app import SessionBackendRegistry

    (tmp_path / "docker-compose.dev.yml").write_text("")
    config = HopConfig(backends=(_devcontainer_config(),))

    calls: list[tuple[str, ...]] = []

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: config,
        sessions_loader=lambda: {},
        runner=runner,
    )

    backend = registry.resolve_for_entry(_make_session(tmp_path), backend_name=None, skip_prepare=True)

    assert isinstance(backend, CommandBackend)
    # Only the activate probe ran; the prepare flock invocation and
    # workspace_path probe did NOT.
    assert calls == [
        ("sh", "-c", "test -f docker-compose.dev.yml"),  # activate
    ]
    assert backend.workspace_path is None

    # Now simulate the popup-driven prepare completing and the caller
    # invoking probe_workspace_path explicitly: the workspace_path probe
    # runs here, and the returned backend carries the captured path.
    backend = registry.probe_workspace_path(_make_session(tmp_path), backend)
    assert isinstance(backend, CommandBackend)
    assert calls[-1] == ("sh", "-c", "compose exec -T devcontainer pwd")


def test_session_base_registry_captures_workspace_path_from_probe(tmp_path: Path) -> None:
    """When the workspace_path probe returns a path, it's captured on the
    backend so the persisted record carries it and ``focused.paths_exist``
    can fall back to it for OSC-7-less shells."""
    import subprocess

    from hop.app import SessionBackendRegistry

    (tmp_path / "docker-compose.dev.yml").write_text("")
    config = HopConfig(backends=(_devcontainer_config(),))

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        del cwd, stdin
        stdout = "/workspace\n" if "pwd" in args[-1] else ""
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=stdout, stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: config,
        sessions_loader=lambda: {},
        runner=runner,
    )

    backend = registry.resolve_for_entry(_make_session(tmp_path), backend_name=None)

    assert isinstance(backend, CommandBackend)
    assert backend.workspace_path == "/workspace"


def test_session_base_registry_project_override_can_flip_autodetect(tmp_path: Path) -> None:
    """A project's `.hop.toml` overrides a backend's `default` command to
    pick a different backend than the global auto-detect would choose.
    """
    import subprocess

    from hop.app import SessionBackendRegistry

    # Two backends, both with activate probes that *would* succeed (test -e .).
    config = HopConfig(
        backends=(
            BackendConfig(
                name="primary",
                activate="test -e .",  # would normally win
                interactive_prefix="primary-prefix",
                noninteractive_prefix="primary-prefix",
            ),
            BackendConfig(
                name="secondary",
                activate="test -e .",
                interactive_prefix="secondary-prefix",
                noninteractive_prefix="secondary-prefix",
            ),
        )
    )

    # Project disables primary by overriding its activate probe to false.
    (tmp_path / ".hop.toml").write_text(
        """
[backends.primary]
activate = "false"
""",
    )

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        # Activate probes go through `sh -c <command>`. Match on the substituted
        # command string: project-overridden "false" → returncode 1; everything
        # else (the secondary backend's "test -e .") succeeds.
        if args == ("sh", "-c", "false"):
            return subprocess.CompletedProcess(args=list(args), returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: config,
        sessions_loader=lambda: {},
        runner=runner,
    )

    backend = registry.resolve_for_entry(_make_session(tmp_path), backend_name=None)

    assert isinstance(backend, CommandBackend)
    assert backend.name == "secondary"


def test_session_backend_registry_uses_project_only_backend_definition(tmp_path: Path) -> None:
    """A project's `.hop.toml` can define a brand-new backend (with shell+editor),
    selectable via --backend.
    """
    import subprocess

    from hop.app import SessionBackendRegistry

    (tmp_path / ".hop.toml").write_text(
        """
[backends.project-only]
activate                      = "true"
interactive_prefix                = "my-prefix"
noninteractive_prefix = "my-prefix"
""",
    )

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(),
        sessions_loader=lambda: {},
        runner=runner,
    )

    backend = registry.resolve_for_entry(_make_session(tmp_path), backend_name="project-only")

    assert isinstance(backend, CommandBackend)
    assert backend.name == "project-only"
    assert backend.interactive_prefix == "my-prefix"


def test_session_backend_registry_project_only_backend_wins_autodetect(tmp_path: Path) -> None:
    """A project-only backend with an `activate` that succeeds wins auto-detect
    when no global backend's activate does.
    """
    import subprocess

    from hop.app import SessionBackendRegistry

    (tmp_path / ".hop.toml").write_text(
        """
[backends.project-only]
activate                      = "true"
interactive_prefix                = "my-prefix"
noninteractive_prefix = "my-prefix"
""",
    )

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        if args == ("sh", "-c", "true"):
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=list(args), returncode=1, stdout="", stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(),
        sessions_loader=lambda: {},
        runner=runner,
    )

    backend = registry.resolve_for_entry(_make_session(tmp_path), backend_name=None)

    assert isinstance(backend, CommandBackend)
    assert backend.name == "project-only"


def test_session_backend_registry_for_session_returns_override(tmp_path: Path) -> None:
    from hop.app import SessionBackendRegistry

    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(),
        sessions_loader=lambda: {},
    )
    session = _make_session(tmp_path)
    override = CommandBackend(
        name="overridden",
        interactive_prefix="overridden-prefix",
        noninteractive_prefix="overridden-prefix",
    )
    registry.set_override(session.session_name, override)

    assert registry.for_session(session) is override

    registry.clear_override(session.session_name)
    assert _is_host_backend(registry.for_session(session))


def test_session_backend_registry_for_session_returns_persisted_command_backend(
    tmp_path: Path,
) -> None:
    from hop.app import SessionBackendRegistry
    from hop.state import CommandBackendRecord, SessionState

    persisted = {
        tmp_path.name: SessionState(
            name=tmp_path.name,
            project_root=tmp_path,
            backend=CommandBackendRecord(
                name="legacy",
                interactive_prefix="legacy-prefix",
                prepare=("legacy-prepare",),
                teardown=("legacy-teardown",),
                noninteractive_prefix="legacy-noninteractive",
            ),
        )
    }
    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(),
        sessions_loader=lambda: persisted,
    )

    backend = registry.for_session(_make_session(tmp_path))

    assert isinstance(backend, CommandBackend)
    assert backend.name == "legacy"
    assert backend.noninteractive_prefix == "legacy-noninteractive"


def test_session_backend_registry_for_session_returns_host_for_built_in_host_record(
    tmp_path: Path,
) -> None:
    """A persisted record with name=host and empty prefixes round-trips to
    the built-in host backend."""
    from hop.app import SessionBackendRegistry
    from hop.state import CommandBackendRecord, SessionState

    persisted = {
        tmp_path.name: SessionState(
            name=tmp_path.name,
            project_root=tmp_path,
            backend=CommandBackendRecord(name="host", interactive_prefix="", noninteractive_prefix=""),
        )
    }
    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(),
        sessions_loader=lambda: persisted,
    )

    assert _is_host_backend(registry.for_session(_make_session(tmp_path)))


def test_record_for_backend_round_trips_command_backend(tmp_path: Path) -> None:
    """The round-trip ``CommandBackend → record → CommandBackend`` preserves
    every field, including the host case (name=host, empty prefixes)."""
    from hop.app import _record_for_backend, backend_from_record  # pyright: ignore[reportPrivateUsage]
    from hop.state import CommandBackendRecord

    command = CommandBackend(
        name="devcontainer",
        interactive_prefix="compose exec devcontainer",
        prepare_command=("compose up -d",),
        teardown_command=("compose down",),
        noninteractive_prefix="compose exec -T devcontainer",
    )
    record = _record_for_backend(command)

    assert record == CommandBackendRecord(
        name="devcontainer",
        interactive_prefix="compose exec devcontainer",
        prepare=("compose up -d",),
        teardown=("compose down",),
        noninteractive_prefix="compose exec -T devcontainer",
    )

    restored = backend_from_record(record)
    assert isinstance(restored, CommandBackend)
    assert restored.interactive_prefix == "compose exec devcontainer"
    assert restored.noninteractive_prefix == "compose exec -T devcontainer"

    # Host round-trip: built-in backend ↔ built-in record.
    host_record = _record_for_backend(_host_backend())
    assert host_record == CommandBackendRecord(name="host", interactive_prefix="", noninteractive_prefix="")
    assert _is_host_backend(backend_from_record(host_record))


def test_persist_bootstrap_record_writes_session_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hop.app import _persist_bootstrap_record  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv("HOP_SESSIONS_DIR", str(tmp_path / "sessions"))
    session = ProjectSession(
        project_root=tmp_path,
        session_name="bootstrap",
        workspace_name="p:bootstrap",
    )
    backend = CommandBackend(
        name="devcontainer",
        interactive_prefix="compose exec devcontainer",
        noninteractive_prefix="compose exec -T devcontainer",
    )

    _persist_bootstrap_record(session, backend)

    payload = json.loads((tmp_path / "sessions" / "bootstrap.json").read_text())
    assert payload["backend"]["type"] == "command"
    assert payload["backend"]["name"] == "devcontainer"


def test_build_default_services_returns_real_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    from hop.app import build_default_services
    from hop.browser import SessionBrowserAdapter
    from hop.editor import SharedNeovimEditorAdapter
    from hop.sway import SwayIpcAdapter

    monkeypatch.setenv("HOP_SESSIONS_DIR", "/tmp/hop-test-sessions")
    services = build_default_services()

    assert isinstance(services.sway, SwayIpcAdapter)
    assert isinstance(services.kitty, KittyRemoteControlAdapter)
    assert isinstance(services.neovim, SharedNeovimEditorAdapter)
    assert isinstance(services.browser, SessionBrowserAdapter)


def test_build_kitten_services_wires_boss_kitty_editor_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kitten path must hand the editor adapter a BossKittyEditorIO so
    file-open dispatches drive nvim through the boss API instead of IPC,
    which would deadlock kitty's event loop while handle_result is running."""
    from hop.app import build_kitten_services
    from hop.editor import BossKittyEditorIO, SharedNeovimEditorAdapter

    monkeypatch.setenv("HOP_SESSIONS_DIR", "/tmp/hop-test-sessions")
    services = build_kitten_services(boss=object())

    assert isinstance(services.neovim, SharedNeovimEditorAdapter)
    # Hop's Protocol-typed kitty_io is private to the adapter; reach in by
    # name to verify the boss-based variant was chosen.
    assert isinstance(services.neovim._kitty_io, BossKittyEditorIO)  # type: ignore[attr-defined]  # pyright: ignore[reportPrivateUsage]


def test_session_base_registry_persisted_state_wins_over_autodetect(tmp_path: Path) -> None:
    from hop.app import SessionBackendRegistry
    from hop.state import CommandBackendRecord, SessionState

    (tmp_path / "docker-compose.dev.yml").write_text("")
    persisted = {
        tmp_path.name: SessionState(
            name=tmp_path.name,
            project_root=tmp_path,
            backend=CommandBackendRecord(
                name="legacy",
                interactive_prefix="legacy-prefix",
                noninteractive_prefix="legacy-prefix -T",
            ),
        )
    }

    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(backends=(_devcontainer_config(),)),
        sessions_loader=lambda: persisted,
    )

    backend = registry.resolve_for_entry(_make_session(tmp_path), backend_name=None)

    assert isinstance(backend, CommandBackend)
    assert backend.name == "legacy"
    assert backend.noninteractive_prefix == "legacy-prefix -T"


def test_session_base_registry_re_resolves_when_kitty_dead(tmp_path: Path) -> None:
    """Persisted record acts as cache only while kitty is alive. When the
    kitty has died (stale state file), `resolve_for_entry` re-probes
    against current config + filesystem state instead of replaying the
    recorded backend — so a session whose recorded preconditions are
    gone (e.g. compose file removed) re-resolves cleanly to host."""
    import subprocess

    from hop.app import SessionBackendRegistry
    from hop.state import CommandBackendRecord, SessionState

    # Compose file is gone: the recorded devcontainer's preconditions no
    # longer hold, so the next cold bootstrap should fall back to host.
    persisted = {
        tmp_path.name: SessionState(
            name=tmp_path.name,
            project_root=tmp_path,
            backend=CommandBackendRecord(
                name="devcontainer",
                interactive_prefix="compose exec devcontainer",
                prepare=("compose up -d devcontainer",),
                noninteractive_prefix="compose exec -T devcontainer",
            ),
        )
    }

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        del cwd
        # The devcontainer probe `test -f docker-compose.dev.yml` fails (no
        # file present); the built-in host's "true" probe succeeds, so the
        # auto-detect walk falls through to host as the implicit last match.
        if args == ("sh", "-c", "true"):
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=list(args), returncode=1, stdout="", stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(backends=(_devcontainer_config(),)),
        sessions_loader=lambda: persisted,
        runner=runner,
    )

    backend = registry.resolve_for_entry(
        _make_session(tmp_path),
        backend_name=None,
        kitty_alive=False,
    )

    assert _is_host_backend(backend)


def test_execute_bridge_shim_prints_shim_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    services = build_services().as_services()

    assert execute_command(BridgeShimCommand(), cwd=tmp_path, services=services) == 0
    captured = capsys.readouterr()
    assert captured.out == BRIDGE_SHIM
    assert "/run/hop.sock" in captured.out
    assert captured.err == ""


def test_execute_bridge_shim_bakes_socket_flag_into_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    services = build_services().as_services()
    custom_socket = "/run/user/1000/hop/api.sock"

    rc = execute_command(BridgeShimCommand(socket=custom_socket), cwd=tmp_path, services=services)

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == render_bridge_shim(custom_socket)
    assert custom_socket in captured.out
    assert "/run/hop.sock" not in captured.out
    assert captured.err == ""


def test_execute_path_prints_kitten_main_py(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    services = build_services().as_services()

    rc = execute_command(PathCommand(name="kitten/hints"), cwd=tmp_path, services=services)

    assert rc == 0
    captured = capsys.readouterr()
    printed = Path(captured.out.strip())
    assert printed.is_file()
    assert printed.name == "main.py"
    assert printed.parent.name == "hints"


def test_execute_path_prints_sway_script(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    services = build_services().as_services()

    rc = execute_command(PathCommand(name="sway/term-or-kitty"), cwd=tmp_path, services=services)

    assert rc == 0
    captured = capsys.readouterr()
    printed = Path(captured.out.strip())
    assert printed.is_file()
    assert printed.name == "term-or-kitty"


def test_execute_path_rejects_unknown_name(tmp_path: Path) -> None:
    services = build_services().as_services()

    with pytest.raises(ValueError, match="unknown hop path"):
        execute_command(PathCommand(name="nope/missing"), cwd=tmp_path, services=services)


def test_execute_path_rejects_traversal(tmp_path: Path) -> None:
    services = build_services().as_services()

    with pytest.raises(ValueError, match="invalid hop path"):
        execute_command(PathCommand(name="../etc/passwd"), cwd=tmp_path, services=services)
