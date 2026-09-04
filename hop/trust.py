from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


def default_trust_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "hop" / "trusted"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _entry_path(config_path: str, *, trust_dir: Path | None = None) -> Path:
    directory = trust_dir if trust_dir is not None else default_trust_dir()
    key = hashlib.sha256(config_path.encode()).hexdigest()
    return directory / key


def is_trusted(config_path: str, content: str, *, trust_dir: Path | None = None) -> bool:
    entry = _entry_path(config_path, trust_dir=trust_dir)
    try:
        recorded = entry.read_text().splitlines()[0]
    except OSError:
        return False
    return recorded == _content_hash(content)


def record(config_path: str, content: str, *, trust_dir: Path | None = None) -> None:
    directory = trust_dir if trust_dir is not None else default_trust_dir()
    directory.mkdir(parents=True, exist_ok=True)
    entry = _entry_path(config_path, trust_dir=directory)
    entry.write_text(f"{_content_hash(content)}\n{config_path}\n")
    entry.chmod(0o600)


def revoke(config_path: str, *, trust_dir: Path | None = None) -> None:
    entry = _entry_path(config_path, trust_dir=trust_dir)
    entry.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class TrustEntry:
    config_path: str
    content_hash: str


def list_entries(*, trust_dir: Path | None = None) -> tuple[TrustEntry, ...]:
    directory = trust_dir if trust_dir is not None else default_trust_dir()
    if not directory.is_dir():
        return ()
    entries: list[TrustEntry] = []
    for path in sorted(directory.iterdir()):
        lines = path.read_text().splitlines()
        if len(lines) < 2:
            continue
        entries.append(TrustEntry(config_path=lines[1], content_hash=lines[0]))
    return tuple(entries)
