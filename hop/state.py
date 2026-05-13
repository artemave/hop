from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from hop.session import ProjectSession


@dataclass(frozen=True, slots=True)
class CommandBackendRecord:
    """Persisted session backend chosen at session creation.

    Lifecycle commands (``prepare`` / ``teardown`` / translate helpers) and
    the two prefixes are shell snippets (run via ``sh -c`` after placeholder
    substitution). Per-role launch commands are NOT persisted — layouts and
    top-level windows live in the active config and re-resolve on every
    session entry, so adding a layout or `bin/rails` to a project after the
    session was first created picks up on the next `hop kill` + `hop` cycle.

    The implicit ``host`` backend hop ships with persists as a regular record
    with empty-string prefixes — there's no separate "host record" type.
    """

    name: str
    interactive_prefix: str
    noninteractive_prefix: str
    prepare: str | None = None
    teardown: str | None = None
    port_translate_command: str | None = None
    host_translate_command: str | None = None
    # Cached result of ``<noninteractive_prefix> pwd`` captured at bootstrap.
    # Used as a fallback base cwd in ``hop.focused.paths_exist`` when the
    # focused window's OSC-7-driven ``cwd_of_child`` is unset.
    workspace_path: str | None = None
    type: str = "command"

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": "command",
            "name": self.name,
            "interactive_prefix": self.interactive_prefix,
            "noninteractive_prefix": self.noninteractive_prefix,
        }
        if self.prepare is not None:
            payload["prepare"] = self.prepare
        if self.teardown is not None:
            payload["teardown"] = self.teardown
        if self.port_translate_command is not None:
            payload["port_translate_command"] = self.port_translate_command
        if self.host_translate_command is not None:
            payload["host_translate_command"] = self.host_translate_command
        if self.workspace_path is not None:
            payload["workspace_path"] = self.workspace_path
        return payload


# Single backend record type now. Kept as an alias so existing imports
# (``from hop.state import BackendRecord``) keep working without churn.
BackendRecord = CommandBackendRecord


# Hop's built-in ``host`` record — used as the default for unbootstrapped
# sessions so adapters always have a concrete backend to call into without
# any None-checks. Matches the built-in BackendConfig in hop/config.py.
_HOST_RECORD = CommandBackendRecord(
    name="host",
    interactive_prefix="",
    noninteractive_prefix="",
)


@dataclass(frozen=True, slots=True)
class SessionState:
    name: str
    project_root: Path
    backend: BackendRecord = field(default_factory=lambda: _HOST_RECORD)

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
        backend=backend if backend is not None else _HOST_RECORD,
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
        payload = json.loads(path.read_text())
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
            interactive_prefix = _optional_str(record.get("interactive_prefix"))
            noninteractive_prefix = _optional_str(record.get("noninteractive_prefix"))
            if isinstance(backend_name, str) and interactive_prefix is not None and noninteractive_prefix is not None:
                return CommandBackendRecord(
                    name=backend_name,
                    interactive_prefix=interactive_prefix,
                    noninteractive_prefix=noninteractive_prefix,
                    prepare=_optional_str(record.get("prepare")),
                    teardown=_optional_str(record.get("teardown")),
                    port_translate_command=_optional_str(record.get("port_translate_command")),
                    host_translate_command=_optional_str(record.get("host_translate_command")),
                    workspace_path=_optional_str(record.get("workspace_path")),
                )
    # Anything we can't decode (legacy ``{"type": "host"}`` records, malformed
    # payloads, the ``workspace_command``/``workspace_path``-era shape) falls
    # back to the host record so adapters keep working.
    return _HOST_RECORD


def _optional_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None
