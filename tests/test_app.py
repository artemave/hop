import io
import json
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from hop.app import HopServices, execute_command
from hop.backends import CommandBackend, HostBackend
from hop.commands import (
    BrowserCommand,
    EditCommand,
    EnterSessionCommand,
    KillCommand,
    ListSessionsCommand,
    RunCommand,
    SwitchSessionCommand,
    TailCommand,
    TermCommand,
)
from hop.config import BackendConfig, HopConfig
from hop.kitty import KittyRemoteControlAdapter, KittyWindow, KittyWindowContext, KittyWindowState
from hop.session import ProjectSession
from hop.sway import SwayWindow


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
        self.closed_windows: list[int] = []
        self.removed_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)

    def list_session_workspaces(self, *, prefix: str = "p:") -> tuple[str, ...]:
        return tuple(workspace for workspace in self.workspaces if workspace.startswith(prefix))

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return self.windows

    def close_window(self, window_id: int) -> None:
        self.closed_windows.append(window_id)
        self.windows = tuple(window for window in self.windows if window.id != window_id)

    def remove_workspace(self, workspace_name: str) -> None:
        self.removed_workspaces.append(workspace_name)

    def get_focused_workspace(self) -> str:
        return self.focused_workspace


class StubKittyAdapter:
    def __init__(self, *, last_cmd_output: str = "") -> None:
        self.ensured_roles: list[tuple[str, str, Path]] = []
        self.runs: list[tuple[str, str, str, Path]] = []
        self.closed_windows: list[int] = []
        self._last_cmd_output = last_cmd_output
        self._state_calls = 0

    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
        self.ensured_roles.append((session.session_name, role, session.project_root))

    def run_in_terminal(self, session: ProjectSession, *, role: str, command: str) -> int:
        self.runs.append((session.session_name, role, command, session.project_root))
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
        self.focused_sessions: list[tuple[str, Path]] = []
        self.opened_targets: list[tuple[str, str, Path]] = []

    def focus(self, session: ProjectSession) -> None:
        self.focused_sessions.append((session.session_name, session.project_root))

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        self.opened_targets.append((session.session_name, target, session.project_root))


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, str | None]] = []

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        self.calls.append((session.session_name, session.project_root, url))


@dataclass
class StubHopServices:
    sway: StubSwayAdapter
    kitty: StubKittyAdapter
    neovim: StubNeovimAdapter
    browser: StubBrowserAdapter

    def as_services(self) -> HopServices:
        from hop.app import SessionBackendRegistry

        return HopServices(
            sway=self.sway,
            kitty=self.kitty,
            neovim=self.neovim,
            browser=self.browser,
            session_backends=SessionBackendRegistry(
                global_config_loader=lambda: HopConfig(),
                sessions_loader=lambda: {},
            ),
        )


def build_services(
    *,
    workspaces: tuple[str, ...] = (),
    focused_workspace: str = "",
    last_cmd_output: str = "",
    sway_windows: tuple[SwayWindow, ...] = (),
) -> StubHopServices:
    return StubHopServices(
        sway=StubSwayAdapter(
            workspaces=workspaces,
            focused_workspace=focused_workspace,
            windows=sway_windows,
        ),
        kitty=StubKittyAdapter(last_cmd_output=last_cmd_output),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
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

    factory = CapturingKittyFactory([{"ok": True, "data": []}, {"ok": True}])
    services = HopServices(
        sway=StubSwayAdapter(),
        kitty=KittyRemoteControlAdapter(transport_factory=factory, launcher=_NoopLauncher()),
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        session_backends=SessionBackendRegistry(
            global_config_loader=lambda: HopConfig(),
            sessions_loader=lambda: {},
        ),
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
    # `hop` should spawn another shell rather than re-enter.
    services = build_services(focused_workspace="p:demo")

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


def test_execute_command_enters_session_when_focused_on_a_different_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    project_root.mkdir()

    services = build_services(focused_workspace="p:other")

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


def test_execute_command_switches_to_named_session() -> None:
    services = build_services(workspaces=("p:demo",))

    result = execute_command(
        SwitchSessionCommand(session_name="demo"), cwd=Path("/tmp"), services=services.as_services()
    )
    assert result == 0
    assert services.sway.switched_workspaces == ["p:demo"]


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
    assert services.kitty.runs == [("src", "server", "bin/dev", nested_directory.resolve())]
    run_id = stdout.getvalue().strip()
    assert run_id
    assert (tmp_path / "runs" / f"{run_id}.json").is_file()


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

    assert execute_command(EditCommand(), cwd=nested_directory, services=services.as_services()) == 0
    assert services.sway.switched_workspaces == []
    assert services.neovim.focused_sessions == [("src", nested_directory.resolve())]


def test_execute_command_routes_edit_targets_to_shared_editor(tmp_path: Path) -> None:
    project_root = tmp_path / "demo"
    nested_directory = project_root / "src"
    nested_directory.mkdir(parents=True)

    services = build_services()

    assert (
        execute_command(
            EditCommand(target="app/models/user.rb:42"),
            cwd=nested_directory,
            services=services.as_services(),
        )
        == 0
    )
    assert services.sway.switched_workspaces == []
    assert services.neovim.opened_targets == [("src", "app/models/user.rb:42", nested_directory.resolve())]


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


# --- SessionBackendRegistry --------------------------------------------------


def _devcontainer_config() -> BackendConfig:
    return BackendConfig(
        name="devcontainer",
        default=("test", "-f", "docker-compose.dev.yml"),
        prepare=("compose", "up", "-d", "devcontainer"),
        shell=("compose", "exec", "devcontainer", "/usr/bin/zsh"),
        editor=("compose", "exec", "devcontainer", "nvim", "--listen", "{listen_addr}"),
        teardown=("compose", "down"),
        workspace=("compose", "exec", "devcontainer", "pwd"),
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

    assert isinstance(backend, HostBackend)


def test_session_base_registry_backend_host_returns_host_base(tmp_path: Path) -> None:
    import subprocess

    from hop.app import SessionBackendRegistry

    (tmp_path / "docker-compose.dev.yml").write_text("")
    config = HopConfig(backends=(_devcontainer_config(),))

    calls: list[tuple[str, ...]] = []

    def runner(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: config,
        sessions_loader=lambda: {},
        runner=runner,
    )

    backend = registry.resolve_for_entry(_make_session(tmp_path), backend_name="host")

    assert isinstance(backend, HostBackend)
    assert calls == []  # host short-circuits before any command runs


def test_session_base_registry_runs_default_then_prepare_and_discovers_workspace(
    tmp_path: Path,
) -> None:
    import subprocess

    from hop.app import SessionBackendRegistry

    (tmp_path / "docker-compose.dev.yml").write_text("")
    config = HopConfig(backends=(_devcontainer_config(),))

    calls: list[tuple[str, ...]] = []

    def runner(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        if "pwd" in args:
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="/workspace\n", stderr="")
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: config,
        sessions_loader=lambda: {},
        runner=runner,
    )

    session = _make_session(tmp_path)
    backend = registry.resolve_for_entry(session, backend_name=None)

    assert isinstance(backend, CommandBackend)
    assert backend.workspace_path == "/workspace"
    flock_args = calls[1][:2]
    assert flock_args[0] == "flock"
    assert flock_args[1].endswith(f"backend-{session.session_name}.lock")
    assert calls == [
        ("test", "-f", "docker-compose.dev.yml"),  # default probe
        flock_args + ("compose", "up", "-d", "devcontainer"),
        ("compose", "exec", "devcontainer", "pwd"),
    ]


def test_session_base_registry_project_override_can_flip_autodetect(tmp_path: Path) -> None:
    """A project's `.hop.toml` overrides a backend's `default` command to
    pick a different backend than the global auto-detect would choose.
    """
    import subprocess

    from hop.app import SessionBackendRegistry

    # Two backends, both with defaults that *would* succeed (test -e .).
    config = HopConfig(
        backends=(
            BackendConfig(
                name="primary",
                default=("test", "-e", "."),  # would normally win
                shell=("primary-shell",),
                editor=("primary-editor",),
            ),
            BackendConfig(
                name="secondary",
                default=("test", "-e", "."),
                shell=("secondary-shell",),
                editor=("secondary-editor",),
            ),
        )
    )

    # Project disables primary by overriding its default to false.
    (tmp_path / ".hop.toml").write_text(
        """
[backends.primary]
default = ["false"]
""",
    )

    def runner(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        # Default probes: ["false"] → returncode 1; ["test", "-e", "."] → 0.
        if args[:1] == ("false",):
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
shell = ["my-shell"]
editor = ["my-editor", "--listen", "{listen_addr}"]
default = ["true"]
""",
    )

    def runner(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(),
        sessions_loader=lambda: {},
        runner=runner,
    )

    backend = registry.resolve_for_entry(_make_session(tmp_path), backend_name="project-only")

    assert isinstance(backend, CommandBackend)
    assert backend.name == "project-only"
    assert backend.shell == ("my-shell",)


def test_session_backend_registry_project_only_backend_wins_autodetect(tmp_path: Path) -> None:
    """A project-only backend with a `default` that succeeds wins auto-detect
    when no global backend's default does.
    """
    import subprocess

    from hop.app import SessionBackendRegistry

    (tmp_path / ".hop.toml").write_text(
        """
[backends.project-only]
shell = ["my-shell"]
editor = ["my-editor", "--listen", "{listen_addr}"]
default = ["true"]
""",
    )

    def runner(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args == ("true",):
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
        shell=("override-shell",),
        editor=("override-editor",),
    )
    registry.set_override(session.session_name, override)

    assert registry.for_session(session) is override

    registry.clear_override(session.session_name)
    assert isinstance(registry.for_session(session), HostBackend)


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
                shell=("legacy-shell",),
                editor=("legacy-editor",),
                prepare=("legacy-prepare",),
                teardown=("legacy-teardown",),
                workspace_command=("legacy-workspace",),
                workspace_path="/legacy",
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
    assert backend.workspace_path == "/legacy"


def test_session_backend_registry_for_session_returns_host_for_persisted_host_record(
    tmp_path: Path,
) -> None:
    from hop.app import SessionBackendRegistry
    from hop.state import HostBackendRecord, SessionState

    persisted = {
        tmp_path.name: SessionState(
            name=tmp_path.name,
            project_root=tmp_path,
            backend=HostBackendRecord(),
        )
    }
    registry = SessionBackendRegistry(
        global_config_loader=lambda: HopConfig(),
        sessions_loader=lambda: persisted,
    )

    assert isinstance(registry.for_session(_make_session(tmp_path)), HostBackend)


def test_record_for_backend_round_trips_command_and_host_backends(tmp_path: Path) -> None:
    from hop.app import _backend_from_record, _record_for_backend  # pyright: ignore[reportPrivateUsage]
    from hop.state import CommandBackendRecord, HostBackendRecord

    command = CommandBackend(
        name="devcontainer",
        shell=("compose", "exec", "devcontainer", "zsh"),
        editor=("compose", "exec", "devcontainer", "nvim"),
        prepare_command=("compose", "up", "-d"),
        teardown_command=("compose", "down"),
        workspace_command=("compose", "exec", "devcontainer", "pwd"),
        workspace_path="/workspace",
    )
    record = _record_for_backend(command)

    assert record == CommandBackendRecord(
        name="devcontainer",
        shell=("compose", "exec", "devcontainer", "zsh"),
        editor=("compose", "exec", "devcontainer", "nvim"),
        prepare=("compose", "up", "-d"),
        teardown=("compose", "down"),
        workspace_command=("compose", "exec", "devcontainer", "pwd"),
        workspace_path="/workspace",
    )

    host_record = _record_for_backend(HostBackend())
    assert host_record == HostBackendRecord()
    assert isinstance(_backend_from_record(host_record), HostBackend)


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
        shell=("compose", "exec", "devcontainer", "zsh"),
        editor=("compose", "exec", "devcontainer", "nvim"),
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
                shell=("legacy-shell",),
                editor=("legacy-editor",),
                workspace_path="/legacy",
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
    assert backend.workspace_path == "/legacy"
