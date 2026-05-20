"""Floating-kitty popup for headless `hop` invocations.

When `hop` runs without a controlling TTY (vicinae's detached `setsid -f hop`,
sway keybindings, launcher scripts), `default_runner` captures subprocess
stderr and `cli.main`'s `HopError` catch prints to a stderr nobody is watching.
This module replaces the missing UI with a regular kitty OS window — marked
floating + centered via sway IPC after it appears — that streams `prepare` /
`teardown` output and surfaces unhandled errors so the user can see what
happened.

A regular floating window (not a `kitten panel` layer-shell overlay) is the
chosen surface so the user can navigate away from it (switching to another
workspace hides it, like any other window) instead of being trapped under a
fullscreen overlay that persists across workspace changes.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from typing import Callable, Literal, Protocol, Sequence

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
# monitor on common 16:9 / 16:10 layouts — kitty's default `initial_window_*`
# can otherwise be set large enough that an unconstrained floating window
# spans across monitor boundaries.
_POPUP_WIDTH_PPT = 60
_POPUP_HEIGHT_PPT = 50
# Fallback initial size baked into kitty's launch flags (cell units, scales
# with the user's font). The sway-IPC ``resize set ... ppt`` is the primary
# sizing path, but it races kitty's window registration — if kitty is slow
# to register (cold start), the resize never fires and the popup is stuck
# at whatever kitty defaulted to. Capping the launch size keeps the popup
# usable even when the race is lost.
_POPUP_INITIAL_WIDTH_CELLS = 120
_POPUP_INITIAL_HEIGHT_CELLS = 30
# Window-discovery bound: kitty typically registers its window within ~200 ms;
# 2 s of polling at 50 ms is comfortable headroom without making a kitty
# failure-to-start hang hop for long.
_FLOAT_POLL_TIMEOUT_SECONDS = 2.0
_FLOAT_POLL_INTERVAL_SECONDS = 0.05


class HopPopup(Protocol):
    def is_interactive(self) -> bool: ...

    def run_prepare(self, session: ProjectSession, backend: SessionBackend) -> None: ...

    def run_teardown(self, session: ProjectSession, backend: SessionBackend) -> None: ...

    def show_error(self, error: HopError) -> None: ...


class PopupSwayAdapter(Protocol):
    """Sway surface KittyHopPopup needs: enumerate windows (to find the popup's
    `con_id` after it appears) and issue a raw command (to float and center
    it). Kept narrow so tests can pass a minimal fake."""

    def list_windows(self) -> Sequence[SwayWindow]: ...

    def run_command(self, command: str) -> None: ...


class KittyHopPopup:
    def __init__(
        self,
        *,
        sway: PopupSwayAdapter | None = None,
        launcher: PopupLauncher | None = None,
        stderr_isatty: StderrIsatty | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sway: PopupSwayAdapter | None = sway
        self._launcher: PopupLauncher = launcher or _default_launcher
        self._stderr_isatty: StderrIsatty = stderr_isatty or (lambda: sys.stderr.isatty())
        self._clock = clock
        self._sleep = sleep

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
        exit_code = self._spawn_and_wait(
            _lifecycle_script(session, steps, kind=kind),
            workspace_name=session.workspace_name,
        )
        if exit_code != 0:
            msg = f"session {kind} did not succeed for {session.session_name!r}; see the popup for details"
            raise SessionBackendError(msg, surfaced_by_popup=True)

    def _spawn_and_wait(self, script: str, *, workspace_name: str | None = None) -> int:
        argv = _kitty_argv(script)
        # Snapshot existing popup-app-id windows so the polling loop can pick
        # out *our* new window even when a stale one is still being torn down
        # by sway (rare but possible if a prior popup raced exit/destroy).
        seen_before: set[int] = self._popup_window_ids() if self._sway is not None else set()
        proc = self._launcher(argv)
        if self._sway is not None:
            self._float_new_popup_window(seen_before, workspace_name=workspace_name)
        return proc.wait()

    def _popup_window_ids(self) -> set[int]:
        assert self._sway is not None
        return {w.id for w in self._sway.list_windows() if w.app_id == POPUP_APP_ID}

    def _float_new_popup_window(
        self,
        seen_before: set[int],
        *,
        workspace_name: str | None,
    ) -> None:
        """Poll sway's window tree for the kitty window we just spawned, then
        issue `move container to workspace p:<session>, floating enable,
        resize set <w>ppt <h>ppt, move position center` against it.

        The move-to-workspace step is the load-bearing fix for the "popup
        spills onto a different monitor" symptom: kitty creates the toplevel
        on whatever workspace happens to be focused at registration time, so
        if the user has wandered off ``p:<session>`` during ``prepare`` the
        popup lands on the wrong output and the subsequent ``ppt`` resize is
        measured against that wrong output. Moving first pins the popup to
        the session's workspace; the resize+center then applies to the
        session's output.

        Resizing is needed because kitty's default ``initial_window_*`` can
        produce a window that spans multiple monitors; ``resize set`` with
        ``ppt`` scales to the workspace's output. ``move position center``
        re-centers after the resize.

        ``workspace_name`` may be ``None`` (currently only the ``show_error``
        path), in which case the popup floats on whichever workspace the
        kitty window happened to register on.

        Bounded by a short timeout so a kitty that fails to register a window
        (typically because the binary crashed at startup) doesn't hang hop —
        ``proc.wait()`` will then surface the exit code.
        """
        assert self._sway is not None
        deadline = self._clock() + _FLOAT_POLL_TIMEOUT_SECONDS
        while self._clock() < deadline:
            current = self._popup_window_ids()
            new_ids = current - seen_before
            if new_ids:
                target = max(new_ids)
                steps: list[str] = []
                if workspace_name is not None:
                    steps.append(f"move container to workspace {workspace_name}")
                steps.extend(
                    [
                        "floating enable",
                        f"resize set {_POPUP_WIDTH_PPT} ppt {_POPUP_HEIGHT_PPT} ppt",
                        "move position center",
                    ]
                )
                self._sway.run_command(f"[con_id={target}] " + ", ".join(steps))
                return
            self._sleep(_FLOAT_POLL_INTERVAL_SECONDS)


def _kitty_argv(script: str) -> tuple[str, ...]:
    # Plain `kitty`, not `kitten panel` — we want a regular xdg-shell
    # toplevel that sway can place on the focused workspace and that the
    # user can navigate away from (workspace switch hides it). The class
    # is the app_id sway sees; the floating + center commands are issued
    # via sway IPC after the window registers.
    return (
        "kitty",
        "--class",
        POPUP_APP_ID,
        "-o",
        f"initial_window_width={_POPUP_INITIAL_WIDTH_CELLS}c",
        "-o",
        f"initial_window_height={_POPUP_INITIAL_HEIGHT_CELLS}c",
        "--",
        "sh",
        "-c",
        script,
    )


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
    lines: list[str] = [
        "set -u",
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
