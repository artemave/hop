"""Tests for the remote-session ssh transport, `hop ssh`, and the host/cwd shim.

No mocks: fake runners/exec are injected (the established style), and the
``SshTransport`` is exercised by decoding the base64 payload it actually builds.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from typing import Sequence

import pytest

from hop.app import (
    SessionBackendRegistry,
    backend_from_record,
)
from hop.backends import (
    REMOTE_KITTEN_PATH,
    CommandBackend,
    SshTransport,
    default_ssh_options,
    substitute,
)
from hop.bridge import dispatch_remote
from hop.commands.ssh import (
    _install_remote_kitten,  # pyright: ignore[reportPrivateUsage]
    host_kitty_version,
    remote_bridge_socket,
    remote_kitten_install_script,
    run_hop_ssh,
    ssh_forward_argv,
    ssh_install_argv,
    ssh_remote_argv,
    ssh_shell_argv,
)
from hop.config import HopConfig, parse_project_config_text
from hop.errors import HopError
from hop.session import ProjectSession, remote_session_from_env
from hop.state import CommandBackendRecord, SessionState, load_sessions, record_session, session_from_state


def _remote_session(session_root: str = "/home/u/thonon-les-pains", host: str = "devbox") -> ProjectSession:
    root = Path(session_root)
    return ProjectSession(
        session_root=root,
        session_name=root.name,
        workspace_name=f"p:{root.name}",
        host=host,
    )


def _decode_remote(remote: str) -> str:
    token = remote.split("printf %s ")[1].split(" | base64")[0]
    return base64.b64decode(token).decode()


# --- SshTransport ------------------------------------------------------------


def test_ssh_transport_noninteractive_wraps_command_over_ssh() -> None:
    transport = SshTransport("devbox", "/home/u/proj", interactive=False)

    argv = transport("podman-compose exec -T devcontainer sh")

    assert argv[0] == "ssh"
    assert "-tt" not in argv
    assert argv[-2] == "devbox"
    assert _decode_remote(argv[-1]) == "cd /home/u/proj && podman-compose exec -T devcontainer sh"


def test_ssh_transport_interactive_allocates_a_remote_tty() -> None:
    transport = SshTransport("devbox", "/home/u/proj", interactive=True)

    argv = transport("bin/dev")

    assert "-tt" in argv
    assert _decode_remote(argv[-1]) == "cd /home/u/proj && bin/dev"


def test_ssh_transport_quotes_a_remote_cwd_with_spaces() -> None:
    transport = SshTransport("devbox", "/home/u/my proj", interactive=False)

    assert _decode_remote(transport("ls")[-1]) == "cd '/home/u/my proj' && ls"


def test_ssh_transport_payload_runs_a_stdin_script_through_a_real_shell(tmp_path: Path) -> None:
    """End-to-end proof of the quoting + stdin passthrough (no real ssh).

    Running the remote-side string through a local ``sh -c`` mirrors exactly what
    the remote sshd does (``$shell -c '<payload>'``): the base64 decodes, a login
    shell cd's into the remote cwd, and the composed ``sh`` reads the existence
    script off stdin — the same shape ``paths_exist`` relies on. Argv-only
    assertions can't catch a quoting/flattening regression here; this can.
    """

    (tmp_path / "present").touch()
    transport = SshTransport("stub", str(tmp_path), interactive=False)
    # paths_exist composes "<prefix> sh"; with an empty prefix that's just "sh".
    payload = transport("sh")[-1]

    result = subprocess.run(
        ["sh", "-c", payload],
        input='test -e present && printf "%s\\n" present\n:\n',
        capture_output=True,
        text=True,
        check=False,
    )

    assert "present" in result.stdout.splitlines()


def test_ssh_to_container_wrap_nests_both_login_wraps() -> None:
    """An ssh→container backend login-wraps twice: ``SshTransport`` wraps the
    whole command for a login shell on the remote host, and the container prefix
    login-wraps it again inside ``podman exec`` for a login shell in the
    container. Both are base64 layers; the innermost decoded command is the
    original, uncorrupted."""
    backend = CommandBackend(
        name="dc",
        interactive_prefix="podman exec dc",
        noninteractive_prefix="podman exec -T dc",
        transport=SshTransport("devbox", "/remote/proj", interactive=True),
        noninteractive_transport=SshTransport("devbox", "/remote/proj", interactive=False),
        host="devbox",
    )

    argv = backend.wrap("kitten run-shell", _remote_session())

    assert argv[0] == "ssh"
    ssh_inner = _decode_remote(argv[-1])
    assert ssh_inner.startswith("cd /remote/proj && podman exec dc sh -c ")
    assert _decode_remote(ssh_inner) == "kitten run-shell"


def test_default_ssh_options_multiplex_and_persist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    options = default_ssh_options()

    assert "ControlMaster=auto" in options
    assert f"ControlPath={tmp_path / 'hop' / 'cm-%r@%h:%p'}" in options
    assert "ControlPersist=600" in options


# --- _runner_cwd: a remote backend runs ssh locally from home ----------------


def test_remote_backend_runs_ssh_locally_from_home() -> None:
    calls: list[tuple[Sequence[str], Path]] = []

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> CompletedProcess[str]:
        calls.append((args, cwd))
        return CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    # Mirror the open-selection kitten: the session is rebuilt from a record and
    # carries *no* host, but the backend (rebuilt with the record's transport_host)
    # does. The local cwd must still be home — keyed off the backend, not the
    # session — or `subprocess.run(cwd=<remote path>)` raises FileNotFoundError.
    session = ProjectSession(
        session_root=Path("/remote/proj"),
        session_name="proj",
        workspace_name="p:proj",
        host=None,
    )
    backend = CommandBackend(
        name="dc",
        interactive_prefix="podman exec dc",
        noninteractive_prefix="podman exec -T dc",
        runner=runner,
        transport=SshTransport("devbox", "/remote/proj", interactive=True),
        noninteractive_transport=SshTransport("devbox", "/remote/proj", interactive=False),
        host="devbox",
    )

    backend.paths_exist(session, [Path("/remote/proj/a")])

    args, cwd = calls[0]
    # The local subprocess cwd is home (the remote path doesn't exist locally);
    # the remote cwd is carried inside the ssh payload instead.
    assert cwd == Path.home()
    assert args[0] == "ssh"


# --- {host} substitution -----------------------------------------------------


def test_host_placeholder_resolves_to_bare_hostname_remotely() -> None:
    session = _remote_session()
    backend = CommandBackend(
        name="dc",
        interactive_prefix="",
        noninteractive_prefix="",
        host="admin@devbox.local",  # the ssh target carries a user@
    )

    # `{host}` is the externally-reachable hostname (for LOCAL_HOSTNAME / host
    # translation), not the ssh target — the `user@` is stripped.
    assert backend.inline("LOCAL_HOSTNAME={host} bin/dev", session) == "LOCAL_HOSTNAME=devbox.local bin/dev"


def test_host_placeholder_resolves_to_localhost_locally() -> None:
    session = ProjectSession(session_root=Path("/p"), session_name="p", workspace_name="p:p")

    assert substitute("echo {host}", session=session) == "echo localhost"


# --- parse_project_config_text ----------------------------------------------


def test_parse_project_config_text_parses_in_memory_toml() -> None:
    config = parse_project_config_text(
        'workspace_layout = "tabbed"\n',
        source=Path("devbox:/home/u/proj/.hop.toml"),
    )

    assert config.workspace_layout == "tabbed"


# --- session record round-trip ----------------------------------------------


def test_transport_host_round_trips_through_the_session_record(tmp_path: Path) -> None:
    record = CommandBackendRecord(
        name="dc",
        interactive_prefix="podman exec dc",
        noninteractive_prefix="podman exec -T dc",
        transport_host="devbox",
    )
    assert record.to_json()["transport_host"] == "devbox"

    session = _remote_session("/remote/proj")
    record_session(session, backend=record, sessions_dir=tmp_path)

    loaded = load_sessions(sessions_dir=tmp_path)["proj"]
    assert loaded.backend.transport_host == "devbox"


# --- session_from_state carries the host -------------------------------------


def test_session_from_state_carries_host_from_record() -> None:
    state = SessionState(
        name="thonon-les-pains",
        session_root=Path("/home/admin/projects/thonon-les-pains"),
        backend=CommandBackendRecord(
            name="dc",
            interactive_prefix="podman exec dc",
            noninteractive_prefix="podman exec -T dc",
            transport_host="admin@devbox.local",
        ),
    )

    session = session_from_state(state)

    # Without the host, every session.host-keyed decision (transport, runner cwd)
    # would treat the remote session as local — the open-selection / editor bug.
    assert session.host == "admin@devbox.local"
    assert session.session_name == "thonon-les-pains"


def test_session_from_state_local_record_has_no_host() -> None:
    state = SessionState(
        name="proj",
        session_root=Path("/p"),
        backend=CommandBackendRecord(name="host", interactive_prefix="", noninteractive_prefix=""),
    )

    assert session_from_state(state).host is None


# --- backend_from_record rebuilds the transport ------------------------------


def test_backend_from_record_rebuilds_ssh_transport() -> None:
    record = CommandBackendRecord(
        name="dc",
        interactive_prefix="podman exec dc",
        noninteractive_prefix="podman exec -T dc",
        transport_host="devbox",
    )

    backend = backend_from_record(record, session_root=Path("/remote/proj"))

    assert isinstance(backend, CommandBackend)
    assert backend.host == "devbox"
    assert isinstance(backend.transport, SshTransport)
    assert isinstance(backend.noninteractive_transport, SshTransport)


def test_backend_from_record_local_record_stays_local() -> None:
    record = CommandBackendRecord(name="host", interactive_prefix="", noninteractive_prefix="")

    backend = backend_from_record(record, session_root=Path("/p"))

    assert isinstance(backend, CommandBackend)
    assert backend.host is None


# --- remote_session_from_env -------------------------------------------------


def test_remote_session_from_env_builds_remote_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOP_REMOTE_HOST", "devbox")
    monkeypatch.setenv("HOP_REMOTE_CWD", "/home/u/thonon-les-pains")

    session = remote_session_from_env()

    assert session is not None
    assert session.host == "devbox"
    assert session.session_name == "thonon-les-pains"
    assert session.workspace_name == "p:thonon-les-pains"
    assert session.session_root == Path("/home/u/thonon-les-pains")


def test_remote_session_from_env_is_none_without_both_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOP_REMOTE_HOST", raising=False)
    monkeypatch.delenv("HOP_REMOTE_CWD", raising=False)

    assert remote_session_from_env() is None


# --- registry fetches the remote .hop.toml over the transport ----------------


def test_registry_fetches_remote_config_over_the_transport() -> None:
    from hop import trust

    trust.record("devbox:/home/u/thonon-les-pains/.hop.toml", 'workspace_layout = "tabbed"\n')

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> CompletedProcess[str]:
        assert args[0] == "ssh"
        return CompletedProcess(args=list(args), returncode=0, stdout='workspace_layout = "tabbed"\n', stderr="")

    registry = SessionBackendRegistry(global_config_loader=HopConfig, runner=runner)

    assert registry.workspace_layout_for_entry(_remote_session()) == "tabbed"


def test_registry_remote_config_missing_yields_empty_config() -> None:
    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> CompletedProcess[str]:
        return CompletedProcess(args=list(args), returncode=1, stdout="", stderr="no such file")

    registry = SessionBackendRegistry(global_config_loader=HopConfig, runner=runner)

    assert registry.workspace_layout_for_entry(_remote_session()) is None


# --- dispatch_remote ---------------------------------------------------------


def test_dispatch_remote_passes_identity_and_argv_via_environment() -> None:
    captured: dict[str, object] = {}

    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[bytes]:
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return CompletedProcess(args=list(args), returncode=0, stdout=b"", stderr=b"")

    # A remote-machine `hop kill`: identity from (host, cwd), the verb in argv.
    dispatch_remote("devbox", "/home/u/proj", ["kill"], runner=runner)

    env = captured["kwargs"]["env"]  # type: ignore[index]
    assert env["HOP_REMOTE_HOST"] == "devbox"
    assert env["HOP_REMOTE_CWD"] == "/home/u/proj"
    assert captured["args"][1:] == ["-m", "hop", "kill"]  # type: ignore[index]


# --- hop ssh -----------------------------------------------------------------


_HOST_KITTY_VERSION = "0.47.2"


def _is_runtime_query(args: Sequence[str]) -> bool:
    return bool(args) and "XDG_RUNTIME_DIR" in args[-1]


def _is_kitty_version_query(args: Sequence[str]) -> bool:
    return tuple(args) == ("kitty", "--version")


def _stdout_for(args: Sequence[str]) -> str:
    """What a stand-in runner should print for the two queries `hop ssh` makes."""
    if _is_kitty_version_query(args):
        return f"kitty {_HOST_KITTY_VERSION} created by Kovid Goyal"
    if _is_runtime_query(args):
        return "/run/user/1001"
    return ""


def test_remote_bridge_socket_forwards_under_the_remote_runtime_dir() -> None:
    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        assert _is_runtime_query(args)
        assert "ControlPath=none" in args  # throwaway, not the master
        return CompletedProcess(args=list(args), returncode=0, stdout="/run/user/1001", stderr="")

    assert remote_bridge_socket("devbox", runner=runner) == "/run/user/1001/hop/api.sock"


def test_remote_bridge_socket_falls_back_when_no_runtime_dir() -> None:
    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    assert remote_bridge_socket("devbox", runner=runner) == "/tmp/hop/api.sock"


def _ssh_op(args: Sequence[str]) -> str | None:
    """The ``-O <op>`` control op in an ssh argv, if any (``forward``/``cancel``)."""
    if "-O" in args:
        return args[args.index("-O") + 1]
    return None


def test_ssh_install_argv_opens_master_and_installs_shim_without_forward() -> None:
    argv = ssh_install_argv("devbox")

    assert argv[0] == "ssh"
    assert "-R" not in argv  # the forward is managed separately, via -O forward
    assert argv[-2] == "devbox"
    assert "install -m 755 /dev/stdin" in argv[-1]


def test_ssh_forward_argv_targets_the_master_control_socket() -> None:
    argv = ssh_forward_argv(
        "devbox",
        "forward",
        remote_socket="/run/user/1001/hop/api.sock",
        api_socket=Path("/run/user/1000/hop/api.sock"),
    )

    assert _ssh_op(argv) == "forward"
    assert "-R" in argv
    assert "/run/user/1001/hop/api.sock:/run/user/1000/hop/api.sock" in argv
    assert argv[-1] == "devbox"


def test_ssh_shell_argv_opens_an_interactive_login_shell() -> None:
    argv = ssh_shell_argv("devbox")

    assert argv[0] == "ssh"
    assert "-t" in argv
    assert argv[-1] == "devbox"


def test_run_hop_ssh_requires_a_running_hopd(tmp_path: Path) -> None:
    with pytest.raises(HopError, match="does not exist"):
        run_hop_ssh("devbox", api_socket=tmp_path / "missing.sock")


def test_run_hop_ssh_installs_shim_refreshes_forward_then_execs(tmp_path: Path) -> None:
    socket = tmp_path / "api.sock"
    socket.touch()
    runs: list[Sequence[str]] = []
    install_kwargs: dict[str, object] = {}
    execs: list[tuple[str, tuple[str, ...]]] = []

    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        runs.append(args)
        if "install -m 755 /dev/stdin" in args[-1]:
            install_kwargs.update(kwargs)
        return CompletedProcess(args=list(args), returncode=0, stdout=_stdout_for(args), stderr="")

    def fake_exec(file: str, args: tuple[str, ...]) -> None:
        execs.append((file, args))

    run_hop_ssh("devbox", api_socket=socket, runner=runner, exec_=fake_exec)

    # Order: query runtime → install shim → cancel stale forward → prep socket
    # path (mkdir + rm) → add forward.
    assert _is_runtime_query(runs[0])
    assert "install -m 755 /dev/stdin" in runs[1][-1]
    assert _ssh_op(runs[2]) == "cancel"
    assert runs[3][-1] == 'mkdir -p "$(dirname /run/user/1001/hop/api.sock)" && rm -f /run/user/1001/hop/api.sock'
    assert _ssh_op(runs[4]) == "forward"
    # The forward (and the cancel) target the runtime-dir socket.
    assert f"/run/user/1001/hop/api.sock:{socket}" in runs[4]

    shim = install_kwargs["input"]
    assert isinstance(shim, str)
    assert "${HOP_SSH_HOST:-devbox}" in shim
    assert "${HOP_SOCKET:-/run/user/1001/hop/api.sock}" in shim

    assert execs == [("ssh", ssh_shell_argv("devbox"))]


def test_run_hop_ssh_raises_when_install_fails(tmp_path: Path) -> None:
    socket = tmp_path / "api.sock"
    socket.touch()

    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        if _is_runtime_query(args):
            return CompletedProcess(args=list(args), returncode=0, stdout="/run/user/1001", stderr="")
        return CompletedProcess(args=list(args), returncode=1, stdout="", stderr="permission denied")

    with pytest.raises(HopError, match="setup failed: permission denied"):
        run_hop_ssh("devbox", api_socket=socket, runner=runner, exec_=lambda _file, _args: None)


def test_run_hop_ssh_raises_when_forward_fails(tmp_path: Path) -> None:
    socket = tmp_path / "api.sock"
    socket.touch()
    execs: list[object] = []

    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        if _is_runtime_query(args):
            return CompletedProcess(args=list(args), returncode=0, stdout="/run/user/1001", stderr="")
        # install + cancel succeed; the `-O forward` is the one that fails.
        if _ssh_op(args) == "forward":
            return CompletedProcess(args=list(args), returncode=255, stdout="", stderr="forwarding refused")
        return CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    with pytest.raises(HopError, match="reverse-forward failed: forwarding refused"):
        run_hop_ssh("devbox", api_socket=socket, runner=runner, exec_=lambda _f, _a: execs.append((_f, _a)))

    # The forward failed before the shell could launch.
    assert execs == []


def test_host_kitty_version_reads_the_version_hop_pins_to() -> None:
    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(args=list(args), returncode=0, stdout=_stdout_for(args), stderr="")

    assert host_kitty_version(runner=runner) == _HOST_KITTY_VERSION


def test_host_kitty_version_raises_when_kitty_cannot_answer() -> None:
    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(args=list(args), returncode=127, stdout="", stderr="kitty: not found")

    with pytest.raises(HopError, match="could not read the host kitty version.*kitty: not found"):
        host_kitty_version(runner=runner)


def test_remote_kitten_install_script_pins_the_hosts_version() -> None:
    script = remote_kitten_install_script(_HOST_KITTY_VERSION)

    # The release tag — not `latest` — is what makes the remote's kitten match
    # the host's kitty, and `$os`/`$arch` stay unexpanded for the remote to fill.
    assert f"/releases/download/v{_HOST_KITTY_VERSION}/kitten-$os-$arch" in script
    assert REMOTE_KITTEN_PATH in script


# The install script is shell, so the tests below run it as shell. `curl` is
# stubbed on PATH — the one dependency that would otherwise pull 25M over the
# network per test — mirroring how this file already plants fake binaries on
# PATH. Everything else (uname, the version check, the verify-then-move) is the
# real thing.
_FAKE_KITTEN = """#!/bin/sh
case "$1" in
--version) echo "kitten {version} created by Kovid Goyal" ;;
run-shell) exit 0 ;;
esac
"""


def _stub_bin(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / name
    stub.write_text(body)
    stub.chmod(0o755)


def _stub_curl(directory: Path, *, writes: str, log: Path | None = None) -> None:
    """A `curl` that writes `writes` to its `-o` target instead of fetching."""
    record = f'echo "$@" >> {log}\n' if log is not None else ""
    body = (
        "#!/bin/sh\n"
        f"{record}"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "-o" ]; then shift; cat > "$1" <<\'PAYLOAD\'\n'
        f"{writes}"
        "PAYLOAD\n"
        "    exit 0\n"
        "  fi\n"
        "  shift\n"
        "done\n"
    )
    _stub_bin(directory, "curl", body)


def _run_install(script: str, *, cache: Path, stubs: Path) -> CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "XDG_CACHE_HOME": str(cache)}
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-s"], input=script, text=True, capture_output=True, env=env, check=False
    )


def _cached_kitten(cache: Path) -> Path:
    return cache / "hop" / "kitten"


def test_install_script_downloads_kitten_into_the_cache(tmp_path: Path) -> None:
    stubs, cache = tmp_path / "stubs", tmp_path / "cache"
    _stub_curl(stubs, writes=_FAKE_KITTEN.format(version=_HOST_KITTY_VERSION))

    result = _run_install(remote_kitten_install_script(_HOST_KITTY_VERSION), cache=cache, stubs=stubs)

    assert result.returncode == 0, result.stderr
    assert _cached_kitten(cache).is_file()
    assert os.access(_cached_kitten(cache), os.X_OK)


def test_install_script_is_a_no_op_when_the_cached_version_matches(tmp_path: Path) -> None:
    stubs, cache = tmp_path / "stubs", tmp_path / "cache"
    calls = tmp_path / "curl-calls"
    _stub_curl(stubs, writes=_FAKE_KITTEN.format(version=_HOST_KITTY_VERSION), log=calls)
    script = remote_kitten_install_script(_HOST_KITTY_VERSION)

    assert _run_install(script, cache=cache, stubs=stubs).returncode == 0
    assert _run_install(script, cache=cache, stubs=stubs).returncode == 0

    # One fetch per kitty version, not one per `hop ssh` — the second run saw a
    # cached binary already reporting the pinned version and stopped.
    assert calls.read_text().count("\n") == 1


def test_install_script_refetches_when_the_cached_version_is_stale(tmp_path: Path) -> None:
    stubs, cache = tmp_path / "stubs", tmp_path / "cache"
    _stub_curl(stubs, writes=_FAKE_KITTEN.format(version="0.40.0"))
    assert _run_install(remote_kitten_install_script("0.40.0"), cache=cache, stubs=stubs).returncode == 0

    # Host kitty upgraded: the cached 0.40.0 no longer matches, so it's replaced
    # in place rather than accumulating a second binary.
    _stub_curl(stubs, writes=_FAKE_KITTEN.format(version="0.48.0"))
    assert _run_install(remote_kitten_install_script("0.48.0"), cache=cache, stubs=stubs).returncode == 0

    assert (
        "0.48.0"
        in subprocess.run(  # noqa: S603
            [str(_cached_kitten(cache)), "--version"], capture_output=True, text=True, check=True
        ).stdout
    )
    assert sorted(p.name for p in (cache / "hop").iterdir()) == ["kitten"]


def test_install_script_rejects_a_download_that_does_not_run(tmp_path: Path) -> None:
    stubs, cache = tmp_path / "stubs", tmp_path / "cache"
    _stub_curl(stubs, writes="TRUNCATED-GARBAGE\n")

    result = _run_install(remote_kitten_install_script(_HOST_KITTY_VERSION), cache=cache, stubs=stubs)

    # Verified before it's moved into place: with no fallback in the remote
    # integration shell, a broken binary here would open dead windows.
    assert result.returncode != 0
    assert "does not run on this host" in result.stderr
    assert not _cached_kitten(cache).exists()
    assert list((cache / "hop").iterdir()) == []


def test_install_script_keeps_a_working_cached_kitten_when_a_refetch_fails(tmp_path: Path) -> None:
    stubs, cache = tmp_path / "stubs", tmp_path / "cache"
    _stub_curl(stubs, writes=_FAKE_KITTEN.format(version="0.40.0"))
    assert _run_install(remote_kitten_install_script("0.40.0"), cache=cache, stubs=stubs).returncode == 0

    _stub_curl(stubs, writes="TRUNCATED-GARBAGE\n")
    assert _run_install(remote_kitten_install_script("0.48.0"), cache=cache, stubs=stubs).returncode != 0

    assert (
        "0.40.0"
        in subprocess.run(  # noqa: S603
            [str(_cached_kitten(cache)), "--version"], capture_output=True, text=True, check=True
        ).stdout
    )


def test_install_script_reports_an_unsupported_platform(tmp_path: Path) -> None:
    stubs, cache = tmp_path / "stubs", tmp_path / "cache"
    _stub_curl(stubs, writes=_FAKE_KITTEN.format(version=_HOST_KITTY_VERSION))
    _stub_bin(stubs, "uname", '#!/bin/sh\ncase "$1" in -s) echo Linux ;; -m) echo sparc64 ;; esac\n')

    result = _run_install(remote_kitten_install_script(_HOST_KITTY_VERSION), cache=cache, stubs=stubs)

    assert result.returncode != 0
    assert "no kitten build published for sparc64" in result.stderr


def test_install_remote_kitten_pipes_the_script_to_a_bare_remote_sh() -> None:
    runs: list[Sequence[str]] = []
    kwargs_seen: dict[str, object] = {}

    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        runs.append(args)
        kwargs_seen.update(kwargs)
        return CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    _install_remote_kitten("devbox", _HOST_KITTY_VERSION, runner=runner)

    # A bare `sh` is one token that survives ssh's argv-flattening, and the
    # multi-line script rides stdin — so a non-POSIX remote login shell (fish,
    # csh) never has to parse it.
    assert runs[0] == ssh_remote_argv("devbox", "sh")
    assert kwargs_seen["input"] == remote_kitten_install_script(_HOST_KITTY_VERSION)


def test_install_remote_kitten_raises_with_the_remote_error(tmp_path: Path) -> None:
    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(args=list(args), returncode=1, stdout="", stderr="curl: not found")

    with pytest.raises(HopError, match=f"kitten {_HOST_KITTY_VERSION} install failed: curl: not found"):
        _install_remote_kitten("devbox", _HOST_KITTY_VERSION, runner=runner)


def test_run_hop_ssh_raises_instead_of_dropping_into_a_shell_without_kitten(tmp_path: Path) -> None:
    socket = tmp_path / "api.sock"
    socket.touch()
    execs: list[object] = []

    def runner(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        code = 1 if tuple(args) == ssh_remote_argv("devbox", "sh") else 0
        stderr = "no kitten build published for sparc64" if code else ""
        return CompletedProcess(args=list(args), returncode=code, stdout=_stdout_for(args), stderr=stderr)

    with pytest.raises(HopError, match="install failed: no kitten build published for sparc64"):
        run_hop_ssh("devbox", api_socket=socket, runner=runner, exec_=lambda f, a: execs.append((f, a)))

    # Dropping into the shell anyway would hand back a session whose every
    # window dies on `exec <cache>/kitten run-shell`.
    assert execs == []
