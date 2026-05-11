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
    UnknownBackendError,
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
        "activate": "test -f docker-compose.dev.yml",
        "prepare": "compose up -d devcontainer",
        "teardown": "compose down",
        "workspace": "compose exec devcontainer pwd",
        "command_prefix": "compose exec devcontainer",
    }
    defaults.update(kwargs)
    return BackendConfig(**defaults)  # type: ignore[arg-type]


# --- HostBackend ---------------------------------------------------------


def test_host_backend_command_prefix_is_none() -> None:
    assert HostBackend().command_prefix is None


def test_host_backend_wrap_empty_returns_empty_argv(tmp_path: Path) -> None:
    """Empty command on host means "let kitty pick the default shell" — the
    launch path passes empty args so kitty falls through to /etc/passwd."""
    assert HostBackend().wrap("", build_session(tmp_path)) == ()


def test_host_backend_wrap_substitutes_command(tmp_path: Path) -> None:
    args = HostBackend().wrap("cd {project_root} && pwd", build_session(tmp_path))
    assert args == ("sh", "-c", f"cd {shlex.quote(str(tmp_path))} && pwd")


def test_host_backend_inline_substitutes_without_sh_wrapping(tmp_path: Path) -> None:
    inlined = HostBackend().inline("nvim", build_session(tmp_path))
    assert inlined == "nvim"


def test_host_backend_translate_terminal_cwd_is_identity(tmp_path: Path) -> None:
    session = build_session(tmp_path)
    cwd = tmp_path / "subdir"
    assert HostBackend().translate_terminal_cwd(session, cwd) == cwd


def test_host_backend_translate_host_path_is_identity(tmp_path: Path) -> None:
    session = build_session(tmp_path)
    host_path = tmp_path / "lib" / "foo.py"
    assert HostBackend().translate_host_path(session, host_path) == host_path


def test_host_backend_translate_localhost_url_is_identity(tmp_path: Path) -> None:
    session = build_session(tmp_path)
    assert HostBackend().translate_localhost_url(session, "http://localhost:3000/foo") == "http://localhost:3000/foo"


# --- CommandBackend wrap / inline ----------------------------------------


def test_command_backend_wrap_prepends_prefix_to_command(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend())

    args = backend.wrap("bin/dev", build_session(tmp_path))

    assert args == ("sh", "-c", "compose exec devcontainer bin/dev")


def test_command_backend_wrap_substitutes_project_root(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(command_prefix="ssh host cd {project_root} &&"))

    args = backend.wrap("exec zsh", build_session(tmp_path))

    assert args == (
        "sh",
        "-c",
        f"ssh host cd {shlex.quote(str(tmp_path))} && exec zsh",
    )


def test_command_backend_wrap_empty_falls_back_to_shell_via_prefix(tmp_path: Path) -> None:
    """Empty command on a prefix backend can't produce empty kitty args
    (kitty would launch its host default, escaping the backend). Wrap
    `${SHELL:-sh}` so the exec lands inside the backend with whatever
    shell binary exists there."""
    backend = backend_from_config(make_backend())

    args = backend.wrap("", build_session(tmp_path))

    assert args == ("sh", "-c", "compose exec devcontainer ${SHELL:-sh}")


def test_command_backend_wrap_without_prefix_returns_substituted_command(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(command_prefix=None))

    args = backend.wrap("nvim", build_session(tmp_path))

    assert args == ("sh", "-c", "nvim")


def test_command_backend_inline_returns_prefix_plus_command(tmp_path: Path) -> None:
    """inline() is used by the editor adapter to compose `<editor>; <shell>`
    inside a single sh -c — each piece must be wrapped by the prefix
    individually so the ; runs each one as its own backend exec."""
    backend = backend_from_config(make_backend())

    inlined = backend.inline("nvim", build_session(tmp_path))

    assert inlined == "compose exec devcontainer nvim"


def test_command_backend_inline_without_prefix_is_identity_substituted(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(command_prefix=None))

    assert backend.inline("nvim", build_session(tmp_path)) == "nvim"


def test_command_backend_substitutes_path_with_spaces_safely(tmp_path: Path) -> None:
    weird = tmp_path / "name with spaces"
    weird.mkdir()
    backend = backend_from_config(make_backend(command_prefix=None))

    args = backend.wrap("cd {project_root} && pwd", build_session(weird))

    completed = subprocess.run(args, capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == str(weird)


# --- translate helpers ----------------------------------------------------


def test_command_backend_translate_terminal_cwd_uses_workspace_path(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(), workspace_path="/workspace")
    session = build_session(tmp_path)

    container_path = Path("/workspace/lib/foo.py")

    assert backend.translate_terminal_cwd(session, container_path) == tmp_path / "lib" / "foo.py"


def test_command_backend_translate_terminal_cwd_identity_without_workspace(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(), workspace_path=None)
    other = Path("/elsewhere")

    assert backend.translate_terminal_cwd(build_session(tmp_path), other) == other


def test_command_backend_translate_terminal_cwd_passes_through_unrelated_paths(tmp_path: Path) -> None:
    """A cwd that isn't under workspace_path is identity-translated; the
    relative_to call raises ValueError and the backend returns it as-is."""
    backend = backend_from_config(make_backend(), workspace_path="/workspace")
    other = Path("/elsewhere")

    assert backend.translate_terminal_cwd(build_session(tmp_path), other) == other


def test_command_backend_translate_host_path_identity_without_workspace(tmp_path: Path) -> None:
    """No workspace_path → no rewrite, even for paths under the project root."""
    backend = backend_from_config(make_backend(), workspace_path=None)
    host_path = tmp_path / "lib" / "foo.py"

    assert backend.translate_host_path(build_session(tmp_path), host_path) == host_path


def test_command_backend_translate_host_path_rewrites_under_project_root(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(), workspace_path="/workspace")
    session = build_session(tmp_path)

    host_path = tmp_path / "lib" / "foo.py"
    assert backend.translate_host_path(session, host_path) == Path("/workspace/lib/foo.py")


def test_command_backend_translate_host_path_identity_outside_project(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(), workspace_path="/workspace")
    other = Path("/etc/hosts")
    assert backend.translate_host_path(build_session(tmp_path), other) == other


# --- prepare / teardown ---------------------------------------------------


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


def test_command_backend_teardown_is_noop_without_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(teardown=None), runner=runner)

    backend.teardown(build_session(tmp_path))

    assert runner.calls == []


# --- workspace discovery / with_workspace_path ---------------------------


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


def test_command_backend_with_workspace_path_preserves_prefix_and_translate(tmp_path: Path) -> None:
    backend = backend_from_config(
        make_backend(port_translate="echo 1234", host_translate="echo myhost"),
    )

    bound = backend.with_workspace_path("/workspace")

    assert bound.workspace_path == "/workspace"
    assert bound.command_prefix == "compose exec devcontainer"
    assert bound.port_translate_command == "echo 1234"
    assert bound.host_translate_command == "echo myhost"


# --- localhost URL translation -------------------------------------------


def test_command_backend_translate_localhost_url_is_identity_when_no_commands(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = backend_from_config(
        make_backend(port_translate=None, host_translate=None),
        runner=runner,
    )

    assert (
        backend.translate_localhost_url(build_session(tmp_path), "http://localhost:3000/foo")
        == "http://localhost:3000/foo"
    )
    assert runner.calls == []


def test_command_backend_translate_localhost_url_raises_when_port_translate_returns_non_numeric(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(stdout="not-a-port\n")
    backend = backend_from_config(make_backend(port_translate="p {port}"), runner=runner)

    with pytest.raises(SessionBackendError, match="non-numeric"):
        backend.translate_localhost_url(build_session(tmp_path), "http://localhost:3000/")


def test_command_backend_translate_localhost_url_preserves_userinfo_when_port_dropped(
    tmp_path: Path,
) -> None:
    """A URL without an explicit port plus host_translate (no port_translate)
    rebuilds the netloc with userinfo + host but no port. Exercises the
    `port is None` branch of the userinfo-preserving rebuilder."""
    runner = RecordingRunner(stdout="myserver\n")
    backend = backend_from_config(make_backend(host_translate="echo myserver"), runner=runner)

    translated = backend.translate_localhost_url(
        build_session(tmp_path),
        "http://user:pw@localhost/foo",
    )

    assert translated == "http://user:pw@myserver/foo"


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


def test_command_backend_translate_localhost_url_skips_non_localhost(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="9999\n")
    backend = backend_from_config(make_backend(port_translate="echo 9999"), runner=runner)

    assert (
        backend.translate_localhost_url(build_session(tmp_path), "https://example.com:3000/foo")
        == "https://example.com:3000/foo"
    )
    assert runner.calls == []


def test_command_backend_translate_localhost_url_treats_127_0_0_1(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="35231")
    backend = backend_from_config(make_backend(port_translate="compose port {port}"), runner=runner)

    assert (
        backend.translate_localhost_url(build_session(tmp_path), "http://127.0.0.1:3000/") == "http://127.0.0.1:35231/"
    )


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


def test_command_backend_translate_localhost_url_preserves_userinfo(tmp_path: Path) -> None:
    runner = RecordingRunner(stdout="35231")
    backend = backend_from_config(make_backend(port_translate="p {port}"), runner=runner)

    translated = backend.translate_localhost_url(
        build_session(tmp_path),
        "http://user:pw@localhost:3000/foo",
    )

    assert translated == "http://user:pw@localhost:35231/foo"


# --- backend selection ----------------------------------------------------


def test_select_backend_returns_first_activate_to_succeed(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backends = (
        make_backend(name="a", activate="a-activate"),
        make_backend(name="b", activate="b-activate"),
    )

    chosen = select_backend(build_session(tmp_path), backends, runner=runner)

    assert chosen is not None
    assert chosen.name == "a"
    assert runner.calls == [(("sh", "-c", "a-activate"), tmp_path)]


def test_select_backend_walks_until_activate_succeeds(tmp_path: Path) -> None:
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
        make_backend(name="a", activate="a-activate"),
        make_backend(name="b", activate="b-activate"),
    )

    chosen = select_backend(build_session(tmp_path), backends, runner=runner)

    assert chosen is not None
    assert chosen.name == "b"


def test_select_backend_skips_backends_without_activate(tmp_path: Path) -> None:
    """A backend without an `activate` probe can't auto-detect; iterate past it."""
    runner = RecordingRunner()
    backends = (
        make_backend(name="a", activate=None),
        make_backend(name="b", activate="b-activate"),
    )

    chosen = select_backend(build_session(tmp_path), backends, runner=runner)

    assert chosen is not None
    assert chosen.name == "b"
    assert runner.calls == [(("sh", "-c", "b-activate"), tmp_path)]


def test_select_backend_returns_none_when_no_activate_succeeds(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=1)
    backends = (
        make_backend(name="a", activate="a-activate"),
        make_backend(name="b", activate="b-activate"),
    )

    assert select_backend(build_session(tmp_path), backends, runner=runner) is None


def test_select_backend_pinned_host_returns_none(tmp_path: Path) -> None:
    backends = (make_backend(),)

    assert select_backend(build_session(tmp_path), backends, pinned_name="host") is None


def test_select_backend_pinned_unknown_name_raises(tmp_path: Path) -> None:
    backends = (make_backend(),)

    with pytest.raises(UnknownBackendError, match="nonexistent"):
        select_backend(build_session(tmp_path), backends, pinned_name="nonexistent")


def test_default_runner_invokes_subprocess_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    backend = backend_from_config(make_backend(prepare="true"))

    backend.prepare(build_session(tmp_path))


def test_default_runner_inherits_stderr_when_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When stderr is a tty, hop streams backend command output live to the
    user's terminal. Verify that default_runner asks subprocess.run to
    inherit stderr (stderr=None) rather than capture it."""
    import sys

    from hop.backends import default_runner

    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr=None)

    monkeypatch.setattr(subprocess, "run", fake_run)

    default_runner(["true"], tmp_path)

    assert captured["stderr"] is None
    assert captured["stdout"] is subprocess.PIPE


def test_default_runner_captures_stderr_when_non_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off a tty (Vicinae, scripts, CI), capture stderr so it can be surfaced
    in error messages and the debug log."""
    import sys

    from hop.backends import default_runner

    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    default_runner(["true"], tmp_path)

    assert captured["stderr"] is subprocess.PIPE
    assert captured["stdout"] is subprocess.PIPE
