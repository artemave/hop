from __future__ import annotations

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
        "default": ("test", "-f", "docker-compose.dev.yml"),
        "shell": ("compose", "exec", "devcontainer", "/usr/bin/zsh"),
        "editor": ("compose", "exec", "devcontainer", "nvim", "--listen", "{listen_addr}"),
        "prepare": ("compose", "up", "-d", "devcontainer"),
        "teardown": ("compose", "down"),
        "workspace": ("compose", "exec", "devcontainer", "pwd"),
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
    backend = backend_from_config(
        make_backend(shell=("ssh", "host", "cd", "{project_root}", "&&", "exec", "zsh"))
    )

    args = backend.shell_args(build_session(tmp_path))

    assert args == ("ssh", "host", "cd", str(tmp_path), "&&", "exec", "zsh")


def test_command_backend_editor_substitutes_listen_addr(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend())
    addr = tmp_path / "nvim.sock"

    args = backend.editor_args(build_session(tmp_path), addr)

    assert args == (
        "compose",
        "exec",
        "devcontainer",
        "nvim",
        "--listen",
        str(addr),
    )


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


def test_command_backend_prepare_runs_prepare_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(), runner=runner)

    backend.prepare(build_session(tmp_path))

    assert runner.calls == [(("compose", "up", "-d", "devcontainer"), tmp_path)]


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


def test_command_backend_teardown_runs_teardown_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(), runner=runner)

    backend.teardown(build_session(tmp_path))

    assert runner.calls == [(("compose", "down"), tmp_path)]


def test_command_backend_teardown_raises_on_failure(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=2, stderr="nope")
    backend = backend_from_config(make_backend(), runner=runner)

    with pytest.raises(SessionBackendError, match="teardown failed"):
        backend.teardown(build_session(tmp_path))


def test_command_backend_discover_workspace_returns_stdout(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="/workspace\n")
    backend = backend_from_config(make_backend(), runner=runner)

    workspace = backend.discover_workspace(build_session(tmp_path))

    assert workspace == "/workspace"
    assert runner.calls == [(("compose", "exec", "devcontainer", "pwd"), tmp_path)]


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


def test_command_backend_with_workspace_path_returns_new_instance(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend())

    bound = backend.with_workspace_path("/workspace")

    assert bound.workspace_path == "/workspace"
    assert backend.workspace_path is None  # original is unchanged


def test_select_backend_returns_none_when_no_backends() -> None:
    project_root = Path("/tmp/demo")

    assert select_backend(build_session(project_root), ()) is None


def test_select_backend_returns_first_default_command_to_succeed(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backends = (
        make_backend(name="a", default=("a-default",)),
        make_backend(name="b", default=("b-default",)),
    )

    # Configure runner so first probe succeeds.
    chosen = select_backend(build_session(tmp_path), backends, runner=runner)

    assert chosen is not None
    assert chosen.name == "a"
    assert runner.calls == [(("a-default",), tmp_path)]


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
        make_backend(name="a", default=("a-default",)),
        make_backend(name="b", default=("b-default",)),
    )

    chosen = select_backend(build_session(tmp_path), backends, runner=runner)

    assert chosen is not None
    assert chosen.name == "b"


def test_select_backend_skips_backends_without_default(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backends = (
        make_backend(name="a", default=None),
        make_backend(name="b", default=("b-default",)),
    )

    chosen = select_backend(build_session(tmp_path), backends, runner=runner)

    assert chosen is not None
    assert chosen.name == "b"
    assert runner.calls == [(("b-default",), tmp_path)]


def test_select_backend_returns_none_when_no_default_succeeds(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=1)
    backends = (
        make_backend(name="a", default=("a-default",)),
        make_backend(name="b", default=("b-default",)),
    )

    assert select_backend(build_session(tmp_path), backends, runner=runner) is None


def test_select_backend_pinned_name_skips_default_probes(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backends = (
        make_backend(name="a", default=("a-default",)),
        make_backend(name="b", default=("b-default",)),
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


def test_editor_remote_address_is_identical_for_both_bases(tmp_path: Path) -> None:
    session = build_session(tmp_path)

    host_addr = HostBackend().editor_remote_address(session)
    backend_addr = backend_from_config(make_backend()).editor_remote_address(session)

    assert host_addr == backend_addr
