from __future__ import annotations

from pathlib import Path

import hop


def resolve_asset_path(name: str) -> Path:
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        msg = f"invalid hop path name: {name!r}"
        raise ValueError(msg)
    base = Path(hop.__file__).parent.joinpath(*parts)
    if base.is_file():
        return base
    main = base / "main.py"
    if main.is_file():
        return main
    msg = f"unknown hop path: {name!r}"
    raise ValueError(msg)
