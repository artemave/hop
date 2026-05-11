"""Reconcile `~/.local/share/vicinae/scripts/hop-*` to the focused session.

`hopd` calls `regenerate(...)` on startup and on every Sway `workspace`
event. The result: per-window vicinae script entries (one per declared
role), per-other-session switch entries, and a session-kill entry — all
gated on the currently focused workspace.

Hop owns the `hop-*` filename namespace in the scripts directory; any
unrelated files are left untouched.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from hop.commands.session import SESSION_WORKSPACE_PREFIX, SessionListing
from hop.config import BROWSER_ROLE, EDITOR_ROLE
from hop.layouts import WindowSpec
from hop.session import ProjectSession

SCRIPT_FILENAME_PREFIX = "hop-"
WINDOW_FILENAME_PREFIX = "hop-window-"
SWITCH_FILENAME_PREFIX = "hop-switch-"
KILL_FILENAME = "hop-kill"
CREATE_FILENAME = "hop-create"
# Leading-underscore suffix keeps this entry from colliding with sanitized
# session names (which derive from path basenames and don't start with `_`).
DAEMON_DOWN_FILENAME = "hop-_daemon-down"
_DAEMON_DOWN_DESCRIPTION_MAX = 200


class VicinaeSwayAdapter(Protocol):
    def get_focused_workspace(self) -> str: ...


SessionsLoader = Callable[[], Sequence[SessionListing]]
WindowsResolver = Callable[[ProjectSession], Sequence[WindowSpec]]


@dataclass(frozen=True, slots=True)
class GeneratedScript:
    filename: str
    content: str


def default_scripts_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "vicinae" / "scripts"


def compute_target_scripts(
    focused_workspace: str,
    sessions: Sequence[SessionListing],
    *,
    windows_for: WindowsResolver,
) -> tuple[GeneratedScript, ...]:
    """Compute the desired vicinae script set for the current state.

    On a `p:<session>` workspace: per-window scripts for every declared
    role, `hop-kill`, plus `hop-switch-<other-session>` for every other
    live session. Off any `p:*` workspace: only `hop-switch-<session>`
    for every live session. `hop-create` is always emitted — it falls
    through to a second vicinae dmenu over directories under `$HOME`
    and either creates a new session or attaches to an existing one.
    """

    scripts: list[GeneratedScript] = []
    used_filenames: set[str] = set()

    focused_session = _focused_session(focused_workspace, sessions)

    if focused_session is not None and focused_session.project_root is not None:
        project_session = ProjectSession(
            project_root=focused_session.project_root,
            session_name=focused_session.name,
            workspace_name=focused_session.workspace,
        )
        windows = windows_for(project_session)
        for window in windows:
            scripts.append(_window_script(window, project_session, used=used_filenames))
        scripts.append(_kill_script(project_session, used=used_filenames))

    other_sessions: Iterable[SessionListing]
    if focused_session is not None:
        other_sessions = (s for s in sessions if s.name != focused_session.name)
    else:
        other_sessions = sessions
    for session in other_sessions:
        scripts.append(_switch_script(session, used=used_filenames))

    scripts.append(_create_script())

    return tuple(scripts)


def reconcile(
    target: Sequence[GeneratedScript],
    *,
    scripts_dir: Path,
) -> None:
    """Apply ``target`` to ``scripts_dir`` atomically.

    Writes new/changed `hop-*` files, deletes any `hop-*` not in the target.
    Non-`hop-*` files are left untouched. Creates ``scripts_dir`` if it
    does not exist.
    """

    scripts_dir.mkdir(parents=True, exist_ok=True)

    target_by_filename = {script.filename: script for script in target}

    for existing in scripts_dir.iterdir():
        if not existing.name.startswith(SCRIPT_FILENAME_PREFIX):
            continue
        if existing.name not in target_by_filename:
            existing.unlink()

    for script in target:
        path = scripts_dir / script.filename
        if path.exists() and path.read_text() == script.content:
            continue
        _atomic_write(path, script.content)


def regenerate(
    *,
    sway: VicinaeSwayAdapter,
    sessions_loader: SessionsLoader,
    scripts_dir: Path,
    windows_for: WindowsResolver,
) -> None:
    target = compute_target_scripts(
        sway.get_focused_workspace(),
        sessions_loader(),
        windows_for=windows_for,
    )
    reconcile(target, scripts_dir=scripts_dir)


def _focused_session(focused_workspace: str, sessions: Sequence[SessionListing]) -> SessionListing | None:
    if not focused_workspace.startswith(SESSION_WORKSPACE_PREFIX):
        return None
    for session in sessions:
        if session.workspace == focused_workspace:
            return session
    return None


def _window_script(
    window: WindowSpec,
    session: ProjectSession,
    *,
    used: set[str],
) -> GeneratedScript:
    role = window.role
    filename = _unique(WINDOW_FILENAME_PREFIX + _sanitize(role), used=used)
    title = f"Hop {role}"
    description = f"Open or focus the {role!r} window in the {session.session_name!r} hop session."
    if role == EDITOR_ROLE:
        body = "exec hop edit\n"
    elif role == BROWSER_ROLE:
        body = "exec hop browser\n"
    else:
        body = f"exec hop term --role {shlex.quote(role)}\n"
    content = _render(
        title=title,
        description=description,
        # Per-window scripts only exist while the session is focused, so
        # the right-side label is always the focused session's name.
        # That gives kill / window entries a "which session?" answer at
        # a glance — vital for `Hop kill`, useful for everything else.
        package_name=session.session_name,
        project_root=session.project_root,
        body=body,
    )
    return GeneratedScript(filename=filename, content=content)


def _kill_script(session: ProjectSession, *, used: set[str]) -> GeneratedScript:
    filename = _unique(KILL_FILENAME, used=used)
    title = "Hop kill"
    description = f"Kill the {session.session_name!r} hop session."
    content = _render_kill(
        title=title,
        description=description,
        package_name=session.session_name,
        project_root=session.project_root,
    )
    return GeneratedScript(filename=filename, content=content)


def _create_script() -> GeneratedScript:
    # The candidate set (every directory under $HOME, modulo dot-dirs and
    # well-known build noise) is far too big for static enumeration as
    # vicinae root entries, so this script falls through to a second
    # `vicinae dmenu` search to fuzzy-pick the target. `cd "$HOME/$chosen"
    # && exec hop` then either creates a fresh session for that path or
    # attaches to an existing one — `hop` already handles both cases.
    #
    # We emit each candidate as a path relative to $HOME (`find -printf '%P'`)
    # rather than absolute. Vicinae's dmenu auto-detects absolute paths and
    # renders only the basename in the list view, which makes nested dirs
    # that share a basename with their parent (e.g. a Python package layout
    # `tmux_super_fingers/tmux_super_fingers`) look like duplicates. Relative
    # strings sidestep that detection so distinct paths render distinctly.
    return GeneratedScript(
        filename=CREATE_FILENAME,
        content=(
            "#!/usr/bin/env bash\n"
            "# @vicinae.schemaVersion 1\n"
            "# @vicinae.title Hop create session\n"
            "# @vicinae.description Create or attach to a hop session for any directory under home.\n"
            "# @vicinae.packageName \n"
            "# @vicinae.mode silent\n"
            "\n"
            "set -euo pipefail\n"
            "\n"
            'candidates=$(find "$HOME" -mindepth 1 -maxdepth 3 \\\n'
            "    \\( -name '.*' -o -name 'node_modules' -o -name 'target' "
            "-o -name 'dist' -o -name '__pycache__' \\) -prune \\\n"
            "    -o -type d -printf '%P\\n')\n"
            "\n"
            'if [ -z "$candidates" ]; then\n'
            "    exit 0\n"
            "fi\n"
            "\n"
            'if ! chosen=$(printf \'%s\\n\' "$candidates" | vicinae dmenu --placeholder "Pick a project"); then\n'
            "    exit 0\n"
            "fi\n"
            "\n"
            'if [ -z "$chosen" ]; then\n'
            "    exit 0\n"
            "fi\n"
            "\n"
            'cd "$HOME/$chosen"\n'
            "exec hop\n"
        ),
    )


def _switch_script(session: SessionListing, *, used: set[str]) -> GeneratedScript:
    filename = _unique(SWITCH_FILENAME_PREFIX + _sanitize(session.name), used=used)
    title = f"Hop switch to {session.name}"
    description = f"Switch focus to the {session.name!r} hop session's workspace."
    body = f"exec hop switch {shlex.quote(session.name)}\n"
    # Switch entries already name the target session in the title, so
    # the right-side label would be redundant. Empty packageName hides
    # the launcher's default ("scripts" — derived from the dir name).
    content = _render_no_cd(
        title=title,
        description=description,
        package_name="",
        body=body,
    )
    return GeneratedScript(filename=filename, content=content)


def _render(*, title: str, description: str, package_name: str, project_root: Path, body: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "# @vicinae.schemaVersion 1\n"
        f"# @vicinae.title {title}\n"
        f"# @vicinae.description {description}\n"
        f"# @vicinae.packageName {package_name}\n"
        "# @vicinae.mode silent\n"
        "\n"
        "set -euo pipefail\n"
        f"cd {shlex.quote(str(project_root))}\n"
        f"{body}"
    )


def _render_no_cd(*, title: str, description: str, package_name: str, body: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "# @vicinae.schemaVersion 1\n"
        f"# @vicinae.title {title}\n"
        f"# @vicinae.description {description}\n"
        f"# @vicinae.packageName {package_name}\n"
        "# @vicinae.mode silent\n"
        "\n"
        "set -euo pipefail\n"
        f"{body}"
    )


def _render_kill(*, title: str, description: str, package_name: str, project_root: Path) -> str:
    # `hop kill` from inside a vicinae action gets SIGTERMed when vicinae
    # closes the UI before teardown completes (devcontainer left in
    # `stopping`, etc.). `setsid -f` detaches into a fresh session so the
    # signal doesn't reach the cleanup path. `vicinae close || true`
    # mirrors the original hop-kill-session script: in `silent` mode the
    # UI may auto-close before the detached script runs, in which case
    # `vicinae close` exits non-zero and `set -e` would abort before
    # teardown — guarding it with `|| true` keeps the cleanup path alive.
    return (
        "#!/usr/bin/env bash\n"
        "# @vicinae.schemaVersion 1\n"
        f"# @vicinae.title {title}\n"
        f"# @vicinae.description {description}\n"
        f"# @vicinae.packageName {package_name}\n"
        "# @vicinae.mode silent\n"
        "\n"
        "exec setsid -f bash -c '\n"
        "    set -e\n"
        "    vicinae close || true\n"
        f"    cd {shlex.quote(str(project_root))}\n"
        "    exec hop kill\n"
        "'\n"
    )


def write_daemon_down_script(scripts_dir: Path, *, error: BaseException) -> None:
    """Replace the hop-* script set with a single "daemon stopped" entry.

    Called from ``hopd``'s exception handler so the user sees a clear
    "click to restart" entry in vicinae instead of a stale hop-* set
    that silently reflects whatever state the daemon last computed.

    Picking the entry runs ``setsid -f hopd`` to detach a fresh daemon
    process — works whether or not the user has systemd-style supervision
    (systemd will adopt the child if the unit is alive).
    """

    scripts_dir.mkdir(parents=True, exist_ok=True)

    for existing in scripts_dir.iterdir():
        if existing.name.startswith(SCRIPT_FILENAME_PREFIX):
            existing.unlink()

    description = _describe_daemon_down_error(error)
    content = (
        "#!/usr/bin/env bash\n"
        "# @vicinae.schemaVersion 1\n"
        "# @vicinae.title Hop daemon stopped — restart\n"
        f"# @vicinae.description {description}\n"
        "# @vicinae.packageName \n"
        "# @vicinae.mode silent\n"
        "\n"
        "exec setsid -f hopd </dev/null >/dev/null 2>&1\n"
    )
    _atomic_write(scripts_dir / DAEMON_DOWN_FILENAME, content)


def _describe_daemon_down_error(error: BaseException) -> str:
    """One-line summary suitable for the vicinae description header.

    Collapses whitespace so multi-line exception messages don't break
    the line-oriented header parser, and truncates at a fixed budget so
    the launcher's description column stays readable.
    """

    message = f"{type(error).__name__}: {error}".strip() or type(error).__name__
    message = " ".join(message.split())
    if len(message) > _DAEMON_DOWN_DESCRIPTION_MAX:
        message = message[: _DAEMON_DOWN_DESCRIPTION_MAX - 3] + "..."
    return message


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    os.chmod(tmp_name, 0o755)
    os.replace(tmp_name, str(path))


_FILENAME_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _sanitize(name: str) -> str:
    return "".join(ch if ch in _FILENAME_ALLOWED else "_" for ch in name)


def _unique(filename: str, *, used: set[str]) -> str:
    if filename not in used:
        used.add(filename)
        return filename
    n = 2
    while f"{filename}-{n}" in used:
        n += 1
    deduped = f"{filename}-{n}"
    used.add(deduped)
    return deduped
