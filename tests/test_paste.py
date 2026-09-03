from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from hop.backends import CommandBackend, SessionBackendError
from hop.paste import _resolve_from_state, _wl_paste_image, run_paste_helper  # pyright: ignore[reportPrivateUsage]
from hop.session import ProjectSession
from hop.state import record_session


def build_session(root: Path) -> ProjectSession:
    return ProjectSession(session_root=root, session_name="demo", workspace_name="p:demo")


class RecordingBackend:
    """A ``write_file``-only stand-in for ``SessionBackend`` (the resolver
    hands it back, so the rest of the Protocol is never touched)."""

    def __init__(self, *, raises: Exception | None = None, block: bool = False) -> None:
        self.writes: list[tuple[Path, bytes]] = []
        self._raises = raises
        self._block = block

    def write_file(self, session: ProjectSession, path: Path, data: bytes) -> None:
        if self._block:
            # Stand in for a wedged ssh / compose write: block until the
            # helper's itimer interrupts us.
            threading.Event().wait()
        if self._raises is not None:
            raise self._raises
        self.writes.append((path, data))


def test_run_paste_helper_writes_bytes_and_returns_zero(tmp_path: Path) -> None:
    backend = RecordingBackend()
    session = build_session(tmp_path)

    code = run_paste_helper(
        session_name="demo",
        write_path="/tmp/hop-paste-1.png",
        mime="image/png",
        resolve=lambda _name: (session, backend),  # type: ignore[arg-type]
        read_clipboard=lambda _mime: b"PNGBYTES",
    )

    assert code == 0
    assert backend.writes == [(Path("/tmp/hop-paste-1.png"), b"PNGBYTES")]


def test_run_paste_helper_unknown_session_returns_one(capsys: pytest.CaptureFixture[str]) -> None:
    code = run_paste_helper(
        session_name="ghost",
        write_path="/tmp/x.png",
        mime="image/png",
        resolve=lambda _name: None,
        read_clipboard=lambda _mime: b"x",
    )

    assert code == 1
    assert "no active session 'ghost'" in capsys.readouterr().err


def test_run_paste_helper_empty_clipboard_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    backend = RecordingBackend()
    session = build_session(tmp_path)

    code = run_paste_helper(
        session_name="demo",
        write_path="/tmp/x.png",
        mime="image/png",
        resolve=lambda _name: (session, backend),  # type: ignore[arg-type]
        read_clipboard=lambda _mime: None,
    )

    assert code == 1
    assert backend.writes == []
    assert "nothing of type 'image/png'" in capsys.readouterr().err


def test_run_paste_helper_backend_error_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    backend = RecordingBackend(raises=SessionBackendError("read-only filesystem"))
    session = build_session(tmp_path)

    code = run_paste_helper(
        session_name="demo",
        write_path="/tmp/x.png",
        mime="image/png",
        resolve=lambda _name: (session, backend),  # type: ignore[arg-type]
        read_clipboard=lambda _mime: b"x",
    )

    assert code == 1
    assert "read-only filesystem" in capsys.readouterr().err


def test_run_paste_helper_times_out_a_wedged_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    backend = RecordingBackend(block=True)
    session = build_session(tmp_path)
    started = time.monotonic()

    code = run_paste_helper(
        session_name="demo",
        write_path="/tmp/x.png",
        mime="image/png",
        resolve=lambda _name: (session, backend),  # type: ignore[arg-type]
        read_clipboard=lambda _mime: b"x",
        write_timeout=0.2,
    )

    assert code == 1
    assert time.monotonic() - started < 5
    assert "exceeded 0.2s" in capsys.readouterr().err


def test_resolve_from_state_rebuilds_session_and_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOP_SESSIONS_DIR", str(tmp_path))
    # ``session_from_state`` re-derives the name from the root basename, so the
    # dir has to be named for the session (the production invariant).
    project = tmp_path / "demo"
    project.mkdir()
    record_session(build_session(project))

    resolved = _resolve_from_state("demo")

    assert resolved is not None
    session, backend = resolved
    assert session.session_name == "demo"
    target = tmp_path / "out.png"
    backend.write_file(session, target, b"RT")
    assert target.read_bytes() == b"RT"


def test_resolve_from_state_returns_none_for_unknown_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOP_SESSIONS_DIR", str(tmp_path))

    assert _resolve_from_state("ghost") is None


def test_wl_paste_image_returns_bytes_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"\x89PNG...", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _wl_paste_image("image/png") == b"\x89PNG..."
    assert calls == [["wl-paste", "--type", "image/png", "--no-newline"]]


def test_wl_paste_image_returns_none_when_missing_or_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_argv: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("wl-paste")

    monkeypatch.setattr(subprocess, "run", missing)
    assert _wl_paste_image("image/png") is None

    def empty(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"no data")

    monkeypatch.setattr(subprocess, "run", empty)
    assert _wl_paste_image("image/png") is None


def test_wl_paste_image_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(_argv: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="wl-paste", timeout=10.0)

    monkeypatch.setattr(subprocess, "run", slow)
    assert _wl_paste_image("image/png") is None


def test_run_paste_helper_through_a_real_host_backend(tmp_path: Path) -> None:
    """End-to-end through a real host ``CommandBackend``: the clipboard bytes
    land on disk at ``write_path``."""

    backend = CommandBackend(name="host", interactive_prefix="", noninteractive_prefix="")
    target = tmp_path / "nested" / "shot.png"
    payload = bytes(range(256))
    session = build_session(tmp_path)

    code = run_paste_helper(
        session_name="demo",
        write_path=str(target),
        mime="image/png",
        resolve=lambda _name: (session, backend),
        read_clipboard=lambda _mime: payload,
    )

    assert code == 0
    assert target.read_bytes() == payload
