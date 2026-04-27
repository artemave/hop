from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from hop.session import ProjectSession


@dataclass(frozen=True, slots=True)
class SessionState:
    name: str
    project_root: Path

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "project_root": str(self.project_root)}


def default_sessions_dir() -> Path:
    if override := os.environ.get("HOP_SESSIONS_DIR"):
        return Path(override)
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "hop" / "sessions"


def record_session(session: ProjectSession, *, sessions_dir: Path | None = None) -> None:
    target = sessions_dir if sessions_dir is not None else default_sessions_dir()
    target.mkdir(parents=True, exist_ok=True)
    payload = SessionState(name=session.session_name, project_root=session.project_root).to_json()
    (target / f"{session.session_name}.json").write_text(json.dumps(payload))


def forget_session(session_name: str, *, sessions_dir: Path | None = None) -> None:
    target = sessions_dir if sessions_dir is not None else default_sessions_dir()
    state_file = target / f"{session_name}.json"
    state_file.unlink(missing_ok=True)


def load_sessions(*, sessions_dir: Path | None = None) -> dict[str, SessionState]:
    target = sessions_dir if sessions_dir is not None else default_sessions_dir()
    if not target.is_dir():
        return {}
    sessions: dict[str, SessionState] = {}
    for path in target.iterdir():
        if path.suffix != ".json":
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        name = payload.get("name")
        project_root = payload.get("project_root")
        if not isinstance(name, str) or not isinstance(project_root, str):
            continue
        sessions[name] = SessionState(name=name, project_root=Path(project_root))
    return sessions
