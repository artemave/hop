from __future__ import annotations

import shutil
from pathlib import Path

from hop.commands.path import resolve_asset_path

# Bundled skill asset and the per-agent skill directories it installs into,
# relative to the target home. Claude Code reads ``~/.claude/skills``; Codex
# reads ``~/.agents/skills``. The body is identical for both.
_SKILL_ASSET = "agents/hop-run/SKILL.md"
_SKILL_DIRS = (
    Path(".claude") / "skills" / "hop-run",
    Path(".agents") / "skills" / "hop-run",
)


def install_agent_skill(*, home: Path | None = None) -> list[Path]:
    """Copy the bundled ``hop-run`` skill into the agent skill directories
    under ``home`` (default: the running user's home).

    Resolving ``Path.home()`` of the calling process is what makes this
    backend-agnostic: run it on the host and it installs into the host home;
    run it as a container/remote ``prepare`` step (``<prefix> hop install
    --agents``) and it installs into that backend's home. Returns the written
    ``SKILL.md`` paths.
    """

    base = home if home is not None else Path.home()
    source = resolve_asset_path(_SKILL_ASSET)

    written: list[Path] = []
    for skill_dir in _SKILL_DIRS:
        target = base / skill_dir / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written.append(target)
    return written
