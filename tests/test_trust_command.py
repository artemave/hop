from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from hop import trust
from hop.commands.trust import trust_command
from hop.errors import HopError
from hop.session import ProjectSession
from hop.state import CommandBackendRecord, load_sessions, record_session


def _session(root: Path) -> ProjectSession:
    return ProjectSession(session_root=root, session_name=root.name, workspace_name=f"p:{root.name}")


def test_trust_raises_when_no_hop_toml_exists(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    with pytest.raises(HopError, match="does not exist"):
        trust_command(session_root, mode="trust")


def test_trust_records_ledger_entry_for_local_config(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()
    (session_root / ".hop.toml").write_text('activate = "true"\n')

    message = trust_command(session_root, mode="trust")

    config_path = str(session_root / ".hop.toml")
    assert config_path in message
    assert "refreshed" not in message
    assert trust.is_trusted(config_path, 'activate = "true"\n') is True


def test_trust_refreshes_a_live_session_record(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()
    (session_root / ".hop.toml").write_text('activate = "true"\n')
    sessions_dir = tmp_path / "sessions"
    session = _session(session_root)
    record_session(
        session,
        backend=CommandBackendRecord(name="host", interactive_prefix="", noninteractive_prefix=""),
        sessions_dir=sessions_dir,
    )

    message = trust_command(session_root, mode="trust", sessions_dir=sessions_dir)

    assert "refreshed live session" in message
    assert session.session_name in message
    reloaded = load_sessions(sessions_dir=sessions_dir)[session.session_name]
    assert reloaded.backend.project_config_toml == 'activate = "true"\n'


def test_trust_does_not_touch_session_state_when_none_is_live(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()
    (session_root / ".hop.toml").write_text('activate = "true"\n')
    sessions_dir = tmp_path / "sessions"

    trust_command(session_root, mode="trust", sessions_dir=sessions_dir)

    assert load_sessions(sessions_dir=sessions_dir) == {}


def test_trust_fetches_and_records_remote_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOP_REMOTE_HOST", "devbox")
    monkeypatch.setenv("HOP_REMOTE_CWD", "/home/u/proj")

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        assert args[0] == "ssh"
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout='activate = "true"\n', stderr="")

    message = trust_command(tmp_path, mode="trust", runner=runner)

    config_path = "devbox:/home/u/proj/.hop.toml"
    assert config_path in message
    assert trust.is_trusted(config_path, 'activate = "true"\n') is True


def test_trust_raises_when_remote_fetch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOP_REMOTE_HOST", "devbox")
    monkeypatch.setenv("HOP_REMOTE_CWD", "/home/u/proj")

    def runner(args: Sequence[str], cwd: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(args), returncode=1, stdout="", stderr="no such file")

    with pytest.raises(HopError, match="does not exist"):
        trust_command(tmp_path, mode="trust", runner=runner)


def test_revoke_without_path_uses_cwd_derived_config_path(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()
    config_path = str(session_root / ".hop.toml")
    trust.record(config_path, "content")

    message = trust_command(session_root, mode="revoke")

    assert config_path in message
    assert trust.is_trusted(config_path, "content") is False


def test_revoke_with_explicit_path_ignores_cwd(tmp_path: Path) -> None:
    other_path = "/elsewhere/.hop.toml"
    trust.record(other_path, "content")

    trust_command(tmp_path, mode="revoke", path=other_path)

    assert trust.is_trusted(other_path, "content") is False


def test_revoke_works_even_when_hop_toml_no_longer_exists(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()
    config_path = str(session_root / ".hop.toml")
    trust.record(config_path, "content")

    trust_command(session_root, mode="revoke")

    assert trust.is_trusted(config_path, "content") is False


def test_list_reports_no_trusted_files_when_ledger_is_empty(tmp_path: Path) -> None:
    assert trust_command(tmp_path, mode="list") == "no trusted .hop.toml files"


def test_list_reports_ok_for_a_matching_local_entry(tmp_path: Path) -> None:
    config_file = tmp_path / ".hop.toml"
    config_file.write_text('activate = "true"\n')
    trust.record(str(config_file), 'activate = "true"\n')

    assert f"ok       {config_file}" in trust_command(tmp_path, mode="list")


def test_list_reports_drifted_for_an_edited_local_entry(tmp_path: Path) -> None:
    config_file = tmp_path / ".hop.toml"
    config_file.write_text('activate = "true"\n')
    trust.record(str(config_file), 'activate = "true"\n')
    config_file.write_text('activate = "false"\n')

    assert f"drifted  {config_file}" in trust_command(tmp_path, mode="list")


def test_list_reports_missing_for_a_deleted_local_entry(tmp_path: Path) -> None:
    config_file = tmp_path / ".hop.toml"
    config_file.write_text('activate = "true"\n')
    trust.record(str(config_file), 'activate = "true"\n')
    config_file.unlink()

    assert f"missing  {config_file}" in trust_command(tmp_path, mode="list")


def test_list_reports_remote_for_a_host_prefixed_entry(tmp_path: Path) -> None:
    trust.record("devbox:/home/u/proj/.hop.toml", "content")

    assert "remote   devbox:/home/u/proj/.hop.toml" in trust_command(tmp_path, mode="list")
