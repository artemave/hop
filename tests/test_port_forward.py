from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pytest

from hop.port_forward import (
    PortForwardError,
    forward_remote_port,
    forwards_path,
    ssh_cancel_local_forward_argv,
    ssh_local_forward_argv,
)


@pytest.fixture(autouse=True)
def isolate_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


@dataclass
class BindingRunner:
    """Stands in for ssh: a forward succeeds only when both loopback addresses
    of its local port are free, as with ``ExitOnForwardFailure``.

    Binding the requested port for real is the behaviour under test. ``-O
    cancel`` calls are recorded and succeed.
    """

    calls: list[tuple[str, ...]] = field(default_factory=lambda: [])

    def __call__(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if "cancel" in args:
            return subprocess.CompletedProcess(list(args), 0, "", "")
        local_port = _local_port(tuple(args))
        try:
            with socket.socket() as v4, socket.socket(socket.AF_INET6) as v6:
                v4.bind(("127.0.0.1", local_port))
                v6.bind(("::1", local_port))
        except OSError:
            return subprocess.CompletedProcess(list(args), 255, "", "Address already in use")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    def forwards(self) -> list[int]:
        return [_local_port(argv) for argv in self.calls if "cancel" not in argv]

    def cancels(self) -> list[int]:
        return [_local_port(argv) for argv in self.calls if "cancel" in argv]


def _local_port(argv: tuple[str, ...]) -> int:
    return int(argv[argv.index("-L") + 1].split(":")[1])


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_ssh_local_forward_argv_forwards_both_loopback_addresses_failing_on_busy() -> None:
    assert ssh_local_forward_argv("devbox", local_port=3001, remote_port=3000, ssh_options=("-o", "X=1")) == (
        "ssh",
        "-o",
        "X=1",
        "-o",
        "ExitOnForwardFailure=yes",
        "-L",
        "127.0.0.1:3001:localhost:3000",
        "-L",
        "[::1]:3001:localhost:3000",
        "devbox",
        "true",
    )


def test_ssh_cancel_local_forward_argv_cancels_both_loopback_addresses() -> None:
    assert ssh_cancel_local_forward_argv("devbox", local_port=3001, remote_port=3000, ssh_options=("-o", "X=1")) == (
        "ssh",
        "-o",
        "X=1",
        "-O",
        "cancel",
        "-L",
        "127.0.0.1:3001:localhost:3000",
        "-L",
        "[::1]:3001:localhost:3000",
        "devbox",
    )


def test_forward_uses_the_remote_port_number_when_it_is_free_locally() -> None:
    remote_port = _free_port()
    runner = BindingRunner()

    assert forward_remote_port("devbox", remote_port, runner=runner, ssh_options=()) == remote_port
    assert json.loads(forwards_path("devbox").read_text()) == {str(remote_port): remote_port}


@pytest.mark.parametrize(("family", "address"), [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
def test_forward_falls_back_to_a_free_port_when_either_loopback_address_is_busy(
    family: socket.AddressFamily, address: str
) -> None:
    runner = BindingRunner()
    with socket.socket(family) as occupied:
        occupied.bind((address, 0))
        busy_port = occupied.getsockname()[1]

        local_port = forward_remote_port("devbox", busy_port, runner=runner, ssh_options=())

    assert local_port != busy_port
    assert runner.forwards() == [busy_port, local_port]
    assert runner.cancels() == [busy_port]
    assert json.loads(forwards_path("devbox").read_text()) == {str(busy_port): local_port}


def test_forward_reuses_the_previously_forwarded_local_port() -> None:
    previous = _free_port()
    forwards_path("devbox").parent.mkdir(parents=True)
    forwards_path("devbox").write_text(json.dumps({"3000": previous}))
    runner = BindingRunner()

    assert forward_remote_port("devbox", 3000, runner=runner, ssh_options=()) == previous
    assert runner.forwards() == [previous]


def test_forwards_are_recorded_per_host() -> None:
    runner = BindingRunner()
    port = _free_port()

    forward_remote_port("devbox", port, runner=runner, ssh_options=())

    assert not forwards_path("otherbox").exists()


def test_forward_raises_when_no_local_port_can_be_forwarded() -> None:
    def failing(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), 255, "", "Connection refused")

    with pytest.raises(PortForwardError, match="could not forward a local port to devbox:3000: Connection refused"):
        forward_remote_port("devbox", 3000, runner=failing, ssh_options=())
    assert not forwards_path("devbox").exists()
