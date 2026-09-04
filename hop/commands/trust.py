from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

from hop import trust
from hop.backends import CommandRunner, SshTransport, default_runner
from hop.config import PROJECT_CONFIG_FILE
from hop.errors import HopError
from hop.session import ProjectSession, remote_session_from_env, resolve_project_session
from hop.state import default_sessions_dir, load_sessions, record_session

TrustMode = Literal["trust", "list", "revoke"]


def trust_command(
    cwd: Path | str,
    *,
    mode: TrustMode,
    path: str | None = None,
    sessions_dir: Path | None = None,
    runner: CommandRunner = default_runner,
) -> str:
    if mode == "list":
        return _list_trusted()

    session = remote_session_from_env() or resolve_project_session(cwd)

    if mode == "revoke":
        config_path = path if path is not None else _config_path_for(session)
        trust.revoke(config_path)
        return f"revoked trust for {config_path}"

    config_path = _config_path_for(session)
    content = _fetch_content(session, runner=runner)
    trust.record(config_path, content)

    target = sessions_dir if sessions_dir is not None else default_sessions_dir()
    live = load_sessions(sessions_dir=target).get(session.session_name)
    if live is None:
        return f"trusted {config_path}"
    record_session(session, backend=replace(live.backend, project_config_toml=content), sessions_dir=target)
    return f"trusted {config_path} and refreshed live session {session.session_name!r}"


def _config_path_for(session: ProjectSession) -> str:
    if session.host is None:
        return str(Path(session.session_root) / PROJECT_CONFIG_FILE)
    return str(Path(f"{session.host}:{session.session_root}") / PROJECT_CONFIG_FILE)


def _fetch_content(session: ProjectSession, *, runner: CommandRunner) -> str:
    if session.host is None:
        path = Path(session.session_root) / PROJECT_CONFIG_FILE
        if not path.is_file():
            msg = f"{path} does not exist — nothing to trust."
            raise HopError(msg)
        return path.read_text()

    transport = SshTransport(session.host, str(session.session_root), interactive=False)
    argv = transport(f"cat {PROJECT_CONFIG_FILE}")
    result = runner(argv, Path.home())
    if result.returncode != 0:
        msg = f"{_config_path_for(session)} does not exist — nothing to trust."
        raise HopError(msg)
    return result.stdout


def _list_trusted() -> str:
    entries = trust.list_entries()
    if not entries:
        return "no trusted .hop.toml files"
    lines = [f"{_entry_status(entry):8} {entry.config_path}" for entry in entries]
    return "\n".join(lines)


def _entry_status(entry: trust.TrustEntry) -> str:
    if not entry.config_path.startswith("/"):
        return "remote"
    path = Path(entry.config_path)
    if not path.is_file():
        return "missing"
    return "ok" if trust.is_trusted(entry.config_path, path.read_text()) else "drifted"
