from __future__ import annotations

import base64
import contextlib
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable, Iterator, Sequence

import pytest

from hop.bridge import BRIDGE_SHIM, BridgeServer, default_api_socket_path, dispatch_via_subprocess, serve_forever
from hop.session import ProjectSession
from hop.state import record_session
from hop.sway import SwayWindow


def _curl_available() -> bool:
    return shutil.which("curl") is not None


pytestmark = pytest.mark.skipif(not _curl_available(), reason="curl is required")


@dataclass(frozen=True, slots=True)
class CurlResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def _parse_headers(raw: bytes) -> tuple[int, dict[str, str]]:
    text = raw.decode("iso-8859-1")
    lines = text.splitlines()
    status = int(lines[0].split(" ", 2)[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers


def _curl_post(socket_path: Path, body: bytes) -> CurlResponse:
    with tempfile.NamedTemporaryFile() as hdr_file:
        proc = subprocess.run(
            [
                "curl",
                "-sS",
                "--unix-socket",
                str(socket_path),
                "-D",
                hdr_file.name,
                "--data-binary",
                "@-",
                "-H",
                "Content-Type: application/octet-stream",
                "http://_/call",
            ],
            input=body,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"curl failed: rc={proc.returncode} stderr={proc.stderr!r}")
        hdr_file.seek(0)
        status, headers = _parse_headers(hdr_file.read())
    return CurlResponse(status=status, headers=headers, body=proc.stdout)


@contextlib.contextmanager
def _running_bridge(
    socket_path: Path,
    sway_source: Callable[[], Sequence[SwayWindow]],
    dispatcher: Callable[[ProjectSession, Sequence[str]], CompletedProcess[bytes]],
    sessions_dir: Path | None = None,
) -> Iterator[BridgeServer]:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = BridgeServer(
        str(socket_path),
        sway_source=sway_source,
        dispatcher=dispatcher,
        sessions_dir=sessions_dir,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _editor_window(session_name: str) -> SwayWindow:
    return SwayWindow(
        id=1,
        workspace_name=f"p:{session_name}",
        app_id="hop:editor",
        window_class=None,
        marks=(f"_hop_editor:{session_name}",),
        focused=True,
    )


def _record_demo_session(sessions_dir: Path, project_root: Path, name: str = "demo") -> ProjectSession:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session = ProjectSession(
        project_root=project_root.resolve(),
        session_name=name,
        workspace_name=f"p:{name}",
    )
    record_session(session, sessions_dir=sessions_dir)
    return session


def test_round_trip_with_focused_editor_window(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    _record_demo_session(sessions_dir, tmp_path)

    sway_windows = [_editor_window("demo")]

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        del session, argv
        return CompletedProcess(args=[], returncode=0, stdout=b"abc123\n", stderr=b"")

    with _running_bridge(socket_path, lambda: sway_windows, dispatcher, sessions_dir=sessions_dir):
        response = _curl_post(socket_path, b"hop\x00run\x00--role\x00test\x00ls\x00")

    assert response.status == 200
    assert response.body == b"abc123\n"
    assert response.headers["x-hop-exit"] == "0"
    assert response.headers["x-hop-stderr"] == ""


def test_non_zero_exit_propagates(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    _record_demo_session(sessions_dir, tmp_path)

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        del session, argv
        return CompletedProcess(args=[], returncode=2, stdout=b"out", stderr=b"err")

    with _running_bridge(socket_path, lambda: [_editor_window("demo")], dispatcher, sessions_dir=sessions_dir):
        response = _curl_post(socket_path, b"hop\x00fail\x00")

    assert response.status == 200
    assert response.body == b"out"
    assert response.headers["x-hop-exit"] == "2"
    assert base64.b64decode(response.headers["x-hop-stderr"]) == b"err"


def test_no_focused_window_returns_400(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        raise AssertionError("dispatcher should not be invoked")

    with _running_bridge(socket_path, lambda: [], dispatcher, sessions_dir=sessions_dir):
        response = _curl_post(socket_path, b"hop\x00")

    assert response.status == 400
    assert b"no focused Sway window" in response.body


def test_focused_window_without_editor_mark_returns_400(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        raise AssertionError("dispatcher should not be invoked")

    bare_window = SwayWindow(
        id=99,
        workspace_name="p:demo",
        app_id="kitty",
        window_class=None,
        marks=("_hop_browser:demo",),
        focused=True,
    )
    with _running_bridge(socket_path, lambda: [bare_window], dispatcher, sessions_dir=sessions_dir):
        response = _curl_post(socket_path, b"hop\x00")

    assert response.status == 400
    assert b"focus your editor window first" in response.body


def test_mark_points_to_unknown_session_returns_400(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        raise AssertionError("dispatcher should not be invoked")

    with _running_bridge(socket_path, lambda: [_editor_window("ghost")], dispatcher, sessions_dir=sessions_dir):
        response = _curl_post(socket_path, b"hop\x00")

    assert response.status == 400
    assert b"'ghost'" in response.body
    assert b"not in hop state" in response.body


def test_dispatcher_receives_resolved_session(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    expected = _record_demo_session(sessions_dir, tmp_path)

    captured: list[tuple[ProjectSession, list[str]]] = []

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        captured.append((session, list(argv)))
        return CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    with _running_bridge(socket_path, lambda: [_editor_window("demo")], dispatcher, sessions_dir=sessions_dir):
        _curl_post(socket_path, b"hop\x00run\x00--role\x00test\x00ls\x00")

    assert len(captured) == 1
    session, argv = captured[0]
    assert session.project_root == expected.project_root
    assert session.session_name == "demo"
    assert argv == ["run", "--role", "test", "ls"]


def test_stderr_round_trips_through_base64_header(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    _record_demo_session(sessions_dir, tmp_path)

    binary_stderr = b"line1\n\x00\x01\xff\xfeline2\n"

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        del session, argv
        return CompletedProcess(args=[], returncode=1, stdout=b"", stderr=binary_stderr)

    with _running_bridge(socket_path, lambda: [_editor_window("demo")], dispatcher, sessions_dir=sessions_dir):
        response = _curl_post(socket_path, b"hop\x00")

    assert base64.b64decode(response.headers["x-hop-stderr"]) == binary_stderr


def test_dispatch_via_subprocess_runs_real_hop(tmp_path: Path) -> None:
    session = ProjectSession(
        project_root=tmp_path.resolve(),
        session_name="demo",
        workspace_name="p:demo",
    )
    result = dispatch_via_subprocess(session, ["--help"])

    assert result.returncode == 0
    assert b"usage:" in result.stdout.lower()


def test_serve_forever_unlinks_stale_socket_and_serves(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    # Pre-create the socket path as a regular file to simulate stale state.
    socket_path.write_text("stale")

    sway_windows: list[SwayWindow] = []

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        raise AssertionError("dispatcher should not be invoked")

    def runner() -> None:
        with contextlib.suppress(Exception):
            serve_forever(
                socket_path,
                sway_source=lambda: list(sway_windows),
                dispatcher=dispatcher,
                sessions_dir=sessions_dir,
            )

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    # Wait until the rebound socket is ready (unlink + bind).
    for _ in range(50):
        if socket_path.is_socket():
            break
        threading.Event().wait(0.01)
    assert socket_path.is_socket(), "serve_forever did not rebind the socket"

    response = _curl_post(socket_path, b"hop\x00")
    assert response.status == 400

    socket_path.unlink(missing_ok=True)


def test_serve_forever_binds_when_no_stale_socket(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    assert not socket_path.exists()

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        raise AssertionError("dispatcher should not be invoked")

    def runner() -> None:
        with contextlib.suppress(Exception):
            serve_forever(
                socket_path,
                sway_source=lambda: [],
                dispatcher=dispatcher,
                sessions_dir=sessions_dir,
            )

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    for _ in range(50):
        if socket_path.is_socket():
            break
        threading.Event().wait(0.01)
    assert socket_path.is_socket(), "serve_forever did not bind the socket"

    response = _curl_post(socket_path, b"hop\x00")
    assert response.status == 400

    socket_path.unlink(missing_ok=True)


def test_unknown_path_returns_404(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        raise AssertionError("dispatcher should not be invoked")

    with _running_bridge(socket_path, lambda: [], dispatcher, sessions_dir=sessions_dir):
        proc = subprocess.run(
            [
                "curl",
                "-sS",
                "--unix-socket",
                str(socket_path),
                "-D",
                "-",
                "--data-binary",
                "@-",
                "http://_/unknown",
            ],
            input=b"",
            capture_output=True,
            check=False,
        )
    assert b"404" in proc.stdout.splitlines()[0]
    assert b"unknown path" in proc.stdout


def test_dispatcher_exception_returns_500(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    _record_demo_session(sessions_dir, tmp_path)

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        raise RuntimeError("boom")

    with _running_bridge(socket_path, lambda: [_editor_window("demo")], dispatcher, sessions_dir=sessions_dir):
        response = _curl_post(socket_path, b"hop\x00")

    assert response.status == 500
    assert b"boom" in response.body


def test_default_api_socket_path_uses_xdg_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/42")
    assert default_api_socket_path() == Path("/run/user/42/hop/api.sock")


def test_default_api_socket_path_falls_back_to_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_api_socket_path() == Path("/tmp/hop/api.sock")


def _run_shim(socket_path: Path, shim_path: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["sh", str(shim_path), *args],
        env={"HOP_SOCKET": str(socket_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        check=False,
    )


def test_shim_round_trips_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    _record_demo_session(sessions_dir, tmp_path)
    shim_path = tmp_path / "hop-shim.sh"
    shim_path.write_text(BRIDGE_SHIM)

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        del session, argv
        return CompletedProcess(args=[], returncode=7, stdout=b"hello\n", stderr=b"warn line\n")

    with _running_bridge(socket_path, lambda: [_editor_window("demo")], dispatcher, sessions_dir=sessions_dir):
        result = _run_shim(socket_path, shim_path, ["run", "--role", "test", "ls"])

    assert result.returncode == 7
    assert result.stdout == b"hello\n"
    assert result.stderr == b"warn line\n"


def test_shim_surfaces_acceptor_error_to_stderr(tmp_path: Path) -> None:
    socket_path = tmp_path / "api.sock"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    shim_path = tmp_path / "hop-shim.sh"
    shim_path.write_text(BRIDGE_SHIM)

    def dispatcher(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]:
        raise AssertionError("dispatcher should not be invoked")

    # No focused window → acceptor returns 400; shim must route body to stderr
    # and exit 1.
    with _running_bridge(socket_path, lambda: [], dispatcher, sessions_dir=sessions_dir):
        result = _run_shim(socket_path, shim_path, ["edit"])

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"no focused Sway window" in result.stderr


def test_shim_fails_when_socket_is_missing(tmp_path: Path) -> None:
    socket_path = tmp_path / "missing.sock"
    shim_path = tmp_path / "hop-shim.sh"
    shim_path.write_text(BRIDGE_SHIM)

    result = _run_shim(socket_path, shim_path, ["edit"])

    assert result.returncode == 2
    # curl wrote the connection diagnostic to its own stderr (because we use -sS).
    assert b"curl" in result.stderr.lower() or b"connect" in result.stderr.lower()
