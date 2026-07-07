"""Debug log file for backend command runs and kitty bootstrap output.

Opt-in via the top-level ``debug_log`` field in ``~/.config/hop/config.toml``
(or a project ``.hop.toml``):

    debug_log = true                # -> $XDG_RUNTIME_DIR/hop/debug.log
    debug_log = "/path/to/log"      # -> custom path
    # absent or false -> disabled (default)

When enabled, hop appends a record of every backend lifecycle command
(prepare/teardown/workspace/translate) and the kitty bootstrap launcher's
argv + stdio to the configured path. Plain append, no rotation — the file
is intended to be inspected by hand and truncated when stale.
"""

from __future__ import annotations

import datetime
import os
import shlex
import subprocess
import threading
from pathlib import Path
from tempfile import gettempdir
from typing import Sequence

_path: Path | None = None
_lock = threading.Lock()

# Env var the vicinae launcher scripts set so their `hop` invocations are
# tagged as coming from vicinae rather than a shell. Absent (a plain CLI run,
# a sway keybinding) means the invocation defaults to ``cli``.
SOURCE_ENV_VAR = "HOP_SOURCE"


def default_log_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or gettempdir()
    return Path(base) / "hop" / "debug.log"


def configure(setting: bool | str | None) -> None:
    """Apply the parsed ``debug_log`` setting.

    ``True`` enables the default path; a non-empty string is taken as a
    custom path. ``None`` / ``False`` disables logging. Idempotent: a
    second call replaces the current state, which keeps tests sane.
    """

    global _path
    if not setting:
        _path = None
        return
    target = Path(setting).expanduser() if isinstance(setting, str) else default_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _path = target


def is_enabled() -> bool:
    return _path is not None


def log_path() -> Path | None:
    return _path


def log(message: str) -> None:
    if _path is None:
        return
    _write(f"{_timestamp()} {message}\n")


def log_invocation(argv: Sequence[str]) -> None:
    """Record a ``hop`` CLI invocation and where it came from.

    Answers "why did my session vanish?" — an accidental ``kill`` where a
    ``switch`` was meant shows up here as a distinct line, tagged with its
    source (``HOP_SOURCE`` env; vicinae launcher scripts set ``vicinae``,
    everything else defaults to ``cli``).
    """
    if _path is None:
        return
    source = os.environ.get(SOURCE_ENV_VAR) or "cli"
    rendered = " ".join(shlex.quote(a) for a in ("hop", *argv))
    _write(f"{_timestamp()} invoke [{source}]: {rendered}\n")


def log_command(
    args: Sequence[str],
    cwd: Path | str | None,
    result: subprocess.CompletedProcess[str],
) -> None:
    if _path is None:
        return
    rendered = " ".join(shlex.quote(a) for a in args)
    lines = [f"{_timestamp()} command: {rendered}"]
    if cwd is not None:
        lines.append(f"  cwd: {cwd}")
    lines.append(f"  exit: {result.returncode}")
    if result.stdout:
        lines.append(f"  stdout: {result.stdout.rstrip()}")
    if result.stderr:
        lines.append(f"  stderr: {result.stderr.rstrip()}")
    _write("\n".join(lines) + "\n")


def _timestamp() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def _write(text: str) -> None:
    assert _path is not None
    with _lock, _path.open("a") as fh:
        fh.write(text)
