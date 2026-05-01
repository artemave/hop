from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir
from typing import Callable, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

from hop.config import (
    HOST_BACKEND_NAME,
    PLACEHOLDER_PORT,
    PLACEHOLDER_PROJECT_ROOT,
    BackendConfig,
)
from hop.errors import HopError
from hop.session import ProjectSession

NVIM_COMMAND = "nvim"

# Sentinel hostnames that, inside a non-host backend's network namespace, all
# refer to "this session's local interface" — i.e. the value the kitten dispatch
# may need to translate before handing the URL to the host's browser.
LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0"})


class SessionBackendError(HopError):
    """Raised when a session backend lifecycle action fails."""


class SessionBackend(Protocol):
    def prepare(self, session: ProjectSession) -> None: ...

    def shell_args(self, session: ProjectSession) -> Sequence[str]: ...

    def editor_args(self, session: ProjectSession) -> Sequence[str]: ...

    def translate_terminal_cwd(self, session: ProjectSession, cwd: Path) -> Path: ...

    def translate_host_path(self, session: ProjectSession, host_path: Path) -> Path: ...

    def translate_localhost_url(self, session: ProjectSession, url: str) -> str: ...

    def teardown(self, session: ProjectSession) -> None: ...


@dataclass(frozen=True, slots=True)
class HostBackend:
    def prepare(self, session: ProjectSession) -> None:
        return None

    def shell_args(self, session: ProjectSession) -> Sequence[str]:
        return ()

    def editor_args(self, session: ProjectSession) -> Sequence[str]:
        # Wrap nvim in an sh that drops into the user's interactive shell
        # after exit. Without this, kitty closes the editor window the moment
        # nvim quits — the user can't run `nvim -S` to restore buffers, can't
        # peek at git state, can't do anything. The post-exit shell uses the
        # window's $SHELL (with /bin/sh as the fallback).
        return ("sh", "-c", f"{NVIM_COMMAND}; ${{SHELL:-sh}}")

    def translate_terminal_cwd(self, session: ProjectSession, cwd: Path) -> Path:
        return cwd

    def translate_host_path(self, session: ProjectSession, host_path: Path) -> Path:
        return host_path

    def translate_localhost_url(self, session: ProjectSession, url: str) -> str:
        return url

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
    """A SessionBackend whose lifecycle is described by shell command strings.

    Every command field is a shell snippet hop runs via ``sh -c``, after
    substituting placeholders. The values come straight from the config
    file: whatever you would type at a terminal, including pipes and
    ``$(...)``. Placeholder values are shell-quoted before insertion, so
    paths with spaces or special characters substitute safely.

    ``workspace_path`` is captured at session creation by running the
    backend's ``workspace`` command and is used to translate terminal cwds
    back to host paths in the open_selection kitten dispatch. When
    ``workspace_path`` is ``None`` (no ``workspace`` command configured),
    translation is identity.
    """

    name: str
    shell: str
    editor: str
    prepare_command: str | None = None
    teardown_command: str | None = None
    workspace_command: str | None = None
    workspace_path: str | None = None
    port_translate_command: str | None = None
    host_translate_command: str | None = None
    runner: CommandRunner = field(default=_default_runner)

    def prepare(self, session: ProjectSession) -> None:
        if self.prepare_command is None:
            return
        result = self.runner(_flock_sh(self.prepare_command, session=session), session.project_root)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout).strip()
            msg = f"backend {self.name!r} prepare failed for {session.session_name!r}: {stderr}"
            raise SessionBackendError(msg)

    def shell_args(self, session: ProjectSession) -> Sequence[str]:
        return _sh_c(_substitute(self.shell, session=session))

    def editor_args(self, session: ProjectSession) -> Sequence[str]:
        # Run the configured editor, then fall through to the configured
        # shell so the kitty window remains usable after the editor exits
        # (`nvim -S Session.vim` to restore buffers, etc.). Both fragments
        # are user-supplied shell snippets meant to be valid in `sh -c`, so
        # `;`-joining is safe; the post-exit shell ends up inside the same
        # backend (devcontainer, ssh host, ...) as the editor was running in.
        editor = _substitute(self.editor, session=session)
        shell = _substitute(self.shell, session=session)
        return _sh_c(f"{editor}; {shell}")

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

    def translate_localhost_url(self, session: ProjectSession, url: str) -> str:
        if self.host_translate_command is None and self.port_translate_command is None:
            return url

        parts = urlsplit(url)
        if (parts.hostname or "") not in LOCALHOST_HOSTS:
            return url

        new_host = parts.hostname or ""
        new_port: int | None = parts.port

        if self.host_translate_command is not None:
            new_host = self._run_translate(
                self.host_translate_command,
                session=session,
                port=parts.port,
                kind="host_translate",
            )

        if self.port_translate_command is not None:
            translated_port = self._run_translate(
                self.port_translate_command,
                session=session,
                port=parts.port,
                kind="port_translate",
            )
            try:
                new_port = int(translated_port)
            except ValueError as exc:
                msg = (
                    f"backend {self.name!r} port_translate returned non-numeric output "
                    f"{translated_port!r} for {session.session_name!r}"
                )
                raise SessionBackendError(msg) from exc

        netloc = _rebuild_netloc(parts, host=new_host, port=new_port)
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def _run_translate(
        self,
        command: str,
        *,
        session: ProjectSession,
        port: int | None,
        kind: str,
    ) -> str:
        substituted = _substitute_translate(command, session=session, port=port)
        result = self.runner(_sh_c(substituted), session.project_root)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout).strip()
            msg = f"backend {self.name!r} {kind} failed for {session.session_name!r}: {stderr}"
            raise SessionBackendError(msg)
        stdout = result.stdout.strip()
        if not stdout:
            msg = f"backend {self.name!r} {kind} returned empty output for {session.session_name!r}"
            raise SessionBackendError(msg)
        return stdout

    def teardown(self, session: ProjectSession) -> None:
        if self.teardown_command is None:
            return
        result = self.runner(_flock_sh(self.teardown_command, session=session), session.project_root)
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
            port_translate_command=self.port_translate_command,
            host_translate_command=self.host_translate_command,
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
        substituted = _substitute(self.workspace_command, session=session)
        result = self.runner(_sh_c(substituted), session.project_root)
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
        substituted = _substitute(candidate.default, session=session)
        result = runner(_sh_c(substituted), session.project_root)
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
        msg = f"backend {config.name!r} is missing shell or editor; only runnable backends can be instantiated"
        raise UnknownBackendError(msg)
    return CommandBackend(
        name=config.name,
        shell=config.shell,
        editor=config.editor,
        prepare_command=config.prepare,
        teardown_command=config.teardown,
        workspace_command=config.workspace,
        workspace_path=workspace_path,
        port_translate_command=config.port_translate,
        host_translate_command=config.host_translate,
        runner=runner,
    )


def _substitute(template: str, *, session: ProjectSession) -> str:
    replacements: dict[str, str] = {
        PLACEHOLDER_PROJECT_ROOT: shlex.quote(str(session.project_root)),
    }
    return _apply(template, replacements)


def _substitute_translate(
    template: str,
    *,
    session: ProjectSession,
    port: int | None,
) -> str:
    replacements: dict[str, str] = {
        PLACEHOLDER_PROJECT_ROOT: shlex.quote(str(session.project_root)),
        PLACEHOLDER_PORT: "" if port is None else shlex.quote(str(port)),
    }
    return _apply(template, replacements)


def _rebuild_netloc(parts: object, *, host: str, port: int | None) -> str:
    # parts: SplitResult; reconstruct netloc preserving userinfo so URLs like
    # http://user:pw@localhost:3000/ keep their auth segment after rewrite.
    userinfo = ""
    username = getattr(parts, "username", None)
    if username is not None:
        password = getattr(parts, "password", None)
        userinfo = username if password is None else f"{username}:{password}"
        userinfo += "@"
    if port is None:
        return f"{userinfo}{host}"
    return f"{userinfo}{host}:{port}"


def _apply(template: str, replacements: dict[str, str]) -> str:
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


def _sh_c(command: str) -> tuple[str, ...]:
    return ("sh", "-c", command)


def _backend_lock_path(session: ProjectSession) -> Path:
    runtime_root = os.environ.get("XDG_RUNTIME_DIR") or gettempdir()
    runtime_dir = Path(runtime_root).expanduser().resolve() / "hop"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir / f"backend-{session.session_name}.lock"


def _flock_sh(command: str, *, session: ProjectSession) -> tuple[str, ...]:
    # Serialize prepare and teardown for the same session: when `hop kill`
    # detaches its teardown via setsid -f (so it survives vicinae's SIGTERM),
    # a subsequent `hop` would otherwise race the still-running teardown and
    # leave podman-compose in an inconsistent state. flock(1) holds the lock
    # for the lifetime of the wrapped command, so even if our parent dies the
    # lock is held by the subprocess and the next caller blocks on it.
    substituted = _substitute(command, session=session)
    return ("flock", str(_backend_lock_path(session)), "sh", "-c", substituted)
