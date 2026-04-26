from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hop.session import ProjectSession, resolve_project_session

DEFAULT_RUN_ROLE = "shell"


@dataclass(frozen=True, slots=True)
class RunDispatch:
    run_id: str
    session: ProjectSession
    window_id: int


class RunSwayAdapter(Protocol):
    def switch_to_workspace(self, workspace_name: str) -> None: ...


class RunKittyAdapter(Protocol):
    def run_in_terminal(
        self,
        session: ProjectSession,
        *,
        role: str,
        command: str,
    ) -> int: ...


def default_runs_dir() -> Path:
    if override := os.environ.get("HOP_RUNS_DIR"):
        return Path(override)
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "hop" / "runs"


def run_command(
    cwd: Path | str,
    *,
    sway: RunSwayAdapter,
    terminals: RunKittyAdapter,
    command: str,
    role: str = DEFAULT_RUN_ROLE,
    runs_dir: Path | None = None,
) -> RunDispatch:
    session = resolve_project_session(cwd)
    sway.switch_to_workspace(session.workspace_name)
    window_id = terminals.run_in_terminal(session, role=role, command=command)

    run_id = uuid.uuid4().hex
    target_dir = runs_dir if runs_dir is not None else default_runs_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "window_id": window_id,
        "session": session.session_name,
        "role": role,
        "dispatched_at": time.time(),
    }
    (target_dir / f"{run_id}.json").write_text(json.dumps(state))

    return RunDispatch(run_id=run_id, session=session, window_id=window_id)
