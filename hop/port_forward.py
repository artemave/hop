"""Host-side ssh ``-L`` forwards for URLs served on a remote session's localhost."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING, Iterator

from hop import debug
from hop.errors import HopError

if TYPE_CHECKING:
    from hop.backends import CommandRunner


_FREE_PORT_ATTEMPTS = 3


class PortForwardError(HopError):
    """Raised when no host port could be forwarded to the remote port."""


def ssh_local_forward_argv(
    host: str, *, local_port: int, remote_port: int, ssh_options: tuple[str, ...]
) -> tuple[str, ...]:
    """ssh argv that adds ``local_port → remote localhost:remote_port`` to the master.

    A mux client's ``-L`` (rather than ``-O forward``) because ``-O`` needs a live
    master, while this call brings one up when it has died — and either way the
    forward outlives the client. Re-adding an identical forward succeeds, and
    ``ExitOnForwardFailure`` turns a busy local port into a non-zero exit instead
    of a warning — but only per ``-L``. A single ``localhost`` bind counts a
    ``::1``-only listener as success when ``127.0.0.1`` is taken (and a master
    spawned that way spins and never releases the caller's pipes), so each
    loopback address gets its own forward and both must bind.
    """

    return (
        "ssh",
        *ssh_options,
        "-o",
        "ExitOnForwardFailure=yes",
        *_loopback_forwards(local_port, remote_port),
        host,
        "true",
    )


def ssh_cancel_local_forward_argv(
    host: str, *, local_port: int, remote_port: int, ssh_options: tuple[str, ...]
) -> tuple[str, ...]:
    """ssh argv that drops whichever half of a failed forward the master kept.

    On a live master the ``-L``s are added one by one, so when ``::1`` is busy the
    ``127.0.0.1`` listener stays behind — still routing to the remote — after the
    call fails. Cancelling a forward that isn't there is harmless (exit 0), and with
    no master there is nothing to cancel.
    """

    return ("ssh", *ssh_options, "-O", "cancel", *_loopback_forwards(local_port, remote_port), host)


def _loopback_forwards(local_port: int, remote_port: int) -> tuple[str, ...]:
    return (
        "-L",
        f"127.0.0.1:{local_port}:localhost:{remote_port}",
        "-L",
        f"[::1]:{local_port}:localhost:{remote_port}",
    )


def forward_remote_port(
    host: str,
    remote_port: int,
    *,
    runner: CommandRunner,
    ssh_options: tuple[str, ...],
) -> int:
    """Forward a host port to ``remote_port`` on ``host`` and return the host port.

    The host port is the one this remote port was forwarded on before (so the
    browser keeps its cookies), else the same number, else whatever port is free.
    """

    forwards = _load_forwards(host)
    stderr = ""
    for local_port in _candidate_ports(forwards.get(str(remote_port)), remote_port):
        argv = ssh_local_forward_argv(host, local_port=local_port, remote_port=remote_port, ssh_options=ssh_options)
        result = runner(argv, Path.home())
        debug.log_command(argv, Path.home(), result)
        if result.returncode == 0:
            forwards[str(remote_port)] = local_port
            _save_forwards(host, forwards)
            return local_port
        stderr = (result.stderr or "").strip()
        cancel = ssh_cancel_local_forward_argv(
            host, local_port=local_port, remote_port=remote_port, ssh_options=ssh_options
        )
        debug.log_command(cancel, Path.home(), runner(cancel, Path.home()))
    msg = f"could not forward a local port to {host}:{remote_port}: {stderr}"
    raise PortForwardError(msg)


def _candidate_ports(previous: int | None, remote_port: int) -> Iterator[int]:
    yield previous if previous is not None else remote_port
    # A port the kernel hands out free on 127.0.0.1 can still be taken on ::1.
    for _ in range(_FREE_PORT_ATTEMPTS):
        yield _free_port()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def forwards_path(host: str) -> Path:
    runtime_root = os.environ.get("XDG_RUNTIME_DIR") or gettempdir()
    return Path(runtime_root).expanduser() / "hop" / "forwards" / f"{host}.json"


def _load_forwards(host: str) -> dict[str, int]:
    path = forwards_path(host)
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def _save_forwards(host: str, forwards: dict[str, int]) -> None:
    path = forwards_path(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(forwards))
