"""``hop ssh <host>`` — transport setup for remote sessions.

Opens an ssh ControlMaster to ``<host>``, reverse-forwards hopd's bridge API
socket onto the remote, installs the host-aware ``hop`` shim on the remote's
PATH, and drops into a remote login shell. From there ``cd <project> && hop``
starts a hop session on the host driven over that ssh connection.

The command does *only* transport setup — it neither creates a session nor
touches any container. Session creation happens when the user runs ``hop`` on
the remote (the installed shim reports ``(host, cwd)`` back to hopd).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from hop.backends import default_ssh_options
from hop.bridge import default_api_socket_path, render_bridge_shim
from hop.errors import HopError

# Where the shim is installed on the remote. ``$HOME`` is expanded by the remote
# shell at install time.
REMOTE_SHIM_PATH = "$HOME/.local/bin/hop"

# Where a copied ``kitten`` lands on the remote — same no-sudo ``~/.local/bin``
# as the shim, so a login shell's PATH finds it.
REMOTE_KITTEN_PATH = "$HOME/.local/bin/kitten"

# Fallback when the remote has no ``XDG_RUNTIME_DIR`` (no logind session).
_FALLBACK_REMOTE_RUNTIME = "/tmp"

SubprocessRunner = Callable[..., "subprocess.CompletedProcess[str]"]
# Return type ``None`` (not ``NoReturn``) so a test can inject a fake that
# returns; ``os.execvp``'s ``NoReturn`` satisfies it, and the covariant tuple
# arg lets ``os.execvp`` be the default without a wrapper.
Exec = Callable[[str, tuple[str, ...]], None]


def remote_bridge_socket(host: str, *, runner: SubprocessRunner) -> str:
    """Resolve where to reverse-forward hopd's socket on the remote.

    ``${XDG_RUNTIME_DIR}/hop/api.sock`` — the *same relative path* hopd listens
    on locally (``bridge.default_api_socket_path``), placed under the runtime
    dir so (1) a recipe's ``--socket "$XDG_RUNTIME_DIR/hop/api.sock"`` resolves
    identically whether the session is local or remote, and (2) a devcontainer
    that already bind-mounts the host runtime dir surfaces the bridge socket
    into the container with *no extra compose mount*. The path must be concrete:
    ``ssh -R`` binds a literal remote path and doesn't expand remote env, and
    ``hop ssh`` runs on the host — so query the remote's ``XDG_RUNTIME_DIR`` over
    a throwaway connection (``ControlPath=none`` so it doesn't become the master
    the ``-R``-bearing setup call needs to be).
    """

    argv = ("ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none", host, 'printf %s "${XDG_RUNTIME_DIR}"')
    result = runner(argv, text=True, capture_output=True, check=False)
    runtime = (result.stdout or "").strip() or _FALLBACK_REMOTE_RUNTIME
    return f"{runtime}/hop/api.sock"


def ssh_remote_argv(host: str, command: str) -> tuple[str, ...]:
    """ssh argv running ``command`` on ``host`` over (or establishing) the master."""

    return ("ssh", *default_ssh_options(), host, command)


def ssh_install_argv(host: str) -> tuple[str, ...]:
    """ssh argv that opens (or reuses) the master and installs the shim.

    The shim text is piped to this command's stdin and written by ``install``
    reading ``/dev/stdin``. ``default_ssh_options`` carries ControlMaster +
    ControlPersist, so the master persists in the background after this call
    returns. No ``-R`` here — the reverse-forward is managed separately via
    ``-O forward`` so it can be refreshed idempotently (see ``ssh_forward_argv``).
    """

    return ssh_remote_argv(
        host,
        f'mkdir -p "$(dirname {REMOTE_SHIM_PATH})" && install -m 755 /dev/stdin {REMOTE_SHIM_PATH}',
    )


def ssh_install_kitten_argv(host: str) -> tuple[str, ...]:
    """ssh argv that installs a piped ``kitten`` binary on the remote.

    Mirrors ``ssh_install_argv``: the local binary is piped to stdin and written
    by ``install`` reading ``/dev/stdin``, into the same no-sudo ``~/.local/bin``.
    """

    return ssh_remote_argv(
        host,
        f'mkdir -p "$(dirname {REMOTE_KITTEN_PATH})" && install -m 755 /dev/stdin {REMOTE_KITTEN_PATH}',
    )


def _ensure_remote_kitten(host: str, *, runner: SubprocessRunner) -> None:
    """Best-effort: copy the host's ``kitten`` onto the remote host.

    Under implicit shell integration a remote-host role window runs
    ``kitten run-shell``; copying the host's binary (a portable kitty release
    needing only an ancient glibc) makes that work with no manual setup. This
    never raises: a musl / mismatched-arch remote, a missing local ``kitten``,
    or an already-present remote one all just leave the remote to degrade + warn
    at the shell. It reaches only the remote *host* — a container behind an
    ssh→container backend still installs kitten in its ``prepare`` step.
    """

    probe = runner(
        ssh_remote_argv(host, "command -v kitten >/dev/null 2>&1"),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return
    local = shutil.which("kitten")
    if local is None:
        return
    runner(ssh_install_kitten_argv(host), input=Path(local).read_bytes(), capture_output=True, check=False)


def ssh_unlink_argv(host: str, remote_socket: str) -> tuple[str, ...]:
    """ssh argv preparing the remote forward path before re-binding.

    Ensures the parent dir exists (the socket lives under ``…/hop/``, which the
    remote may not have) and removes any stale socket file: for a remote
    (``-R``) unix forward the unlink-before-bind is the *server's* call
    (``StreamLocalBindUnlink`` in sshd_config), which the client can't force, so
    a leftover socket (from an earlier session or an unclean exit) makes
    ``-O forward`` fail with "remote port forwarding failed for listen path".
    """

    quoted = shlex.quote(remote_socket)
    return ssh_remote_argv(host, f'mkdir -p "$(dirname {quoted})" && rm -f {quoted}')


def ssh_forward_argv(host: str, operation: str, *, remote_socket: str, api_socket: Path) -> tuple[str, ...]:
    """ssh argv for a master control op (``forward``/``cancel``) on the reverse-forward.

    ``-O forward`` adds the ``-R`` forward to the *already-running master*, so it
    persists with the master rather than dying with the requesting session;
    ``-O cancel`` removes it. Re-running ``hop ssh`` cancels then re-adds,
    refreshing the forward in place — no master teardown, and a forward lost to a
    laptop-sleep is re-established on the next run.
    """

    return (
        "ssh",
        *default_ssh_options(),
        "-O",
        operation,
        "-R",
        f"{remote_socket}:{api_socket}",
        host,
    )


def ssh_shell_argv(host: str) -> tuple[str, ...]:
    """ssh argv for the interactive login shell that reuses the master."""

    return ("ssh", *default_ssh_options(), "-t", host)


def _setup_error(host: str, result: "subprocess.CompletedProcess[str]", what: str) -> str:
    stderr = (result.stderr or "").strip()
    detail = f": {stderr}" if stderr else ""
    return f"`hop ssh {host}` {what} failed{detail}"


def run_hop_ssh(
    host: str,
    *,
    api_socket: Path | None = None,
    runner: SubprocessRunner = subprocess.run,
    exec_: Exec = os.execvp,
) -> None:
    socket = api_socket if api_socket is not None else default_api_socket_path()
    if not socket.exists():
        msg = (
            f"hopd's bridge socket {socket} does not exist — start hopd before "
            "`hop ssh` (the reverse-forward needs it as its target)."
        )
        raise HopError(msg)
    remote_socket = remote_bridge_socket(host, runner=runner)
    shim = render_bridge_shim(socket_default=remote_socket, host_default=host)

    # Open (or reuse) the master and install the host-aware shim.
    install = runner(ssh_install_argv(host), input=shim, text=True, capture_output=True, check=False)
    if install.returncode != 0:
        raise HopError(_setup_error(host, install, "setup"))

    # Refresh the reverse-forward on the master idempotently: cancel any stale
    # forward, unlink any stale socket file the server won't (a remote -R unix
    # bind fails on a leftover path), then (re)add it. Both prep steps are
    # best-effort — there's nothing to clean on a first run. This survives
    # re-runs and laptop-sleep without tearing the master down by hand.
    runner(
        ssh_forward_argv(host, "cancel", remote_socket=remote_socket, api_socket=socket),
        capture_output=True,
        text=True,
        check=False,
    )
    runner(ssh_unlink_argv(host, remote_socket), capture_output=True, text=True, check=False)
    forward = runner(
        ssh_forward_argv(host, "forward", remote_socket=remote_socket, api_socket=socket),
        capture_output=True,
        text=True,
        check=False,
    )
    if forward.returncode != 0:
        raise HopError(_setup_error(host, forward, "reverse-forward"))

    # Best-effort — never blocks the drop-in shell (unlike the shim above).
    _ensure_remote_kitten(host, runner=runner)

    exec_("ssh", ssh_shell_argv(host))
