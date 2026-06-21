from __future__ import annotations

import base64
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
    PLACEHOLDER_HOST,
    PLACEHOLDER_PORT,
    PLACEHOLDER_SESSION_ROOT,
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

# Sentinel exit code used by ``CommandBackend.read_file`` to signal
# "path doesn't exist" from inside the shell script (so the caller can
# distinguish missing-file from other cat failures by exit code alone).
_READ_FILE_NOT_FOUND_EXIT = 42


class SessionBackendError(HopError):
    """Raised when a session backend lifecycle action fails."""


class BackendFileNotFoundError(SessionBackendError):
    """Raised by ``backend.read_file`` when the path doesn't exist.

    Distinct subclass so callers (e.g. the Rails-ref resolver in
    ``hop/targets.py``) can treat "no such file" as a normal miss while
    still propagating other backend failures (compose dead, ssh down).
    """


class SessionBackend(Protocol):
    """Public Protocol implemented by ``CommandBackend``.

    Kept as a Protocol so tests can pass minimal fakes and adapter call
    sites stay structural about their dependencies; production code only
    has one implementer.
    """

    @property
    def interactive_prefix(self) -> str: ...

    @property
    def prepare_command(self) -> tuple[str, ...] | None: ...

    @property
    def teardown_command(self) -> tuple[str, ...] | None: ...

    def prepare(self, session: ProjectSession) -> None: ...

    def wrap(self, command: str, session: ProjectSession) -> Sequence[str]: ...

    def compose(self, command: str) -> Sequence[str]: ...

    def inline(self, command: str, session: ProjectSession) -> str: ...

    def translate_localhost_url(self, session: ProjectSession, url: str) -> str: ...

    def paths_exist(self, session: ProjectSession, paths: Sequence[Path]) -> set[Path]: ...

    def read_file(self, session: ProjectSession, path: Path) -> str: ...

    def lifecycle_argv(self, step: str, session: ProjectSession) -> tuple[str, ...]: ...

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


class Transport(Protocol):
    """Turns a composed shell command string into the argv that runs it.

    The seam that makes a backend portable between host and remote: the same
    composed ``<prefix> <command>`` string is wrapped either to run locally
    (``sh -c``) or on a remote machine over ssh. Everything a backend does —
    window launches, ``prepare``/``teardown``, ``paths_exist``, ``read_file``,
    the translates, the ``activate`` probe — funnels through a transport, so a
    single swap relocates the whole backend.
    """

    def __call__(self, command: str) -> tuple[str, ...]: ...


def local_transport(command: str) -> tuple[str, ...]:
    """Run ``command`` on the host via ``sh -c`` — the default transport."""

    return ("sh", "-c", command)


def _substitution_host(host: str | None) -> str:
    """The ``{host}`` value — the bare hostname, or ``localhost`` when local.

    Strips any ``user@`` from the ssh target (``admin@devbox.local`` →
    ``devbox.local``): the transport uses the full target, but ``{host}`` stands
    for the externally-reachable hostname (``LOCAL_HOSTNAME``, host translation).
    """

    if host is None:
        return "localhost"
    return host.rsplit("@", 1)[-1]


def runner_cwd(host: str | None, session_root: Path) -> Path:
    """Local working directory for a backend subprocess.

    For a local backend (``host is None``) this is the project root — backend
    commands (e.g. ``podman-compose -f docker-compose.dev.yml …``) must run
    there. For a remote backend the transport carries its own ``cd <remote_cwd>``
    and the ssh client ignores the local cwd, so use the host home: ``session_root``
    is a path on the *remote* and handing it to ``subprocess.run(cwd=…)`` would
    fail because it doesn't exist locally.

    Keyed off the *backend's* host rather than the session's — a session rebuilt
    from a record (e.g. in the open-selection kitten) may not carry ``host``,
    but the backend always does.
    """

    if host is not None:
        return Path.home()
    return session_root


def default_ssh_options() -> tuple[str, ...]:
    """Shared ssh flags for the master + every transported command.

    ``ControlMaster=auto`` + ``ControlPath`` + ``ControlPersist`` mean the
    first ssh call establishes a multiplexed master and the rest reuse it; a
    later call after the master died silently re-establishes it, so a session
    survives a laptop-sleep / connection drop and redials lazily on the next
    command. ``ServerAliveInterval`` keeps the master warm; ``StreamLocalBindUnlink``
    lets a re-entered ``hop ssh`` rebind the reverse-forward socket cleanly.
    """

    runtime_root = os.environ.get("XDG_RUNTIME_DIR") or gettempdir()
    control_path = Path(runtime_root).expanduser() / "hop" / "cm-%r@%h:%p"
    return (
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_path}",
        "-o",
        "ControlPersist=600",
        "-o",
        "ServerAliveInterval=60",
        "-o",
        "StreamLocalBindUnlink=yes",
    )


@dataclass(frozen=True, slots=True)
class SshTransport:
    """Run a composed command on ``host`` over ssh, cd'd into ``remote_cwd``.

    The composed command is base64-encoded and dropped behind a *fixed* decode
    wrapper, so the only variable token ssh sees carries no shell metacharacters
    — ssh's argv-flattening can't corrupt it, and stdin stays free for data
    (``printf``/``base64`` don't read it), which is what lets the
    ``paths_exist``/``read_file`` "script over stdin" pattern keep working over
    ssh. The decoded command runs under a login shell (``$SHELL -lc``) so the
    remote user's normal PATH (e.g. Homebrew) resolves with no extra config.

    ``interactive`` adds ``-tt`` to allocate a remote tty for window-launch
    shells; non-interactive runner calls leave it off and pipe stdin instead.
    """

    host: str
    remote_cwd: str
    interactive: bool = False
    options: tuple[str, ...] = field(default_factory=default_ssh_options)

    def __call__(self, command: str) -> tuple[str, ...]:
        inner = f"cd {shlex.quote(self.remote_cwd)} && {command}"
        encoded = base64.b64encode(inner.encode()).decode("ascii")
        remote = f'exec "${{SHELL:-/bin/sh}}" -lc "$(printf %s {encoded} | base64 -d)"'
        tty = ("-tt",) if self.interactive else ()
        return ("ssh", *tty, *self.options, self.host, remote)


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
    prepare_command: tuple[str, ...] | None = None
    teardown_command: tuple[str, ...] | None = None
    port_translate_command: tuple[str, ...] | None = None
    host_translate_command: tuple[str, ...] | None = None
    # The backend's default working directory, captured by running
    # ``<noninteractive_prefix> pwd`` once at bootstrap. Used as a fallback
    # in ``hop.focused.paths_exist`` when the kitty window's ``cwd_of_child``
    # is unset (e.g. the in-shell shell doesn't emit OSC 7). ``None`` for
    # the host backend or when the probe failed.
    workspace_path: str | None = None
    runner: CommandRunner = field(default=_default_runner)
    # How composed commands become argv. ``transport`` wraps window-launch
    # commands (interactive, gets a remote tty over ssh); ``noninteractive_transport``
    # wraps runner-mediated calls (prepare/teardown, paths_exist, read_file,
    # translate, the activate probe) where stdin is piped and no tty is wanted.
    # Both default to ``local_transport`` (``sh -c``); a remote session swaps in
    # ``SshTransport``. ``host`` is the ssh target for ``{host}`` substitution
    # (``None`` ⇒ the local ``localhost``).
    transport: Transport = local_transport
    noninteractive_transport: Transport = local_transport
    host: str | None = None

    def prepare(self, session: ProjectSession) -> None:
        if self.prepare_command is None:
            return
        self._run_lifecycle_steps(self.prepare_command, session=session, kind="prepare")

    @property
    def _host(self) -> str:
        # The value substituted for ``{host}`` — the externally-reachable
        # *hostname*, not the ssh target: a user passes ``admin@devbox.local`` to
        # ``hop ssh`` but ``LOCAL_HOSTNAME={host}`` / ``host_translate = "echo
        # {host}"`` want ``devbox.local`` (the name a browser or the app uses).
        return _substitution_host(self.host)

    def wrap(self, command: str, session: ProjectSession) -> Sequence[str]:
        if not command and not self.interactive_prefix:
            # In-place (host-equivalent) backend with no shell override:
            # let kitty pick the user's login shell from /etc/passwd by
            # returning empty argv. Any other empty-command case has a
            # backend prefix to honor, so we still need to exec *something*
            # inside the backend; fall back to ${SHELL:-sh}.
            return ()
        if not command:
            return self.compose(self.inline(SHELL_FALLBACK, session=session))
        return self.compose(self.inline(command, session=session))

    def compose(self, command: str) -> Sequence[str]:
        """Wrap an already-composed inline command string into launch argv.

        The window-launch transport seam: locally ``("sh","-c",command)``,
        remotely ``("ssh", …, command-over-ssh)``. Callers that build a
        ``<a>; <b>`` script from two ``inline`` pieces (kitty/editor) route it
        through here so the single outer wrapper is transport-aware.
        """

        return self.transport(command)

    def inline(self, command: str, session: ProjectSession) -> str:
        """Build the substituted, prefix-wrapped command string (no transport).

        Used when the caller composes multiple commands (e.g. the editor
        adapter's ``<editor>; <shell>`` post-exit drop) before a single
        outer transport wraps the script. Each piece must be wrapped by
        the prefix individually so the ``;`` separator runs each piece
        as its own backend exec, preserving today's two-call behavior.
        """

        substituted = substitute(command, session=session, host=self._host)
        if not self.interactive_prefix:
            return substituted
        substituted_prefix = substitute(self.interactive_prefix, session=session, host=self._host)
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
        steps: tuple[str, ...],
        *,
        session: ProjectSession,
        port: int | None,
        kind: str,
    ) -> str:
        """Run translate steps sequentially, returning the last step's stdout.

        Earlier steps run for their side effects (probes, container lookups)
        and their stdout is dropped — only the final step's stdout determines
        the translated value. Any failing step aborts the sequence with the
        same step-labeled error shape as ``_run_lifecycle_steps``.
        """

        multi_step = len(steps) > 1
        last_stdout = ""
        for index, step in enumerate(steps, start=1):
            substituted = _substitute_translate(step, session=session, port=port, host=self._host)
            argv = self.noninteractive_transport(substituted)
            result = self.runner(argv, runner_cwd(self.host, session.session_root))
            debug.log_command(argv, session.session_root, result)
            if result.returncode != 0:
                stderr = (result.stderr or result.stdout or "").strip()
                label = f"{kind} step {index} ({step!r})" if multi_step else kind
                msg = f"backend {self.name!r} {label} failed for {session.session_name!r}: {stderr}"
                raise SessionBackendError(msg)
            last_stdout = result.stdout.strip()
        if not last_stdout:
            msg = f"backend {self.name!r} {kind} returned empty output for {session.session_name!r}"
            raise SessionBackendError(msg)
        return last_stdout

    def teardown(self, session: ProjectSession) -> None:
        if self.teardown_command is None:
            return
        self._run_lifecycle_steps(self.teardown_command, session=session, kind="teardown")

    def lifecycle_argv(self, step: str, session: ProjectSession) -> tuple[str, ...]:
        """The exact ``flock + transport`` argv for one prepare/teardown step.

        Public so the headless popup renders the *same* command hop would run
        inline — flock-serialized, transported (``sh -c`` locally, ``ssh`` for a
        remote session). Without this the popup would re-compose the step itself
        and run it on the host, ignoring the transport.
        """

        return _flock_sh(step, session=session, transport=self.noninteractive_transport, host=self._host)

    def _run_lifecycle_steps(
        self,
        steps: tuple[str, ...],
        *,
        session: ProjectSession,
        kind: str,
    ) -> None:
        """Run lifecycle steps sequentially under the per-session flock.

        Each step is its own ``flock -o ... sh -c '<step>'`` invocation so
        non-zero exits surface step-by-step (the popup's held-open shell shows
        only the failing step's output, not the whole sequence). The
        ``SessionBackendError`` message names the step by 1-indexed position
        when there's more than one — single-step sequences keep the legacy
        "<kind> failed" phrasing so existing error consumers don't churn.
        """

        multi_step = len(steps) > 1
        for index, step in enumerate(steps, start=1):
            argv = _flock_sh(step, session=session, transport=self.noninteractive_transport, host=self._host)
            result = self.runner(argv, runner_cwd(self.host, session.session_root))
            debug.log_command(argv, session.session_root, result)
            if result.returncode != 0:
                stderr = (result.stderr or result.stdout or "").strip()
                label = f"{kind} step {index} ({step!r})" if multi_step else kind
                msg = f"backend {self.name!r} {label} failed for {session.session_name!r}: {stderr}"
                raise SessionBackendError(msg)

    def probe_workspace_path(self, session: ProjectSession) -> str | None:
        """Return the backend's default working directory (``pwd``), or ``None``.

        Run once at bootstrap by ``SessionBackendRegistry.resolve_for_entry``
        and persisted in the session record. The result is used as the
        fallback ``base_cwd`` in ``hop.focused.paths_exist`` when the kitty
        window's OSC-7-driven ``cwd_of_child`` is unavailable.

        Best-effort: probe failures (empty prefix, non-zero exit, empty
        stdout) return ``None`` rather than raising — a missing fallback
        just degrades the kitten's relative-path matching, it doesn't
        break the session.
        """

        if not self.noninteractive_prefix:
            return None
        substituted_prefix = substitute(self.noninteractive_prefix, session=session, host=self._host)
        composed = f"{substituted_prefix} pwd"
        argv = self.noninteractive_transport(composed)
        result = self.runner(argv, runner_cwd(self.host, session.session_root))
        debug.log_command(argv, session.session_root, result)
        if result.returncode != 0:
            return None
        stdout = result.stdout.strip()
        return stdout or None

    def paths_exist(self, session: ProjectSession, paths: Sequence[Path]) -> set[Path]:
        if not paths:
            return set()
        substituted_prefix = substitute(self.noninteractive_prefix, session=session, host=self._host)
        # Pipe the existence-check script to a bare `sh` over stdin rather than
        # passing it as a `sh -c '<script>'` argument. A quoted argument doesn't
        # survive a prefix that flattens argv — `ssh host …` joins the remote
        # argv with spaces and strips one quote level, so the remote shell then
        # re-parses the bare script and errors. `sh` is a single token that
        # passes through any prefix unchanged; the script (each path inlined)
        # rides stdin untouched. Trailing `:` keeps the exit code at 0 when the
        # last path is missing.
        composed = f"{substituted_prefix} sh".lstrip()
        argv = self.noninteractive_transport(composed)
        script = "".join(f'test -e {shlex.quote(str(p))} && printf "%s\\n" {shlex.quote(str(p))}\n' for p in paths)
        result = self.runner(argv, runner_cwd(self.host, session.session_root), stdin=f"{script}:\n")
        debug.log_command(argv, session.session_root, result)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            msg = f"backend {self.name!r} paths_exist failed for {session.session_name!r}: {stderr}"
            raise SessionBackendError(msg)
        reported = {line for line in result.stdout.splitlines() if line}
        return {p for p in paths if str(p) in reported}

    def read_file(self, session: ProjectSession, path: Path) -> str:
        substituted_prefix = substitute(self.noninteractive_prefix, session=session, host=self._host)
        quoted = shlex.quote(str(path))
        # exit 42 distinguishes "file missing" from any other cat failure
        # (permissions, dead backend) — the resolver treats missing as a
        # normal miss but lets other failures propagate. Delivered over stdin
        # to a bare `sh` (not `sh -c '<script>'`) so it survives argv-flattening
        # prefixes like `ssh host …` — see paths_exist.
        script = f"[ -f {quoted} ] || exit 42\ncat {quoted}\n"
        composed = f"{substituted_prefix} sh".lstrip()
        argv = self.noninteractive_transport(composed)
        result = self.runner(argv, runner_cwd(self.host, session.session_root), stdin=script)
        debug.log_command(argv, session.session_root, result)
        if result.returncode == _READ_FILE_NOT_FOUND_EXIT:
            msg = f"backend {self.name!r}: {path} not found"
            raise BackendFileNotFoundError(msg)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            msg = f"backend {self.name!r} read_file failed for {path}: {stderr}"
            raise SessionBackendError(msg)
        return result.stdout


class UnknownBackendError(HopError):
    """Raised when a pinned backend name doesn't match any configured backend."""


def select_backend(
    session: ProjectSession,
    backends: Sequence[BackendConfig],
    *,
    pinned_name: str | None = None,
    runner: CommandRunner = _default_runner,
    transport: Transport = local_transport,
    host: str | None = None,
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
        substituted = substitute(candidate.activate, session=session, host=_substitution_host(host))
        argv = transport(substituted)
        result = runner(argv, runner_cwd(host, session.session_root))
        debug.log_command(argv, session.session_root, result)
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
    transport: Transport = local_transport,
    noninteractive_transport: Transport = local_transport,
    host: str | None = None,
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
        transport=transport,
        noninteractive_transport=noninteractive_transport,
        host=host,
    )


def substitute(template: str, *, session: ProjectSession, host: str = "localhost") -> str:
    replacements: dict[str, str] = {
        PLACEHOLDER_SESSION_ROOT: shlex.quote(str(session.session_root)),
        PLACEHOLDER_HOST: shlex.quote(host),
    }
    return _apply(template, replacements)


def _substitute_translate(
    template: str,
    *,
    session: ProjectSession,
    port: int | None,
    host: str = "localhost",
) -> str:
    replacements: dict[str, str] = {
        PLACEHOLDER_SESSION_ROOT: shlex.quote(str(session.session_root)),
        PLACEHOLDER_PORT: "" if port is None else shlex.quote(str(port)),
        PLACEHOLDER_HOST: shlex.quote(host),
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


def backend_lock_path(session: ProjectSession) -> Path:
    runtime_root = os.environ.get("XDG_RUNTIME_DIR") or gettempdir()
    runtime_dir = Path(runtime_root).expanduser().resolve() / "hop"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir / f"backend-{session.session_name}.lock"


def _flock_sh(
    command: str,
    *,
    session: ProjectSession,
    transport: Transport,
    host: str,
) -> tuple[str, ...]:
    # Serialize prepare and teardown for the same session: when `hop kill`
    # detaches its teardown via setsid -f (so it survives vicinae's SIGTERM),
    # a subsequent `hop` would otherwise race the still-running teardown and
    # leave podman-compose in an inconsistent state. flock(1) holds the lock
    # for the lifetime of the wrapped command, so even if our parent dies the
    # lock is held by the subprocess and the next caller blocks on it.
    #
    # ``-o`` closes the lock fd before exec'ing the wrapped command. Without
    # it, any detached daemon the prepare spawns (podman's aardvark-dns is
    # the one that bit us) inherits the fd and pins the lock open forever —
    # the next prepare/teardown then blocks on flock with no recourse short
    # of killing the daemon by hand.
    #
    # The flock stays *local* (it serializes host-side prepare/teardown); the
    # transport wraps only the command inside it, so for a remote session the
    # `compose down` runs over ssh while the lock is still held on the host.
    substituted = substitute(command, session=session, host=host)
    return ("flock", "-o", str(backend_lock_path(session)), *transport(substituted))
