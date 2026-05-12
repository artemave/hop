from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pytest

from hop.backends import (
    CommandBackend,
    SessionBackendError,
    UnknownBackendError,
    backend_from_config,
    select_backend,
)
from hop.config import BackendConfig
from hop.session import ProjectSession


def host_backend() -> CommandBackend:
    """Construct hop's built-in 'host' backend instance for tests."""
    return CommandBackend(name="host", interactive_prefix="", noninteractive_prefix="")


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
    calls: list[tuple[tuple[str, ...], Path, str | None]] = field(default_factory=lambda: [])

    def __call__(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(args), cwd, stdin))
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
        "interactive_prefix": "compose exec devcontainer",
        "noninteractive_prefix": "compose exec devcontainer",
    }
    defaults.update(kwargs)
    return BackendConfig(**defaults)  # type: ignore[arg-type]


# --- Built-in host backend (CommandBackend with empty prefixes) ---------


def test_host_backend_interactive_prefix_is_empty_string() -> None:
    assert host_backend().interactive_prefix == ""


def test_host_backend_wrap_empty_returns_empty_argv(tmp_path: Path) -> None:
    """Empty command + empty prefix means "let kitty pick the default shell" —
    the launch path passes empty args so kitty falls through to /etc/passwd."""
    assert host_backend().wrap("", build_session(tmp_path)) == ()


def test_host_backend_wrap_substitutes_command(tmp_path: Path) -> None:
    args = host_backend().wrap("cd {project_root} && pwd", build_session(tmp_path))
    assert args == ("sh", "-c", f"cd {shlex.quote(str(tmp_path))} && pwd")


def test_host_backend_inline_substitutes_without_sh_wrapping(tmp_path: Path) -> None:
    inlined = host_backend().inline("nvim", build_session(tmp_path))
    assert inlined == "nvim"


def test_host_backend_translate_localhost_url_is_identity(tmp_path: Path) -> None:
    session = build_session(tmp_path)
    assert host_backend().translate_localhost_url(session, "http://localhost:3000/foo") == "http://localhost:3000/foo"


def test_host_backend_paths_exist_runs_loop_unwrapped(tmp_path: Path) -> None:
    """With empty prefixes the synthesized command is just ``sh -c '<loop>'``
    which runs locally — same answer as Path.exists, just via subprocess."""
    existing = tmp_path / "exists.txt"
    existing.write_text("")
    missing = tmp_path / "missing.txt"

    result = host_backend().paths_exist(build_session(tmp_path), (existing, missing))

    assert result == {existing}


def test_host_backend_paths_exist_empty_input_returns_empty(tmp_path: Path) -> None:
    assert host_backend().paths_exist(build_session(tmp_path), ()) == set()


# --- CommandBackend wrap / inline ----------------------------------------


def test_command_backend_wrap_prepends_prefix_to_command(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend())

    args = backend.wrap("bin/dev", build_session(tmp_path))

    assert args == ("sh", "-c", "compose exec devcontainer bin/dev")


def test_command_backend_wrap_substitutes_project_root(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(interactive_prefix="ssh host cd {project_root} &&"))

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


def test_command_backend_wrap_with_empty_prefix_returns_substituted_command(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(interactive_prefix="", noninteractive_prefix=""))

    args = backend.wrap("nvim", build_session(tmp_path))

    assert args == ("sh", "-c", "nvim")


def test_command_backend_inline_returns_prefix_plus_command(tmp_path: Path) -> None:
    """inline() is used by the editor adapter to compose `<editor>; <shell>`
    inside a single sh -c — each piece must be wrapped by the prefix
    individually so the ; runs each one as its own backend exec."""
    backend = backend_from_config(make_backend())

    inlined = backend.inline("nvim", build_session(tmp_path))

    assert inlined == "compose exec devcontainer nvim"


def test_command_backend_inline_with_empty_prefix_is_identity_substituted(tmp_path: Path) -> None:
    backend = backend_from_config(make_backend(interactive_prefix="", noninteractive_prefix=""))

    assert backend.inline("nvim", build_session(tmp_path)) == "nvim"


def test_command_backend_substitutes_path_with_spaces_safely(tmp_path: Path) -> None:
    weird = tmp_path / "name with spaces"
    weird.mkdir()
    backend = backend_from_config(make_backend(interactive_prefix="", noninteractive_prefix=""))

    args = backend.wrap("cd {project_root} && pwd", build_session(weird))

    completed = subprocess.run(args, capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == str(weird)


# --- prepare / teardown ---------------------------------------------------


def test_command_backend_prepare_runs_prepare_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(), runner=runner)

    backend.prepare(build_session(tmp_path))

    lock = tmp_path / "hop" / f"backend-{tmp_path.name}.lock"
    assert runner.calls == [(("flock", "-o", str(lock), "sh", "-c", "compose up -d devcontainer"), tmp_path, None)]


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
    assert runner.calls == [(("flock", "-o", str(lock), "sh", "-c", "compose down"), tmp_path, None)]


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


# --- paths_exist ----------------------------------------------------------


def test_command_backend_paths_exist_runs_loop_with_noninteractive_prefix(tmp_path: Path) -> None:
    """When `noninteractive_prefix` is set, the synthesized command
    starts with it (not `interactive_prefix`). The recorded stdin is the newline-
    joined paths; stdout names which exist."""
    runner = RecordingRunner(stdout="/abs/exists.rb\n")
    backend = backend_from_config(
        make_backend(noninteractive_prefix="compose exec -T devcontainer"),
        runner=runner,
    )

    existing = backend.paths_exist(
        build_session(tmp_path),
        (Path("/abs/exists.rb"), Path("/abs/missing.rb")),
    )

    assert existing == {Path("/abs/exists.rb")}
    assert len(runner.calls) == 1
    argv, _, stdin = runner.calls[0]
    assert argv[:2] == ("sh", "-c")
    composed = argv[2]
    assert composed.startswith("compose exec -T devcontainer sh -c ")
    assert "while IFS= read -r p" in composed
    assert stdin == "/abs/exists.rb\n/abs/missing.rb\n"


def test_command_backend_paths_exist_with_empty_prefix_runs_loop_unwrapped(tmp_path: Path) -> None:
    """With empty noninteractive_prefix (the built-in host case),
    the synthesized command is just ``sh -c '<loop>'`` — runs locally."""
    runner = RecordingRunner(stdout="/abs/exists.rb\n")
    backend = backend_from_config(
        make_backend(interactive_prefix="", noninteractive_prefix=""),
        runner=runner,
    )

    backend.paths_exist(build_session(tmp_path), (Path("/abs/exists.rb"),))

    argv = runner.calls[0][0]
    assert argv[2].startswith("sh -c ")
    assert "compose" not in argv[2]


def test_backend_from_config_requires_both_prefixes() -> None:
    """A partial backend declaration (missing one of the prefixes) is
    rejected at session entry with an actionable error."""
    from hop.errors import HopError

    with pytest.raises(HopError, match="interactive_prefix"):
        backend_from_config(make_backend(interactive_prefix=None))

    with pytest.raises(HopError, match="noninteractive_prefix"):
        backend_from_config(make_backend(noninteractive_prefix=None))


def test_command_backend_paths_exist_empty_input_skips_subprocess(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = backend_from_config(make_backend(), runner=runner)

    assert backend.paths_exist(build_session(tmp_path), ()) == set()
    assert runner.calls == []


def test_command_backend_paths_exist_raises_on_failure(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=1, stderr="container is gone")
    backend = backend_from_config(
        make_backend(noninteractive_prefix="compose exec -T devcontainer"),
        runner=runner,
    )

    with pytest.raises(SessionBackendError, match="paths_exist failed"):
        backend.paths_exist(build_session(tmp_path), (Path("/abs/foo"),))


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
    assert runner.calls == [(("sh", "-c", "compose port devcontainer 3000"), tmp_path, None)]


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


# --- backend_from_config wiring -------------------------------------------


def test_backend_from_config_carries_both_prefixes() -> None:
    backend = backend_from_config(
        make_backend(
            interactive_prefix="compose exec devcontainer",
            noninteractive_prefix="compose exec -T devcontainer",
        ),
    )

    assert isinstance(backend, CommandBackend)
    assert backend.interactive_prefix == "compose exec devcontainer"
    assert backend.noninteractive_prefix == "compose exec -T devcontainer"


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
    assert runner.calls == [(("sh", "-c", "a-activate"), tmp_path, None)]


def test_select_backend_walks_until_activate_succeeds(tmp_path: Path) -> None:
    @dataclass
    class ScriptedRunner:
        scripts: list[int]
        calls: list[tuple[tuple[str, ...], Path]] = field(default_factory=lambda: [])

        def __call__(
            self,
            args: Sequence[str],
            cwd: Path,
            *,
            stdin: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            del stdin
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
    assert runner.calls == [(("sh", "-c", "b-activate"), tmp_path, None)]


def test_select_backend_raises_when_no_activate_succeeds_and_no_host_fallback(tmp_path: Path) -> None:
    """In production the merged backend list always includes hop's built-in
    ``host`` entry with ``activate = "true"``. Bypassing the merge means
    auto-detect can fail to match anything — surface that as an error."""
    runner = RecordingRunner(returncode=1)
    backends = (
        make_backend(name="a", activate="a-activate"),
        make_backend(name="b", activate="b-activate"),
    )

    with pytest.raises(UnknownBackendError):
        select_backend(build_session(tmp_path), backends, runner=runner)


def test_select_backend_picks_host_when_listed(tmp_path: Path) -> None:
    """The merged list ends with the built-in ``host`` (activate='true').
    Verify select_backend honors that entry."""
    runner = RecordingRunner()
    backends = (
        make_backend(name="a", activate=None),
        BackendConfig(
            name="host",
            activate="true",
            interactive_prefix="",
            noninteractive_prefix="",
        ),
    )

    chosen = select_backend(build_session(tmp_path), backends, runner=runner)

    assert chosen is not None
    assert chosen.name == "host"


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


def test_default_runner_pipes_stdin_when_provided(tmp_path: Path) -> None:
    """End-to-end: paths_exist uses the default runner to pipe stdin into
    a real subprocess. The shell loop echoes inputs whose paths exist."""
    from hop.backends import default_runner

    existing = tmp_path / "exists.txt"
    existing.write_text("")
    missing = tmp_path / "missing.txt"
    payload = f"{existing}\n{missing}\n"

    result = default_runner(
        ["sh", "-c", 'while IFS= read -r p; do test -e "$p" && printf "%s\\n" "$p"; :; done'],
        tmp_path,
        stdin=payload,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [str(existing)]
