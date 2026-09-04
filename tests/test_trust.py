from __future__ import annotations

import stat
from pathlib import Path

import pytest

from hop.trust import (
    TrustEntry,
    default_trust_dir,
    is_trusted,
    list_entries,
    record,
    revoke,
)


def test_default_trust_dir_uses_xdg_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_trust_dir() == tmp_path / "hop" / "trusted"


def test_default_trust_dir_falls_back_to_home_local_share(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_trust_dir() == tmp_path / ".local" / "share" / "hop" / "trusted"


def test_is_trusted_false_when_never_recorded(tmp_path: Path) -> None:
    assert is_trusted("/proj/.hop.toml", "content", trust_dir=tmp_path) is False


def test_record_then_is_trusted(tmp_path: Path) -> None:
    record("/proj/.hop.toml", "activate = true", trust_dir=tmp_path)
    assert is_trusted("/proj/.hop.toml", "activate = true", trust_dir=tmp_path) is True


def test_is_trusted_false_after_content_changes(tmp_path: Path) -> None:
    record("/proj/.hop.toml", "activate = true", trust_dir=tmp_path)
    assert is_trusted("/proj/.hop.toml", "activate = false", trust_dir=tmp_path) is False


def test_record_writes_owner_only_permissions(tmp_path: Path) -> None:
    record("/proj/.hop.toml", "content", trust_dir=tmp_path)
    entries = list(tmp_path.iterdir())
    assert len(entries) == 1
    mode = stat.S_IMODE(entries[0].stat().st_mode)
    assert mode == 0o600


def test_record_stores_config_path_on_second_line(tmp_path: Path) -> None:
    record("/proj/.hop.toml", "content", trust_dir=tmp_path)
    (entry_path,) = tmp_path.iterdir()
    lines = entry_path.read_text().splitlines()
    assert lines[1] == "/proj/.hop.toml"


def test_revoke_removes_a_recorded_entry(tmp_path: Path) -> None:
    record("/proj/.hop.toml", "content", trust_dir=tmp_path)
    revoke("/proj/.hop.toml", trust_dir=tmp_path)
    assert is_trusted("/proj/.hop.toml", "content", trust_dir=tmp_path) is False


def test_revoke_is_a_noop_when_nothing_was_recorded(tmp_path: Path) -> None:
    revoke("/proj/.hop.toml", trust_dir=tmp_path)


def test_list_entries_empty_when_trust_dir_does_not_exist(tmp_path: Path) -> None:
    assert list_entries(trust_dir=tmp_path / "does-not-exist") == ()


def test_list_entries_returns_recorded_entries(tmp_path: Path) -> None:
    record("/proj-a/.hop.toml", "a", trust_dir=tmp_path)
    record("/proj-b/.hop.toml", "b", trust_dir=tmp_path)
    entries = list_entries(trust_dir=tmp_path)
    assert {entry.config_path for entry in entries} == {"/proj-a/.hop.toml", "/proj-b/.hop.toml"}
    assert all(isinstance(entry, TrustEntry) for entry in entries)


def test_list_entries_skips_malformed_files(tmp_path: Path) -> None:
    record("/proj/.hop.toml", "content", trust_dir=tmp_path)
    (tmp_path / "garbage").write_text("not-a-real-entry")
    entries = list_entries(trust_dir=tmp_path)
    assert [entry.config_path for entry in entries] == ["/proj/.hop.toml"]
