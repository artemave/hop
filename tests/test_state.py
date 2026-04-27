import json
from pathlib import Path

import pytest
from hop.session import ProjectSession
from hop.state import (
    SessionState,
    default_sessions_dir,
    forget_session,
    load_sessions,
    record_session,
)


def make_session(*, name: str, project_root: Path) -> ProjectSession:
    return ProjectSession(
        session_name=name,
        project_root=project_root,
        workspace_name=f"p:{name}",
    )


def test_record_session_writes_json_payload(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session = make_session(name="demo", project_root=tmp_path / "demo")

    record_session(session, sessions_dir=sessions_dir)

    payload = json.loads((sessions_dir / "demo.json").read_text())
    assert payload == {"name": "demo", "project_root": str(tmp_path / "demo")}


def test_forget_session_removes_state_file(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    state_file = sessions_dir / "demo.json"
    state_file.write_text("{}")

    forget_session("demo", sessions_dir=sessions_dir)

    assert not state_file.exists()


def test_forget_session_is_idempotent_when_state_file_missing(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    forget_session("demo", sessions_dir=sessions_dir)


def test_load_sessions_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert load_sessions(sessions_dir=tmp_path / "missing") == {}


def test_load_sessions_skips_non_json_and_malformed_files(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "alpha.json").write_text(json.dumps({"name": "alpha", "project_root": "/projects/alpha"}))
    (sessions_dir / "beta.txt").write_text("not json")
    (sessions_dir / "broken.json").write_text("{not valid json")
    (sessions_dir / "wrong-shape.json").write_text(json.dumps({"name": 1, "project_root": "/x"}))

    sessions = load_sessions(sessions_dir=sessions_dir)

    assert sessions == {"alpha": SessionState(name="alpha", project_root=Path("/projects/alpha"))}


def test_default_sessions_dir_honors_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOP_SESSIONS_DIR", "/custom/sessions")
    assert default_sessions_dir() == Path("/custom/sessions")


def test_default_sessions_dir_prefers_xdg_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOP_SESSIONS_DIR", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert default_sessions_dir() == Path("/run/user/1000/hop/sessions")


def test_default_sessions_dir_falls_back_to_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOP_SESSIONS_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_sessions_dir() == Path("/tmp/hop/sessions")
