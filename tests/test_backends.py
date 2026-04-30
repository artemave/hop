from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pytest

from hop.backends import (
    HostBackend,
    SessionBackendError,
    backend_from_config,
    select_backend,
)
from hop.config import BackendConfig
from hop.session import ProjectSession


def build_session(project_root: Path) -> ProjectSession:
    return ProjectSession(
        project_root=project_root,
        session_name=project_root.name,
        workspace_name=f"p:{project_root.name}",
    )


@dataclass
class RecordingRunner:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    calls: list[tuple[tuple[str, ...], Path]] = field(default_factory=lambda: [])

    def __call__(self, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(args), cwd))
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def make_backend(**kwargs: object) -> BackendConfig:
    defaults: dict[str, object] = {
        "name": "devcontainer",
        "default": "test -f docker-compose.dev.yml",
        "shell": "compose exec devcontainer /usr/bin/zsh",
        "editor": "compose exec devcontainer nvim --listen {listen_addr}",
        "prepare": "compose up -d devcontainer",
        "teardown": "compose down",
        "workspace": "compose exec devcontainer pwd",
    }
    defaults.update(kwargs)
    return BackendConfig(**defaults)  # type: ignore[arg-type]


def test_host_base_shell_args_is_empty(tmp_path: Path) -> None:
    assert HostBackend().shell_args(build_session(tmp_path)) == ()


def test_host_base_editor_args_uses_listen_addr(tmp_path: Path) -> None:
    addr = tmp_path / "nvim.sock"
    args = HostBackend().editor_args(build_session(tmp_path), addr)
    assert args == ("nvim", "--listen", str(addr))


def test_host_base_translate_terminal_cwd_is_identity(tmp_path: Path) -> None:
    session = build_session(tmp_path)
    cwd = tmp_path / "subdir"
    assert HostBackend().translate_terminal_cwd(session, cwd) == cwd


def test_command_backend_shell_substitutes_project_root(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(shell="ssh host cd {project_root} && exec zsh"))

    args = backend.shell_args(build_session(tmp_path))

    # Substituted shell-quoted, then wrapped in sh -c for execution.
    assert args == ("sh", "-c", f"ssh host cd {shlex.quote(str(tmp_path))} && exec zsh")


def test_command_backend_editor_substitutes_listen_addr(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend())
    addr = tmp_path / "nvim.sock"

    args = backend.editor_args(build_session(tmp_path), addr)

    assert args == (
        "sh",
        "-c",
        f"compose exec devcontainer nvim --listen {shlex.quote(str(addr))}",
    )


def test_command_backend_substitutes_path_with_spaces_safely(tmp_path: Path) -> None:
    """A project_root with spaces must round-trip through sh as one token."""
    weird = tmp_path / "name with spaces"
    weird.mkdir()
    backend = backend_from_config(make_backend(shell="cd {project_root} && pwd"))

    args = backend.shell_args(build_session(weird))

    # sh parses the substituted command and recovers the original path.
    completed = subprocess.run(args, capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == str(weird)


def test_command_backend_translate_terminal_cwd_uses_workspace_path(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(), workspace_path="/workspace")
    session = build_session(tmp_path)

    container_path = Path("/workspace/lib/foo.py")

    assert backend.translate_terminal_cwd(session, container_path) == tmp_path / "lib" / "foo.py"


def test_command_backend_translate_is_identity_without_workspace_path(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(), workspace_path=None)
    session = build_session(tmp_path)
    other = Path("/somewhere/else")

    assert backend.translate_terminal_cwd(session, other) == other


def test_command_backend_translate_leaves_unrelated_paths(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(), workspace_path="/workspace")
    session = build_session(tmp_path)

    other = Path("/other/path")

    assert backend.translate_terminal_cwd(session, other) == other


def test_command_backend_translate_host_path_rewrites_under_project_root(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(), workspace_path="/workspace")
    session = build_session(tmp_path)

    host_path = tmp_path / "lib" / "foo.py"

    assert backend.translate_host_path(session, host_path) == Path("/workspace/lib/foo.py")


def test_command_backend_translate_host_path_is_identity_outside_project(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(), workspace_path="/workspace")
    session = build_session(tmp_path)

    other = Path("/etc/hosts")

    assert backend.translate_host_path(session, other) == other


def test_command_backend_translate_host_path_is_identity_without_workspace(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(), workspace_path=None)
    session = build_session(tmp_path)

    host_path = tmp_path / "lib" / "foo.py"

    assert backend.translate_host_path(session, host_path) == host_path


def test_host_backend_translate_host_path_is_identity(tmp_path: Path) -> None:
    session = build_session(tmp_path)
    host_path = tmp_path / "lib" / "foo.py"

    assert HostBackend().translate_host_path(session, host_path) == host_path


def test_command_backend_prepare_runs_prepare_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(), runner=runner)

    backend.prepare(build_session(tmp_path))

    lock = tmp_path / "hop" / f"backend-{tmp_path.name}.lock"
    assert runner.calls == [(("flock", str(lock), "sh", "-c", "compose up -d devcontainer"), tmp_path)]


def test_command_backend_prepare_is_noop_without_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(prepare=None), runner=runner)

    backend.prepare(build_session(tmp_path))

    assert runner.calls == []


def test_command_backend_prepare_raises_on_failure(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=1, stderr="boom")
    backend = backend_from_config(make_backend(), runner=runner)

    with pytest.raises(SessionBackendError, match="prepare failed"):
        backend.prepare(build_session(tmp_path))


def test_command_backend_teardown_runs_teardown_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(), runner=runner)

    backend.teardown(build_session(tmp_path))

    lock = tmp_path / "hop" / f"backend-{tmp_path.name}.lock"
    assert runner.calls == [(("flock", str(lock), "sh", "-c", "compose down"), tmp_path)]


def test_command_backend_teardown_raises_on_failure(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=2, stderr="nope")
    backend = backend_from_config(make_backend(), runner=runner)

    with pytest.raises(SessionBackendError, match="teardown failed"):
        backend.teardown(build_session(tmp_path))


def test_command_backend_prepare_and_teardown_share_a_session_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same lock path must wrap both prepare and teardown so that a
    # `hop` invocation that follows a still-running `hop kill` blocks until
    # the teardown subprocess exits, instead of racing it inside
    # podman-compose.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(), runner=runner)
    session = build_session(tmp_path)

    backend.prepare(session)
    backend.teardown(session)

    prepare_args, _ = runner.calls[0]
    teardown_args, _ = runner.calls[1]
    assert prepare_args[:2] == ("flock", str(tmp_path / "hop" / f"backend-{tmp_path.name}.lock"))
    assert teardown_args[:2] == prepare_args[:2]


def test_command_backend_discover_workspace_returns_stdout(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="/workspace\n")
    backend = backend_from_config(make_backend(), runner=runner)

    workspace = backend.discover_workspace(build_session(tmp_path))

    assert workspace == "/workspace"
    assert runner.calls == [(("sh", "-c", "compose exec devcontainer pwd"), tmp_path)]


def test_command_backend_discover_workspace_returns_none_without_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(workspace=None), runner=runner)

    assert backend.discover_workspace(build_session(tmp_path)) is None
    assert runner.calls == []


def test_command_backend_discover_workspace_raises_on_failure(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=3, stderr="bad")
    backend = backend_from_config(make_backend(), runner=runner)

    with pytest.raises(SessionBackendError, match="workspace discovery failed"):
        backend.discover_workspace(build_session(tmp_path))


def test_command_backend_discover_workspace_substitutes_project_root(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="/workspace\n")
    backend = backend_from_config(make_backend(workspace="lookup {project_root}"), runner=runner)

    backend.discover_workspace(build_session(tmp_path))

    assert runner.calls == [(("sh", "-c", f"lookup {shlex.quote(str(tmp_path))}"), tmp_path)]


def test_command_backend_with_workspace_path_returns_new_instance(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend())

    bound = backend.with_workspace_path("/workspace")

    assert bound.workspace_path == "/workspace"
    assert backend.workspace_path is None  # original is unchanged


def test_command_backend_with_workspace_path_preserves_translate_commands(tmp_path: Path) -> None:
    backend = backend_from_config(
        make_backend(
            port_translate="echo 1234",
            host_translate="echo myhost",
        )
    )

    bound = backend.with_workspace_path("/workspace")

    assert bound.port_translate_command == "echo 1234"
    assert bound.host_translate_command == "echo myhost"


def test_host_backend_translate_localhost_url_is_identity(tmp_path: Path) -> None:
    session = build_session(tmp_path)
    assert HostBackend().translate_localhost_url(session, "http://localhost:3000/foo") == "http://localhost:3000/foo"


def test_command_backend_translate_localhost_url_is_identity_when_no_commands(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(port_translate=None, host_translate=None), runner=runner)

    assert (
        backend.translate_localhost_url(build_session(tmp_path), "http://localhost:3000/foo")
        == "http://localhost:3000/foo"
    )
    assert runner.calls == []


def test_command_backend_translate_localhost_url_skips_non_localhost(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="9999\n")
    backend = backend_from_config(
        make_backend(port_translate="echo 9999"),
        runner=runner,
    )

    assert (
        backend.translate_localhost_url(build_session(tmp_path), "https://example.com:3000/foo")
        == "https://example.com:3000/foo"
    )
    assert runner.calls == []


def test_command_backend_translate_localhost_url_replaces_port(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="35231\n")
    backend = backend_from_config(
        make_backend(port_translate="compose port devcontainer {port}"),
        runner=runner,
    )

    translated = backend.translate_localhost_url(
        build_session(tmp_path),
        "http://localhost:3000/path?q=1#frag",
    )

    assert translated == "http://localhost:35231/path?q=1#frag"
    assert runner.calls == [(("sh", "-c", "compose port devcontainer 3000"), tmp_path)]


def test_command_backend_translate_localhost_url_treats_127_0_0_1_as_localhost(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="35231")
    backend = backend_from_config(make_backend(port_translate="compose port {port}"), runner=runner)

    assert (
        backend.translate_localhost_url(build_session(tmp_path), "http://127.0.0.1:3000/") == "http://127.0.0.1:35231/"
    )


def test_command_backend_translate_localhost_url_treats_0_0_0_0_as_localhost(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="35231")
    backend = backend_from_config(make_backend(port_translate="compose port {port}"), runner=runner)

    assert backend.translate_localhost_url(build_session(tmp_path), "http://0.0.0.0:3000/") == "http://0.0.0.0:35231/"


def test_command_backend_translate_localhost_url_replaces_host(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="myserver.example.com\n")
    backend = backend_from_config(
        make_backend(host_translate="echo myserver.example.com"),
        runner=runner,
    )

    translated = backend.translate_localhost_url(
        build_session(tmp_path),
        "http://localhost:3000/foo",
    )

    assert translated == "http://myserver.example.com:3000/foo"
    assert runner.calls == [(("sh", "-c", "echo myserver.example.com"), tmp_path)]


def test_command_backend_translate_localhost_url_runs_both_when_both_set(tmp_path: Path) -> None:
    host_runner_calls: list[tuple[tuple[str, ...], Path]] = []
    port_runner_calls: list[tuple[tuple[str, ...], Path]] = []

    def runner(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        # args is ("sh", "-c", "<command>"); discriminate by command text.
        if "host-cmd" in args[2]:
            host_runner_calls.append((tuple(args), cwd))
            return subprocess.CompletedProcess(list(args), 0, "myserver\n", "")
        port_runner_calls.append((tuple(args), cwd))
        return subprocess.CompletedProcess(list(args), 0, "35231\n", "")

    backend = backend_from_config(
        make_backend(
            host_translate="host-cmd",
            port_translate="port-cmd {port}",
        ),
        runner=runner,
    )

    translated = backend.translate_localhost_url(
        build_session(tmp_path),
        "http://localhost:3000/",
    )

    assert translated == "http://myserver:35231/"
    assert host_runner_calls == [(("sh", "-c", "host-cmd"), tmp_path)]
    assert port_runner_calls == [(("sh", "-c", "port-cmd 3000"), tmp_path)]


def test_command_backend_translate_localhost_url_runs_with_empty_port_when_url_has_none(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="myserver")
    backend = backend_from_config(
        make_backend(host_translate="host-cmd '{port}'"),
        runner=runner,
    )

    translated = backend.translate_localhost_url(build_session(tmp_path), "http://localhost/")

    assert translated == "http://myserver/"
    assert runner.calls == [(("sh", "-c", "host-cmd ''"), tmp_path)]


def test_command_backend_translate_localhost_url_substitutes_project_root(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="35231")
    backend = backend_from_config(
        make_backend(port_translate="lookup {project_root} {port}"),
        runner=runner,
    )

    backend.translate_localhost_url(build_session(tmp_path), "http://localhost:3000/")

    assert runner.calls == [(("sh", "-c", f"lookup {shlex.quote(str(tmp_path))} 3000"), tmp_path)]


def test_command_backend_translate_localhost_url_preserves_userinfo(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="35231")
    backend = backend_from_config(make_backend(port_translate="p {port}"), runner=runner)

    translated = backend.translate_localhost_url(
        build_session(tmp_path),
        "http://user:pw@localhost:3000/foo",
    )

    assert translated == "http://user:pw@localhost:35231/foo"


def test_command_backend_translate_localhost_url_raises_on_nonzero_exit(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=1, stderr="container is gone")
    backend = backend_from_config(make_backend(port_translate="p {port}"), runner=runner)

    with pytest.raises(SessionBackendError, match="port_translate failed"):
        backend.translate_localhost_url(build_session(tmp_path), "http://localhost:3000/")


def test_command_backend_translate_localhost_url_raises_on_empty_stdout(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="   \n")
    backend = backend_from_config(make_backend(port_translate="p {port}"), runner=runner)

    with pytest.raises(SessionBackendError, match="port_translate returned empty output"):
        backend.translate_localhost_url(build_session(tmp_path), "http://localhost:3000/")


def test_command_backend_translate_localhost_url_raises_when_port_translate_returns_non_numeric(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="not-a-port\n")
    backend = backend_from_config(make_backend(port_translate="p {port}"), runner=runner)

    with pytest.raises(SessionBackendError, match="non-numeric"):
        backend.translate_localhost_url(build_session(tmp_path), "http://localhost:3000/")


def test_command_backend_translate_localhost_url_host_translate_failure(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=2, stderr="dns failed")
    backend = backend_from_config(make_backend(host_translate="h"), runner=runner)

    with pytest.raises(SessionBackendError, match="host_translate failed"):
        backend.translate_localhost_url(build_session(tmp_path), "http://localhost:3000/")


def test_select_backend_returns_none_when_no_backends() -> None:
    project_root = Path("/tmp/demo")

    assert select_backend(build_session(project_root), ()) is None


def test_select_backend_returns_first_default_command_to_succeed(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backends = (
        make_backend(name="a", default="a-default"),
        make_backend(name="b", default="b-default"),
    )

    chosen = select_backend(build_session(tmp_path), backends, runner=runner)

    assert chosen is not None
    assert chosen.name == "a"
    assert runner.calls == [(("sh", "-c", "a-default"), tmp_path)]


def test_select_backend_walks_until_a_default_succeeds(tmp_path: Path) -> None:
    @dataclass
    class ScriptedRunner:
        scripts: list[int]
        calls: list[tuple[tuple[str, ...], Path]] = field(default_factory=lambda: [])

        def __call__(self, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            self.calls.append((tuple(args), cwd))
            code = self.scripts.pop(0)
            return subprocess.CompletedProcess(args=list(args), returncode=code, stdout="", stderr="")

    runner = ScriptedRunner(scripts=[1, 0])
    backends = (
        make_backend(name="a", default="a-default"),
        make_backend(name="b", default="b-default"),
    )

    chosen = select_backend(build_session(tmp_path), backends, runner=runner)

    assert chosen is not None
    assert chosen.name == "b"


def test_select_backend_skips_backends_without_default(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backends = (
        make_backend(name="a", default=None),
        make_backend(name="b", default="b-default"),
    )

    chosen = select_backend(build_session(tmp_path), backends, runner=runner)

    assert chosen is not None
    assert chosen.name == "b"
    assert runner.calls == [(("sh", "-c", "b-default"), tmp_path)]


def test_select_backend_returns_none_when_no_default_succeeds(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=1)
    backends = (
        make_backend(name="a", default="a-default"),
        make_backend(name="b", default="b-default"),
    )

    assert select_backend(build_session(tmp_path), backends, runner=runner) is None


def test_select_backend_pinned_name_skips_default_probes(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backends = (
        make_backend(name="a", default="a-default"),
        make_backend(name="b", default="b-default"),
    )

    chosen = select_backend(build_session(tmp_path), backends, pinned_name="b", runner=runner)

    assert chosen is not None
    assert chosen.name == "b"
    assert runner.calls == []


def test_select_backend_pinned_host_returns_none(tmp_path: Path) -> None:
    backends = (make_backend(),)

    assert select_backend(build_session(tmp_path), backends, pinned_name="host") is None


def test_select_backend_pinned_unknown_name_raises(tmp_path: Path) -> None:
    from hop.backends import UnknownBackendError

    backends = (make_backend(),)

    with pytest.raises(UnknownBackendError, match="nonexistent"):
        select_backend(build_session(tmp_path), backends, pinned_name="nonexistent")


def test_command_backend_teardown_is_noop_without_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(teardown=None), runner=runner)

    backend.teardown(build_session(tmp_path))

    assert runner.calls == []


def test_backend_from_config_raises_for_partial_backend(tmp_path: Path) -> None:
    from hop.backends import UnknownBackendError

    partial = BackendConfig(name="lima", shell="lima")  # editor missing

    with pytest.raises(UnknownBackendError, match="missing shell or editor"):
        backend_from_config(partial)


def test_default_runner_invokes_subprocess_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke-test the default runner by running a real `true` (no runner override)."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    backend = backend_from_config(make_backend(prepare="true"))

    backend.prepare(build_session(tmp_path))


def test_editor_remote_address_is_identical_for_both_bases(tmp_path: Path) -> None:
    session = build_session(tmp_path)

    host_addr = HostBackend().editor_remote_address(session)
    backend_addr = backend_from_config(make_backend()).editor_remote_address(session)

    assert host_addr == backend_addr
