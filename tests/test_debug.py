from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hop import debug
from hop.backends import backend_from_config
from hop.config import BackendConfig
from hop.session import ProjectSession


@pytest.fixture(autouse=True)
def reset_debug() -> None:
    debug.configure(None)
    yield
    debug.configure(None)


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


def build_session(project_root: Path) -> ProjectSession:
    return ProjectSession(
        project_root=project_root,
        session_name=project_root.name,
        workspace_name=f"p:{project_root.name}",
    )


class StaticRunner:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, args, cwd):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def test_configure_disabled_when_setting_is_none() -> None:
    debug.configure(None)
    assert debug.is_enabled() is False
    assert debug.log_path() is None


def test_configure_disabled_when_setting_is_false() -> None:
    debug.configure(False)
    assert debug.is_enabled() is False


def test_configure_default_path_when_setting_is_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    debug.configure(True)

    assert debug.is_enabled() is True
    assert debug.log_path() == tmp_path / "hop" / "debug.log"


def test_configure_custom_path_when_setting_is_string(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "debug.log"

    debug.configure(str(target))

    assert debug.log_path() == target
    assert target.parent.is_dir()


def test_log_appends_timestamped_message(tmp_path: Path) -> None:
    target = tmp_path / "debug.log"
    debug.configure(str(target))

    debug.log("hello")
    debug.log("world")

    lines = target.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" hello")
    assert lines[1].endswith(" world")


def test_log_is_noop_when_disabled(tmp_path: Path) -> None:
    target = tmp_path / "debug.log"
    debug.configure(None)

    debug.log("hello")

    assert not target.exists()


def test_log_command_writes_argv_exit_stdout_stderr(tmp_path: Path) -> None:
    target = tmp_path / "debug.log"
    debug.configure(str(target))

    result = subprocess.CompletedProcess(
        args=["sh", "-c", "echo hi"],
        returncode=2,
        stdout="hi\n",
        stderr="oops\n",
    )
    debug.log_command(("sh", "-c", "echo hi"), tmp_path, result)

    contents = target.read_text()
    assert "command: sh -c 'echo hi'" in contents
    assert f"cwd: {tmp_path}" in contents
    assert "exit: 2" in contents
    assert "stdout: hi" in contents
    assert "stderr: oops" in contents


def test_log_command_omits_empty_stdout_and_stderr(tmp_path: Path) -> None:
    target = tmp_path / "debug.log"
    debug.configure(str(target))

    result = subprocess.CompletedProcess(args=["true"], returncode=0, stdout="", stderr="")
    debug.log_command(("true",), tmp_path, result)

    contents = target.read_text()
    assert "stdout:" not in contents
    assert "stderr:" not in contents
    assert "exit: 0" in contents


def test_log_command_omits_cwd_line_when_cwd_is_none(tmp_path: Path) -> None:
    target = tmp_path / "debug.log"
    debug.configure(str(target))

    result = subprocess.CompletedProcess(args=["true"], returncode=0, stdout="", stderr="")
    debug.log_command(("true",), None, result)

    contents = target.read_text()
    assert "cwd:" not in contents
    assert "exit: 0" in contents


def test_log_command_is_noop_when_disabled(tmp_path: Path) -> None:
    target = tmp_path / "debug.log"
    debug.configure(None)

    result = subprocess.CompletedProcess(args=["true"], returncode=0, stdout="", stderr="")
    debug.log_command(("true",), tmp_path, result)

    assert not target.exists()


def test_command_backend_prepare_writes_to_debug_log_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    target = tmp_path / "debug.log"
    debug.configure(str(target))
    runner = StaticRunner(returncode=0, stdout="ok\n")
    backend = backend_from_config(make_backend(), runner=runner)

    backend.prepare(build_session(tmp_path))

    contents = target.read_text()
    assert "compose up -d devcontainer" in contents
    assert "exit: 0" in contents
    assert "stdout: ok" in contents


def test_command_backend_prepare_logs_failure_before_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    target = tmp_path / "debug.log"
    debug.configure(str(target))
    runner = StaticRunner(returncode=1, stderr="boom")
    backend = backend_from_config(make_backend(), runner=runner)

    from hop.backends import SessionBackendError

    with pytest.raises(SessionBackendError):
        backend.prepare(build_session(tmp_path))

    contents = target.read_text()
    assert "exit: 1" in contents
    assert "stderr: boom" in contents
