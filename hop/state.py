from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from hop.session import ProjectSession


@dataclass(frozen=True, slots=True)
class HostBackendRecord:
    type: str = "host"

    def to_json(self) -> dict[str, object]:
        return {"type": "host"}


@dataclass(frozen=True, slots=True)
class CommandBackendRecord:
    """Persisted command-template backend chosen at session creation.

    Stores the name and the resolved (post-merge, post-substitution-template)
    command lists, plus the workspace path discovered at bootstrap. Subsequent
    commands instantiate a CommandBackend directly from this record without
    re-reading the global config.
    """

    name: str
    shell: tuple[str, ...]
    editor: tuple[str, ...]
    prepare: tuple[str, ...] | None = None
    teardown: tuple[str, ...] | None = None
    workspace_command: tuple[str, ...] | None = None
    workspace_path: str | None = None
    type: str = "command"

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": "command",
            "name": self.name,
            "shell": list(self.shell),
            "editor": list(self.editor),
        }
        if self.prepare is not None:
            payload["prepare"] = list(self.prepare)
        if self.teardown is not None:
            payload["teardown"] = list(self.teardown)
        if self.workspace_command is not None:
            payload["workspace_command"] = list(self.workspace_command)
        if self.workspace_path is not None:
            payload["workspace_path"] = self.workspace_path
        return payload


BackendRecord = HostBackendRecord | CommandBackendRecord


@dataclass(frozen=True, slots=True)
class SessionState:
    name: str
    project_root: Path
    backend: BackendRecord = field(default_factory=HostBackendRecord)

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "project_root": str(self.project_root),
            "backend": self.backend.to_json(),
        }


def default_sessions_dir() -> Path:
    if override := os.environ.get("HOP_SESSIONS_DIR"):
        return Path(override)
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "hop" / "sessions"


def record_session(
    session: ProjectSession,
    *,
    backend: BackendRecord | None = None,
    sessions_dir: Path | None = None,
) -> None:
    target = sessions_dir if sessions_dir is not None else default_sessions_dir()
    target.mkdir(parents=True, exist_ok=True)
    state = SessionState(
        name=session.session_name,
        project_root=session.project_root,
        backend=backend if backend is not None else HostBackendRecord(),
    )
    (target / f"{session.session_name}.json").write_text(json.dumps(state.to_json()))


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
        backend_record = _decode_backend_record(payload.get("backend"))
        sessions[name] = SessionState(
            name=name,
            project_root=Path(project_root),
            backend=backend_record,
        )
    return sessions


def _decode_backend_record(raw: object) -> BackendRecord:
    if isinstance(raw, dict):
        record = cast(dict[str, Any], raw)
        kind = record.get("type")
        if kind == "command":
            backend_name = record.get("name")
            shell = record.get("shell")
            editor = record.get("editor")
            if isinstance(backend_name, str) and isinstance(shell, list) and isinstance(editor, list):
                return CommandBackendRecord(
                    name=backend_name,
                    shell=_str_tuple(cast(list[Any], shell)),
                    editor=_str_tuple(cast(list[Any], editor)),
                    prepare=_optional_str_tuple(record.get("prepare")),
                    teardown=_optional_str_tuple(record.get("teardown")),
                    workspace_command=_optional_str_tuple(record.get("workspace_command")),
                    workspace_path=(
                        str(record["workspace_path"]) if isinstance(record.get("workspace_path"), str) else None
                    ),
                )
    return HostBackendRecord()


def _str_tuple(value: list[Any]) -> tuple[str, ...]:
    return tuple(str(part) for part in value)


def _optional_str_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    return _str_tuple(cast(list[Any], value))
