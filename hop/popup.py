"""Floating-kitty popup for headless `hop` invocations.

When `hop` runs without a controlling TTY (vicinae's detached `setsid -f hop`,
sway keybindings, launcher scripts), `default_runner` captures subprocess
stderr and `cli.main`'s `HopError` catch prints to a stderr nobody is watching.
This module replaces the missing UI with a regular kitty OS window — floated
and centered by a sway ``for_window`` rule installed at the moment of window
registration — that streams `prepare` / `teardown` output and surfaces
unhandled errors so the user can see what happened.

A regular floating window (not a `kitten panel` layer-shell overlay) is the
chosen surface so the user can navigate away from it (switching to another
workspace hides it, like any other window) instead of being trapped under a
fullscreen overlay that persists across workspace changes.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Literal, Protocol, Sequence

from hop import debug
from hop.backends import SessionBackend, SessionBackendError, backend_lock_path, substitute
from hop.errors import HopError
from hop.session import ProjectSession
from hop.sway import SwayWindow


class PopupProcess(Protocol):
    """Minimal surface KittyHopPopup needs from a spawned popup process: a
    blocking `wait()` that returns the exit code. Real spawns return a
    `subprocess.Popen`; tests pass a fake of the same shape."""

    def wait(self) -> int: ...


PopupLauncher = Callable[[Sequence[str]], PopupProcess]
StderrIsatty = Callable[[], bool]

POPUP_APP_ID = "hop:popup"
# Size of the floating popup, as a percentage of the workspace's output (sway's
# `ppt` unit). 60% × 50% gives a comfortable popup that fits inside a single
# monitor on common 16:9 / 16:10 layouts.
_POPUP_WIDTH_PPT = 60
_POPUP_HEIGHT_PPT = 50


def popup_for_window_commands() -> tuple[str, ...]:
    """The ``for_window`` rules that float and size every hop popup.

    Issued via sway IPC before each ``kitty`` launch. Sway evaluates
    ``for_window`` *as the window registers*, which sidesteps the polling
    race the previous implementation had (kitty cold-start could miss the
    polling window, leaving the popup tiled at the workspace's full size
    when the user's ``workspace_layout`` is ``tabbed`` / ``stacking``).

    Three separate rules rather than one comma-chained rule because sway's
    parser binds only the first command after ``for_window [criteria]`` to
    the rule; subsequent comma-chained commands are evaluated immediately
    against the currently focused container, which fails for ``move
    position center`` ("Only floating containers can be moved to an
    absolute position").

    Rules accumulate in sway's runtime state — three per ``hop`` invocation
    until sway exits or reloads — but every rule is identical and runs the
    same idempotent commands, so a duplicate just costs an extra IPC
    dispatch when the next popup appears. Sway IPC has no query for
    installed rules, so de-duplicating across invocations isn't possible.
    """

    criteria = f'[app_id="{POPUP_APP_ID}"]'
    return (
        f"for_window {criteria} floating enable",
        f"for_window {criteria} resize set {_POPUP_WIDTH_PPT} ppt {_POPUP_HEIGHT_PPT} ppt",
        f"for_window {criteria} move position center",
    )


class HopPopup(Protocol):
    def is_interactive(self) -> bool: ...

    def run_prepare(self, session: ProjectSession, backend: SessionBackend) -> None: ...

    def run_teardown(self, session: ProjectSession, backend: SessionBackend) -> None: ...

    def show_error(self, error: HopError) -> None: ...


class PopupSwayAdapter(Protocol):
    """Sway surface KittyHopPopup needs: a single ``run_command`` to install
    the ``for_window`` rule that floats / sizes / centers the popup as it
    registers."""

    def list_windows(self) -> Sequence[SwayWindow]: ...

    def run_command(self, command: str) -> None: ...


class KittyHopPopup:
    def __init__(
        self,
        *,
        sway: PopupSwayAdapter | None = None,
        launcher: PopupLauncher | None = None,
        stderr_isatty: StderrIsatty | None = None,
    ) -> None:
        self._sway: PopupSwayAdapter | None = sway
        self._launcher: PopupLauncher = launcher or _default_launcher
        self._stderr_isatty: StderrIsatty = stderr_isatty or (lambda: sys.stderr.isatty())

    def is_interactive(self) -> bool:
        return self._stderr_isatty()

    def run_prepare(self, session: ProjectSession, backend: SessionBackend) -> None:
        command = backend.prepare_command
        if command is None:
            return
        self._run_lifecycle(session, command, kind="prepare")

    def run_teardown(self, session: ProjectSession, backend: SessionBackend) -> None:
        command = backend.teardown_command
        if command is None:
            return
        self._run_lifecycle(session, command, kind="teardown")

    def show_error(self, error: HopError) -> None:
        # Best-effort: launcher exit code is informational only. The user
        # closing the panel IS the success signal — there's no failure mode
        # for "display a message".
        self._spawn_and_wait(_error_script(error))

    def _run_lifecycle(
        self,
        session: ProjectSession,
        steps: Sequence[str],
        *,
        kind: Literal["prepare", "teardown"],
    ) -> None:
        exit_code = self._spawn_and_wait(_lifecycle_script(session, steps, kind=kind))
        if exit_code != 0:
            msg = f"session {kind} did not succeed for {session.session_name!r}; see the popup for details"
            raise SessionBackendError(msg, surfaced_by_popup=True)

    def _spawn_and_wait(self, script: str) -> int:
        # Install the ``for_window`` rules *before* launching kitty: sway
        # evaluates them when the popup window registers, so the float +
        # resize + center happens as part of registration rather than being
        # raced by polling afterwards.
        if self._sway is not None:
            for rule in popup_for_window_commands():
                debug.log(f"popup: installing for_window rule: {rule}")
                self._sway.run_command(rule)
            debug.log("popup: sway accepted for_window rules")
        proc = self._launcher(_kitty_argv(script))
        return proc.wait()


def _kitty_argv(script: str) -> tuple[str, ...]:
    # Plain `kitty`, not `kitten panel` — we want a regular xdg-shell
    # toplevel that sway can place on the focused workspace and that the
    # user can navigate away from (workspace switch hides it). The class
    # is the app_id sway sees; floating / sizing / centering is handled by
    # the ``for_window`` rule installed before launch (see
    # ``popup_for_window_command``).
    #
    # ``bash`` (not ``sh``) so the lifecycle script can use process
    # substitution (``> >(tee …)``) to stream output to both the popup
    # terminal and an on-disk log file — POSIX sh has no portable way to
    # do that.
    return (
        "kitty",
        "--class",
        POPUP_APP_ID,
        "--",
        "bash",
        "-c",
        script,
    )


def popup_log_path(session: ProjectSession, kind: str) -> Path:
    """Where lifecycle popups stream their output.

    One file per (session, kind), overwritten on each invocation — so
    ``cat $(xdg-runtime-dir)/hop/popup-<session>-prepare.log`` always shows
    the most recent prepare run. Lets the user inspect what the prepare
    script actually printed without having to keep the popup open.
    """

    base_env = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(base_env) if base_env else Path("/tmp")
    return base / "hop" / f"popup-{session.session_name}-{kind}.log"


def _lifecycle_script(
    session: ProjectSession,
    steps: Sequence[str],
    *,
    kind: Literal["prepare", "teardown"],
) -> str:
    """Render the popup-side shell script for an N-step lifecycle sequence.

    Each step renders as ``printf '$ ...'`` + ``flock ... sh -c '<step>'``
    followed by a per-step status check that drops into a held-open ``sh``
    on failure (so the user sees the failing step's output, can scroll, and
    Ctrl-D closes the popup). A non-zero step short-circuits the rest of the
    sequence by ``exit "$status"`` after the held shell returns.

    Held-open-on-failure: don't ``exec sh`` — the user dismissing it (sh
    exiting 0) would clobber the captured non-zero status and silently
    signal success to the parent hop process.
    """

    verb = "Preparing" if kind == "prepare" else "Tearing down"
    noun = kind
    lock_path = str(backend_lock_path(session))
    log_path = popup_log_path(session, kind)
    # `tee` (no `-a`) truncates each run so the file always holds the most
    # recent invocation's output. The directory is created via the parent
    # shell's `mkdir -p` ahead of the `exec` so the redirect can't fail on a
    # missing parent.
    lines: list[str] = [
        "set -u",
        f"mkdir -p {shlex.quote(str(log_path.parent))}",
        f"exec > >(tee {shlex.quote(str(log_path))}) 2>&1",
        f"cd {shlex.quote(str(session.project_root))}",
        f"printf '%s\\n' {shlex.quote(f'{verb} {session.session_name}')}",
    ]
    for index, step in enumerate(steps):
        substituted = substitute(step, session=session)
        if index > 0:
            lines.append("")
        lines.append(f"printf '$ %s\\n\\n' {shlex.quote(step)}")
        lines.append(f"flock -o {shlex.quote(lock_path)} sh -c {shlex.quote(substituted)}")
        lines.append("status=$?")
        lines.append('if [ "$status" -ne 0 ]; then')
        lines.append(f"    printf '\\n%s failed (exit %d). Press Ctrl-D to close.\\n' {shlex.quote(noun)} \"$status\"")
        lines.append("    sh")
        lines.append('    exit "$status"')
        lines.append("fi")
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def _error_script(error: HopError) -> str:
    text = f"{type(error).__name__}: {error}"
    return f"set -u\nprintf 'hop: %s\\n\\n' {shlex.quote(text)}\nprintf 'Press Ctrl-D to close.\\n'\nexec sh\n"


def _default_launcher(argv: Sequence[str]) -> PopupProcess:
    # Non-detached `Popen`: hop blocks on `.wait()` for the kitty window to
    # exit, so we want the child to die with us if hop is killed. stdin is
    # /dev/null because kitty's parent stdio is usually detached (vicinae,
    # setsid); stdout/stderr inherit so any kitty crash messages land
    # somewhere the user can recover them.
    return subprocess.Popen(list(argv), stdin=subprocess.DEVNULL)
