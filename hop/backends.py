from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir
from typing import Callable, Protocol, Sequence

from hop.config import (
    HOST_BACKEND_NAME,
    PLACEHOLDER_LISTEN_ADDR,
    PLACEHOLDER_PROJECT_ROOT,
    BackendConfig,
)
from hop.errors import HopError
from hop.session import ProjectSession

NVIM_COMMAND = "nvim"


class SessionBackendError(HopError):
    """Raised when a session backend lifecycle action fails."""


class SessionBackend(Protocol):
    def prepare(self, session: ProjectSession) -> None: ...

    def shell_args(self, session: ProjectSession) -> Sequence[str]: ...

    def editor_args(self, session: ProjectSession, listen_addr: Path) -> Sequence[str]: ...

    def editor_remote_address(self, session: ProjectSession) -> Path: ...

    def translate_terminal_cwd(self, session: ProjectSession, cwd: Path) -> Path: ...

    def translate_host_path(self, session: ProjectSession, host_path: Path) -> Path: ...

    def teardown(self, session: ProjectSession) -> None: ...


@dataclass(frozen=True, slots=True)
class HostBackend:
    def prepare(self, session: ProjectSession) -> None:
        return None

    def shell_args(self, session: ProjectSession) -> Sequence[str]:
        return ()

    def editor_args(self, session: ProjectSession, listen_addr: Path) -> Sequence[str]:
        return (NVIM_COMMAND, "--listen", str(listen_addr))

    def editor_remote_address(self, session: ProjectSession) -> Path:
        return _editor_remote_address(session)

    def translate_terminal_cwd(self, session: ProjectSession, cwd: Path) -> Path:
        return cwd

    def translate_host_path(self, session: ProjectSession, host_path: Path) -> Path:
        return host_path

    def teardown(self, session: ProjectSession) -> None:
        return None


CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


def _default_runner(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


@dataclass(frozen=True, slots=True)
class CommandBackend:
    """A SessionBackend whose lifecycle is described by command lists in the global config.

    ``workspace_path`` is captured at session creation by running the backend's
    ``workspace`` command and is used to translate terminal cwds back to host
    paths in the open_selection kitten dispatch. When ``workspace_path`` is
    ``None`` (no ``workspace`` command configured), translation is identity.
    """

    name: str
    shell: tuple[str, ...]
    editor: tuple[str, ...]
    prepare_command: tuple[str, ...] | None = None
    teardown_command: tuple[str, ...] | None = None
    workspace_command: tuple[str, ...] | None = None
    workspace_path: str | None = None
    runner: CommandRunner = field(default=_default_runner)

    def prepare(self, session: ProjectSession) -> None:
        if self.prepare_command is None:
            return
        result = self.runner(self.prepare_command, session.project_root)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout).strip()
            msg = f"backend {self.name!r} prepare failed for {session.session_name!r}: {stderr}"
            raise SessionBackendError(msg)

    def shell_args(self, session: ProjectSession) -> Sequence[str]:
        return _substitute(self.shell, session=session, listen_addr=None)

    def editor_args(self, session: ProjectSession, listen_addr: Path) -> Sequence[str]:
        return _substitute(self.editor, session=session, listen_addr=listen_addr)

    def editor_remote_address(self, session: ProjectSession) -> Path:
        return _editor_remote_address(session)

    def translate_terminal_cwd(self, session: ProjectSession, cwd: Path) -> Path:
        if self.workspace_path is None:
            return cwd
        prefix = Path(self.workspace_path)
        try:
            relative = cwd.relative_to(prefix)
        except ValueError:
            return cwd
        return session.project_root / relative

    def translate_host_path(self, session: ProjectSession, host_path: Path) -> Path:
        # Inverse of translate_terminal_cwd: rewrite a host path under the
        # project root to its in-backend location so commands like nvim's
        # `:drop <path>` reach the right file from inside the container.
        if self.workspace_path is None:
            return host_path
        try:
            relative = host_path.relative_to(session.project_root)
        except ValueError:
            return host_path
        return Path(self.workspace_path) / relative

    def teardown(self, session: ProjectSession) -> None:
        if self.teardown_command is None:
            return
        result = self.runner(self.teardown_command, session.project_root)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout).strip()
            msg = f"backend {self.name!r} teardown failed for {session.session_name!r}: {stderr}"
            raise SessionBackendError(msg)

    def with_workspace_path(self, workspace_path: str | None) -> "CommandBackend":
        return CommandBackend(
            name=self.name,
            shell=self.shell,
            editor=self.editor,
            prepare_command=self.prepare_command,
            teardown_command=self.teardown_command,
            workspace_command=self.workspace_command,
            workspace_path=workspace_path,
            runner=self.runner,
        )

    def discover_workspace(self, session: ProjectSession) -> str | None:
        """Run the ``workspace`` command and return its stripped stdout.

        Returns ``None`` when no workspace command is configured. Raises
        ``SessionBackendError`` if the command fails — discovery sits on the
        critical path for cwd translation, so a bad command should not be
        silently absorbed into ``workspace_path = None``.
        """

        if self.workspace_command is None:
            return None
        result = self.runner(self.workspace_command, session.project_root)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout).strip()
            msg = f"backend {self.name!r} workspace discovery failed for {session.session_name!r}: {stderr}"
            raise SessionBackendError(msg)
        return result.stdout.strip() or None


class UnknownBackendError(HopError):
    """Raised when a pinned backend name doesn't match any configured backend."""


def select_backend(
    session: ProjectSession,
    backends: Sequence[BackendConfig],
    *,
    pinned_name: str | None = None,
    runner: CommandRunner = _default_runner,
) -> BackendConfig | None:
    """Choose which backend (if any) applies to ``session``.

    Returns ``None`` to mean "use HostBackend". Only runnable backends
    (those with both ``shell`` and ``editor`` set) are considered.

    Resolution rules:

    - ``pinned_name == "host"`` short-circuits to host.
    - ``pinned_name`` (any other value) picks the runnable backend with that
      name. Raises ``UnknownBackendError`` when no runnable backend matches —
      either because the name is missing entirely, or because the merged
      definition lacks ``shell`` or ``editor``.
    - Otherwise auto-detect walks ``backends`` in declaration order and runs
      each runnable backend's ``default`` command (skipping ones without a
      ``default``); the first that exits 0 wins. If none succeed, returns
      ``None`` so the host backend is used as the implicit fallback.
    """

    if pinned_name == HOST_BACKEND_NAME:
        return None

    runnable = [b for b in backends if b.is_runnable]

    if pinned_name is not None:
        for candidate in runnable:
            if candidate.name == pinned_name:
                return candidate
        msg = f"unknown backend {pinned_name!r}"
        raise UnknownBackendError(msg)

    for candidate in runnable:
        if candidate.default is None:
            continue
        result = runner(candidate.default, session.project_root)
        if result.returncode == 0:
            return candidate
    return None


def backend_from_config(
    config: BackendConfig,
    *,
    workspace_path: str | None = None,
    runner: CommandRunner = _default_runner,
) -> CommandBackend:
    if config.shell is None or config.editor is None:
        msg = (
            f"backend {config.name!r} is missing shell or editor; "
            f"only runnable backends can be instantiated"
        )
        raise UnknownBackendError(msg)
    return CommandBackend(
        name=config.name,
        shell=config.shell,
        editor=config.editor,
        prepare_command=config.prepare,
        teardown_command=config.teardown,
        workspace_command=config.workspace,
        workspace_path=workspace_path,
        runner=runner,
    )


def _substitute(
    template: tuple[str, ...],
    *,
    session: ProjectSession,
    listen_addr: Path | None,
) -> tuple[str, ...]:
    replacements: dict[str, str] = {
        PLACEHOLDER_PROJECT_ROOT: str(session.project_root),
    }
    if listen_addr is not None:
        replacements[PLACEHOLDER_LISTEN_ADDR] = str(listen_addr)

    return tuple(_apply(part, replacements) for part in template)


def _apply(part: str, replacements: dict[str, str]) -> str:
    for placeholder, value in replacements.items():
        part = part.replace(placeholder, value)
    return part


def _editor_remote_address(session: ProjectSession) -> Path:
    runtime_root = os.environ.get("XDG_RUNTIME_DIR") or gettempdir()
    runtime_dir = Path(runtime_root).expanduser().resolve() / "hop"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    root_hash = hashlib.sha256(str(session.project_root).encode()).hexdigest()[:16]
    return runtime_dir / f"hop-{root_hash}.sock"
