from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir
from typing import Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

from hop import debug
from hop.config import (
    PLACEHOLDER_PORT,
    PLACEHOLDER_PROJECT_ROOT,
    BackendConfig,
)
from hop.errors import HopError
from hop.session import ProjectSession

# Sentinel hostnames that, inside a non-host backend's network namespace, all
# refer to "this session's local interface" — i.e. the value the kitten dispatch
# may need to translate before handing the URL to the host's browser.
LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0"})

# Fallback shell snippet used when wrapping an empty command through a
# interactive_prefix. The outer sh expands ${SHELL:-sh} before the prefix exec
# runs, so the resulting binary is whatever path the user's host shell
# resolves to — which works as long as the same path exists in the backend's
# environment (the typical case for container-backed dev setups).
SHELL_FALLBACK = "${SHELL:-sh}"

# Inner shell loop hop wraps in `<noninteractive_prefix> sh -c '...'` to ask
# a backend which of a set of paths exists. Reads newline-separated paths
# from stdin and writes the existing ones, newline-separated, to stdout.
# IFS= preserves leading whitespace; -r disables backslash interpretation.
# Trailing `:` keeps the loop's exit code at 0 — a missing file in the last
# iteration would otherwise leak through as the shell's overall exit.
_PATH_EXISTS_LOOP = 'while IFS= read -r p; do test -e "$p" && printf "%s\\n" "$p"; :; done'


class SessionBackendError(HopError):
    """Raised when a session backend lifecycle action fails."""


class SessionBackend(Protocol):
    """Public Protocol implemented by ``CommandBackend``.

    Kept as a Protocol so tests can pass minimal fakes and adapter call
    sites stay structural about their dependencies; production code only
    has one implementer.
    """

    @property
    def interactive_prefix(self) -> str: ...

    def prepare(self, session: ProjectSession) -> None: ...

    def wrap(self, command: str, session: ProjectSession) -> Sequence[str]: ...

    def inline(self, command: str, session: ProjectSession) -> str: ...

    def translate_localhost_url(self, session: ProjectSession, url: str) -> str: ...

    def paths_exist(self, session: ProjectSession, paths: Sequence[Path]) -> set[Path]: ...

    def teardown(self, session: ProjectSession) -> None: ...


class CommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


def default_runner(
    args: Sequence[str],
    cwd: Path,
    *,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Default ``CommandRunner`` — runs ``args`` in ``cwd``.

    Stdout is always captured so callers that consume it (translate helpers,
    ``paths_exist``) keep working. Stderr is inherited from the parent when
    invoked interactively, so the user sees backend command output live during
    slow operations like ``docker compose up``; otherwise stderr is captured
    and surfaced through the debug log and error messages.

    ``stdin`` is forwarded to ``subprocess.run`` as the ``input`` kwarg when
    provided. The default ``None`` leaves stdin closed, matching prior
    behavior for callers that don't need to pipe.

    Exposed publicly so other modules (e.g. ``hop.app``) can pass it to
    helpers that take a ``CommandRunner`` argument when no override is
    configured. Tests inject their own runners instead.
    """

    return subprocess.run(
        list(args),
        cwd=str(cwd),
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=None if sys.stderr.isatty() else subprocess.PIPE,
        text=True,
        check=False,
    )


# Internal alias used as the default-argument value of fields/parameters that
# accept a CommandRunner. Kept under the original private name so existing
# call-sites don't need to change.
_default_runner = default_runner


@dataclass(frozen=True, slots=True)
class CommandBackend:
    """A session backend described entirely by shell command strings.

    Lifecycle commands (``prepare`` / ``teardown`` / translate helpers) and
    the two prefixes are shell snippets hop runs via ``sh -c`` after
    substituting placeholders. The values come straight from the config file:
    whatever you would type at a terminal, including pipes and ``$(...)``.
    Placeholder values are shell-quoted before insertion.

    ``interactive_prefix`` wraps every window's command launched in this backend's
    environment (e.g. ``podman-compose -f docker-compose.dev.yml exec
    devcontainer``). Hop joins it with the window's command via a single space
    at launch time. Per-role launch commands themselves live in top-level
    ``[layouts.<name>]`` and ``[windows.<role>]`` config sections, not on the
    backend.

    ``noninteractive_prefix`` is the prefix hop uses for non-interactive
    backend operations like the file-existence check that drives the
    open-selection kitten. Backends that allocate a TTY by default
    (podman-compose exec) must set this to the no-TTY variant
    (``... exec -T devcontainer``); backends that don't (ssh) can pass the
    same string as ``interactive_prefix``. Both prefixes may be the empty string
    for an "in-place" backend (e.g. hop's built-in ``host``), in which case
    commands run unwrapped against the host.
    """

    name: str
    interactive_prefix: str
    noninteractive_prefix: str
    prepare_command: str | None = None
    teardown_command: str | None = None
    port_translate_command: str | None = None
    host_translate_command: str | None = None
    runner: CommandRunner = field(default=_default_runner)

    def prepare(self, session: ProjectSession) -> None:
        if self.prepare_command is None:
            return
        argv = _flock_sh(self.prepare_command, session=session)
        result = self.runner(argv, session.project_root)
        debug.log_command(argv, session.project_root, result)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            msg = f"backend {self.name!r} prepare failed for {session.session_name!r}: {stderr}"
            raise SessionBackendError(msg)

    def wrap(self, command: str, session: ProjectSession) -> Sequence[str]:
        if not command and not self.interactive_prefix:
            # In-place (host-equivalent) backend with no shell override:
            # let kitty pick the user's login shell from /etc/passwd by
            # returning empty argv. Any other empty-command case has a
            # backend prefix to honor, so we still need to exec *something*
            # inside the backend; fall back to ${SHELL:-sh}.
            return ()
        if not command:
            return _sh_c(self.inline(SHELL_FALLBACK, session=session))
        return _sh_c(self.inline(command, session=session))

    def inline(self, command: str, session: ProjectSession) -> str:
        """Build the substituted, prefix-wrapped command string (no sh -c).

        Used when the caller composes multiple commands (e.g. the editor
        adapter's ``<editor>; <shell>`` post-exit drop) before a single
        outer ``sh -c`` wraps the script. Each piece must be wrapped by
        the prefix individually so the ``;`` separator runs each piece
        as its own backend exec, preserving today's two-call behavior.
        """

        substituted = _substitute(command, session=session)
        if not self.interactive_prefix:
            return substituted
        substituted_prefix = _substitute(self.interactive_prefix, session=session)
        return f"{substituted_prefix} {substituted}"

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
        argv = _sh_c(substituted)
        result = self.runner(argv, session.project_root)
        debug.log_command(argv, session.project_root, result)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
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
        argv = _flock_sh(self.teardown_command, session=session)
        result = self.runner(argv, session.project_root)
        debug.log_command(argv, session.project_root, result)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            msg = f"backend {self.name!r} teardown failed for {session.session_name!r}: {stderr}"
            raise SessionBackendError(msg)

    def paths_exist(self, session: ProjectSession, paths: Sequence[Path]) -> set[Path]:
        if not paths:
            return set()
        substituted_prefix = _substitute(self.noninteractive_prefix, session=session)
        composed = f"{substituted_prefix} sh -c {shlex.quote(_PATH_EXISTS_LOOP)}".lstrip()
        argv = _sh_c(composed)
        stdin = "\n".join(str(p) for p in paths) + "\n"
        result = self.runner(argv, session.project_root, stdin=stdin)
        debug.log_command(argv, session.project_root, result)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            msg = f"backend {self.name!r} paths_exist failed for {session.session_name!r}: {stderr}"
            raise SessionBackendError(msg)
        reported = {line for line in result.stdout.splitlines() if line}
        return {p for p in paths if str(p) in reported}


class UnknownBackendError(HopError):
    """Raised when a pinned backend name doesn't match any configured backend."""


def select_backend(
    session: ProjectSession,
    backends: Sequence[BackendConfig],
    *,
    pinned_name: str | None = None,
    runner: CommandRunner = _default_runner,
) -> BackendConfig:
    """Choose which backend applies to ``session``.

    Resolution rules:

    - ``pinned_name`` (when given) picks the backend with that name. Raises
      ``UnknownBackendError`` when no backend matches.
    - Otherwise auto-detect walks ``backends`` in declaration order and runs
      each backend's ``activate`` command (skipping ones without an
      ``activate``); the first that exits 0 wins.

    The merged config always carries hop's built-in ``host`` backend
    (``activate = "true"``, empty prefixes) at the lowest priority, so the
    auto-detect walk always ends with a guaranteed match.
    """

    if pinned_name is not None:
        for candidate in backends:
            if candidate.name == pinned_name:
                return candidate
        msg = f"unknown backend {pinned_name!r}"
        raise UnknownBackendError(msg)

    for candidate in backends:
        if candidate.activate is None:
            continue
        substituted = _substitute(candidate.activate, session=session)
        argv = _sh_c(substituted)
        result = runner(argv, session.project_root)
        debug.log_command(argv, session.project_root, result)
        if result.returncode == 0:
            return candidate
    msg = (
        f"no backend matched for {session.session_name!r}; "
        "the built-in 'host' fallback was overridden to decline auto-detect"
    )
    raise UnknownBackendError(msg)


def backend_from_config(
    config: BackendConfig,
    *,
    runner: CommandRunner = _default_runner,
) -> CommandBackend:
    if config.interactive_prefix is None:
        msg = f"backend {config.name!r} requires 'interactive_prefix'"
        raise HopError(msg)
    if config.noninteractive_prefix is None:
        msg = f"backend {config.name!r} requires 'noninteractive_prefix'"
        raise HopError(msg)
    return CommandBackend(
        name=config.name,
        interactive_prefix=config.interactive_prefix,
        noninteractive_prefix=config.noninteractive_prefix,
        prepare_command=config.prepare,
        teardown_command=config.teardown,
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
