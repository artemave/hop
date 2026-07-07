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
from hop.config import BROWSER_ROLE
from hop.debug import SOURCE_ENV_VAR
from hop.layouts import WindowSpec
from hop.session import ProjectSession

# Every generated script exports this so the `hop` invocations it dispatches
# are tagged as coming from vicinae in the debug log (see debug.log_invocation).
_SOURCE_EXPORT = f"export {SOURCE_ENV_VAR}=vicinae\n"

SCRIPT_FILENAME_PREFIX = "hop-"
WINDOW_FILENAME_PREFIX = "hop-window-"
SWITCH_FILENAME_PREFIX = "hop-switch-"
KILL_FILENAME = "hop-kill"
CREATE_FILENAME = "hop-create"
MOVE_FILENAME = "hop-move"
# Leading-underscore suffix keeps this entry from colliding with sanitized
# session names (which derive from path basenames and don't start with `_`).
DAEMON_DOWN_FILENAME = "hop-_daemon-down"
_DAEMON_DOWN_DESCRIPTION_MAX = 200

# Vicinae renders `@vicinae.icon` paths absolutely; `__file__` resolves
# next to the shipped `hop/assets/` directory in both editable and wheel
# installs, so we don't need importlib.resources gymnastics.
_ICON_PATH = Path(__file__).parent / "assets" / "hop-mark-64.png"


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
    hop_bin: str,
) -> tuple[GeneratedScript, ...]:
    """Compute the desired vicinae script set for the current state.

    On a `p:<session>` workspace: per-window scripts for every declared
    role, `hop-kill`, plus `hop-switch-<other-session>` for every other
    live session. Off any `p:*` workspace: only `hop-switch-<session>`
    for every live session. `hop-create` and `hop-move` are always
    emitted — both fall through to a `vicinae dmenu` pick over their
    own candidate list.

    Every script invokes the `hop` CLI through ``hop_bin`` (an absolute
    path), never by bare name: vicinae runs these scripts under whatever
    PATH it inherited from Sway, which need not contain hop's install dir.
    """

    scripts: list[GeneratedScript] = []
    used_filenames: set[str] = set()

    focused_session = _focused_session(focused_workspace, sessions)

    if focused_session is not None and focused_session.session_root is not None:
        project_session = ProjectSession(
            session_root=focused_session.session_root,
            session_name=focused_session.name,
            workspace_name=focused_session.workspace,
            host=focused_session.host,
        )
        windows = windows_for(project_session)
        for window in windows:
            scripts.append(_window_script(window, project_session, hop_bin=hop_bin, used=used_filenames))
        scripts.append(_kill_script(project_session, hop_bin=hop_bin, used=used_filenames))

    other_sessions: Iterable[SessionListing]
    if focused_session is not None:
        other_sessions = (s for s in sessions if s.name != focused_session.name)
    else:
        other_sessions = sessions
    for session in other_sessions:
        scripts.append(_switch_script(session, hop_bin=hop_bin, used=used_filenames))

    scripts.append(_create_script(hop_bin=hop_bin))
    scripts.append(_move_script(hop_bin=hop_bin))

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
    hop_bin: str,
) -> None:
    target = compute_target_scripts(
        sway.get_focused_workspace(),
        sessions_loader(),
        windows_for=windows_for,
        hop_bin=hop_bin,
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
    hop_bin: str,
    used: set[str],
) -> GeneratedScript:
    role = window.role
    filename = _unique(WINDOW_FILENAME_PREFIX + _sanitize(role), used=used)
    title = f"Hop {role}"
    description = f"Open or focus the {role!r} window in the {session.session_name!r} hop session."
    hop = shlex.quote(hop_bin)
    # `setsid -f` detaches hop into its own session so vicinae's SIGTERM
    # (sent when its UI closes after the action fires) doesn't kill hop
    # mid-bootstrap. Without it, a slow first-time `prepare` (compose
    # recreate, image pull) leaves the user with "nothing happens" because
    # hop dies before kitty launches.
    if role == BROWSER_ROLE:
        body = f"exec setsid -f {hop} browser\n"
    else:
        body = f"exec setsid -f {hop} term --role {shlex.quote(role)}\n"
    content = _render(
        title=title,
        description=description,
        # Per-window scripts only exist while the session is focused, so
        # the right-side label is always the focused session's name.
        # That gives kill / window entries a "which session?" answer at
        # a glance — vital for `Hop kill`, useful for everything else.
        package_name=session.session_name,
        session_root=session.session_root,
        host=session.host,
        body=body,
    )
    return GeneratedScript(filename=filename, content=content)


def _kill_script(session: ProjectSession, *, hop_bin: str, used: set[str]) -> GeneratedScript:
    filename = _unique(KILL_FILENAME, used=used)
    title = "Hop kill"
    description = f"Kill the {session.session_name!r} hop session."
    content = _render_kill(
        title=title,
        description=description,
        package_name=session.session_name,
        session_root=session.session_root,
        host=session.host,
        hop_bin=hop_bin,
    )
    return GeneratedScript(filename=filename, content=content)


def _create_script(*, hop_bin: str) -> GeneratedScript:
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
    #
    # Directories that contain a `.git` or `.jj` marker are printed and then
    # pruned, so subdirectories of a project root never surface as separate
    # candidates. Vicinae's fuzzy ranker otherwise lifts deeper children
    # (`projects/foo/lib`, `…/log`) above their parent for a query that
    # matches both, hiding the project root the user actually wants.
    return GeneratedScript(
        filename=CREATE_FILENAME,
        content=(
            "#!/usr/bin/env bash\n"
            "# @vicinae.schemaVersion 1\n"
            "# @vicinae.title Hop create session\n"
            "# @vicinae.description Create or attach to a hop session for any directory under home.\n"
            "# @vicinae.packageName \n"
            f"# @vicinae.icon {_ICON_PATH}\n"
            "# @vicinae.mode silent\n"
            "\n"
            "set -euo pipefail\n"
            f"{_SOURCE_EXPORT}"
            "\n"
            'candidates=$(find "$HOME" -mindepth 1 -maxdepth 3 \\\n'
            "    \\( -name '.*' -o -name 'node_modules' -o -name 'target' "
            "-o -name 'dist' -o -name '__pycache__' \\) -prune \\\n"
            "    -o -type d -printf '%P\\n' \\\n"
            "    \\( -exec test -e {}/.git \\; -o -exec test -e {}/.jj \\; \\) -prune)\n"
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
            # `setsid -f` detaches hop so vicinae's SIGTERM (sent when its
            # UI closes after the action fires) doesn't kill it mid-prepare.
            # A slow first-time `prepare` (compose recreate, image pull) is
            # otherwise enough to lose the whole bootstrap.
            f"exec setsid -f {shlex.quote(hop_bin)}\n"
        ),
    )


def _move_script(*, hop_bin: str) -> GeneratedScript:
    # Single entry that delegates the destination pick to `vicinae dmenu`
    # over `hop list` output, mirroring `_create_script`'s pattern. Setting
    # the destination per session would require one entry per session (the
    # `_switch_script` shape) and force the user to disambiguate at picker
    # time; one entry + dmenu keeps the launcher root uncluttered.
    return GeneratedScript(
        filename=MOVE_FILENAME,
        content=(
            "#!/usr/bin/env bash\n"
            "# @vicinae.schemaVersion 1\n"
            "# @vicinae.title Hop move window to session\n"
            "# @vicinae.description Move the focused window to a hop session's workspace.\n"
            "# @vicinae.packageName \n"
            f"# @vicinae.icon {_ICON_PATH}\n"
            "# @vicinae.mode silent\n"
            "\n"
            "set -euo pipefail\n"
            f"{_SOURCE_EXPORT}"
            "\n"
            f"candidates=$({shlex.quote(hop_bin)} list)\n"
            'if [ -z "$candidates" ]; then\n'
            "    exit 0\n"
            "fi\n"
            "\n"
            "if ! chosen=$(printf '%s\\n' \"$candidates\" "
            '| vicinae dmenu --placeholder "Move window to session"); then\n'
            "    exit 0\n"
            "fi\n"
            "\n"
            'if [ -z "$chosen" ]; then\n'
            "    exit 0\n"
            "fi\n"
            "\n"
            # `setsid -f` mirrors the rationale in `_window_script` — vicinae
            # SIGTERMs the action on UI close, and we don't want that to
            # interrupt the IPC sequence.
            f'exec setsid -f {shlex.quote(hop_bin)} move "$chosen"\n'
        ),
    )


def _switch_script(session: SessionListing, *, hop_bin: str, used: set[str]) -> GeneratedScript:
    filename = _unique(SWITCH_FILENAME_PREFIX + _sanitize(session.name), used=used)
    title = f"Hop switch to {session.name}"
    description = f"Switch focus to the {session.name!r} hop session's workspace."
    # `setsid -f` for the same reason `_window_script` uses it — vicinae
    # SIGTERMs the action on UI close.
    body = f"exec setsid -f {shlex.quote(hop_bin)} switch {shlex.quote(session.name)}\n"
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


def _session_setup(session_root: Path, host: str | None, *, indent: str = "") -> str:
    """The line that tells the dispatched ``hop`` which session to act on.

    Local: ``cd`` into the project dir (hop derives identity from cwd). Remote:
    the project dir is on the *remote*, so ``cd`` would fail on the laptop —
    pass identity via ``HOP_REMOTE_*`` instead, and every session command rebuilds
    the remote session from it.
    """

    if host is None:
        return f"{indent}cd {shlex.quote(str(session_root))}\n"
    return f"{indent}export HOP_REMOTE_HOST={shlex.quote(host)} HOP_REMOTE_CWD={shlex.quote(str(session_root))}\n"


def _render(*, title: str, description: str, package_name: str, session_root: Path, host: str | None, body: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "# @vicinae.schemaVersion 1\n"
        f"# @vicinae.title {title}\n"
        f"# @vicinae.description {description}\n"
        f"# @vicinae.packageName {package_name}\n"
        f"# @vicinae.icon {_ICON_PATH}\n"
        "# @vicinae.mode silent\n"
        "\n"
        "set -euo pipefail\n"
        f"{_SOURCE_EXPORT}"
        f"{_session_setup(session_root, host)}"
        f"{body}"
    )


def _render_no_cd(*, title: str, description: str, package_name: str, body: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "# @vicinae.schemaVersion 1\n"
        f"# @vicinae.title {title}\n"
        f"# @vicinae.description {description}\n"
        f"# @vicinae.packageName {package_name}\n"
        f"# @vicinae.icon {_ICON_PATH}\n"
        "# @vicinae.mode silent\n"
        "\n"
        "set -euo pipefail\n"
        f"{_SOURCE_EXPORT}"
        f"{body}"
    )


def _render_kill(
    *, title: str, description: str, package_name: str, session_root: Path, host: str | None, hop_bin: str
) -> str:
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
        f"# @vicinae.icon {_ICON_PATH}\n"
        "# @vicinae.mode silent\n"
        "\n"
        f"{_SOURCE_EXPORT}"
        "exec setsid -f bash -c '\n"
        "    set -e\n"
        "    vicinae close || true\n"
        f"{_session_setup(session_root, host, indent='    ')}"
        f"    exec {shlex.quote(hop_bin)} kill\n"
        "'\n"
    )


def write_daemon_down_script(scripts_dir: Path, *, error: BaseException, hopd_bin: str) -> None:
    """Replace the hop-* script set with a single "daemon stopped" entry.

    Called from ``hopd``'s exception handler so the user sees a clear
    "click to restart" entry in vicinae instead of a stale hop-* set
    that silently reflects whatever state the daemon last computed.

    Picking the entry runs ``setsid -f <hopd_bin>`` to detach a fresh
    daemon process. ``hopd_bin`` is an absolute path because the action
    runs under vicinae's inherited PATH, which need not contain hop's
    install dir — a bare ``hopd`` would not resolve, leaving the restart
    entry as dead as the daemon it's meant to revive.
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
        f"# @vicinae.icon {_ICON_PATH}\n"
        "# @vicinae.mode silent\n"
        "\n"
        f"exec setsid -f {shlex.quote(hopd_bin)} </dev/null >/dev/null 2>&1\n"
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
