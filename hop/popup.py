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

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Literal, Protocol, Sequence

from hop import debug
from hop.backends import SessionBackend, SessionBackendError, SupportsWrite, runner_cwd, stream_step
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
        self._run_lifecycle(session, command, kind="prepare", backend=backend)

    def run_teardown(self, session: ProjectSession, backend: SessionBackend) -> None:
        command = backend.teardown_command
        if command is None:
            return
        self._run_lifecycle(session, command, kind="teardown", backend=backend)

    def show_error(self, error: HopError) -> None:
        # Best-effort: launcher exit code is informational only. The user
        # closing the panel IS the success signal — there's no failure mode
        # for "display a message". Still a bash one-liner: no lifecycle to run,
        # just a message + a held shell.
        self._spawn_and_wait(_kitty_command_argv("bash", "-c", _error_script(error)))

    def _run_lifecycle(
        self,
        session: ProjectSession,
        steps: Sequence[str],
        *,
        kind: Literal["prepare", "teardown"],
        backend: SessionBackend,
    ) -> None:
        # The popup window runs `hop __run-lifecycle <spec>` (see
        # `run_popup_lifecycle`) rather than a bash script, so it reuses the same
        # `stream_step` spinner + live output as the inline path. Everything the
        # child needs — the composed per-step argv, the cwd, the log path — is
        # frozen into a JSON spec here, since the child is a fresh process with
        # no access to this backend/session object.
        spec_path = popup_spec_path(session, kind)
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(_lifecycle_spec(session, steps, kind=kind, backend=backend))
        exit_code = self._spawn_and_wait(_kitty_lifecycle_argv(spec_path))
        if exit_code != 0:
            msg = f"session {kind} did not succeed for {session.session_name!r}; see the popup for details"
            raise SessionBackendError(msg, surfaced_by_popup=True)

    def _spawn_and_wait(self, argv: Sequence[str]) -> int:
        # Install the ``for_window`` rules *before* launching kitty: sway
        # evaluates them when the popup window registers, so the float +
        # resize + center happens as part of registration rather than being
        # raced by polling afterwards.
        if self._sway is not None:
            for rule in popup_for_window_commands():
                debug.log(f"popup: installing for_window rule: {rule}")
                self._sway.run_command(rule)
            debug.log("popup: sway accepted for_window rules")
        proc = self._launcher(argv)
        return proc.wait()


def _kitty_command_argv(*command: str) -> tuple[str, ...]:
    # Plain `kitty`, not `kitten panel` — we want a regular xdg-shell
    # toplevel that sway can place on the focused workspace and that the
    # user can navigate away from (workspace switch hides it). The class
    # is the app_id sway sees; floating / sizing / centering is handled by
    # the ``for_window`` rule installed before launch (see
    # ``popup_for_window_command``). ``command`` is whatever the window runs.
    return (
        "kitty",
        "--class",
        POPUP_APP_ID,
        "--",
        *command,
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


def popup_spec_path(session: ProjectSession, kind: str) -> Path:
    """Where the lifecycle spec JSON handed to ``hop __run-lifecycle`` lands.

    Sits next to the log so both are overwritten per (session, kind) run and
    are easy to find; the child reads it back to know what to execute."""

    return popup_log_path(session, kind).with_suffix(".spec.json")


def _kitty_lifecycle_argv(spec_path: Path) -> tuple[str, ...]:
    # Run the lifecycle inside the popup window through hop itself, so it reuses
    # `stream_step`'s spinner + live output instead of a bespoke bash script.
    # `sys.executable -m hop` (an absolute interpreter path, frozen from the
    # launching process) is robust to PATH differences inside the fresh window.
    return _kitty_command_argv(sys.executable, "-m", "hop", "__run-lifecycle", str(spec_path))


def _lifecycle_spec(
    session: ProjectSession,
    steps: Sequence[str],
    *,
    kind: Literal["prepare", "teardown"],
    backend: SessionBackend,
) -> str:
    """Freeze an N-step lifecycle sequence into the JSON ``hop __run-lifecycle``
    consumes.

    Each step carries its ``display`` string (for the ``$ …`` announcement) and
    the backend's fully composed ``flock + transport`` ``argv`` (``sh -c``
    locally, ``ssh`` for a remote session — see ``CommandBackend.lifecycle_argv``)
    so the child needs neither the backend nor the config. ``cwd`` is the
    backend's *local* working dir (the project root locally, the host home for a
    remote session whose project root only exists on the remote; the remote cd
    rides inside the transport)."""

    verb = "Preparing" if kind == "prepare" else "Tearing down"
    spec = {
        "kind": kind,
        "verb": f"{verb} {session.session_name}",
        "cwd": str(runner_cwd(session.host, session.session_root)),
        "log_path": str(popup_log_path(session, kind)),
        "steps": [{"display": step, "argv": list(backend.lifecycle_argv(step, session))} for step in steps],
    }
    return json.dumps(spec)


class _Tee:
    """Fan every write out to several sinks — the popup tty and its log file.

    Only the real step output flows through here, so the log stays free of the
    status line's cursor controls (those go straight to the tty)."""

    def __init__(self, *sinks: "SupportsWrite") -> None:
        self._sinks = sinks

    def write(self, data: str, /) -> int:
        for sink in self._sinks:
            sink.write(data)
        return len(data)

    def flush(self) -> None:
        for sink in self._sinks:
            sink.flush()


def _hold_shell() -> None:
    # Drop into an interactive shell so the user can read the failing step's
    # output before the popup closes; Ctrl-D (EOF) returns here and the caller
    # then exits with the step's non-zero status.
    subprocess.run(["sh"], check=False)


def run_popup_lifecycle(spec_path: Path, *, hold: Callable[[], None] = _hold_shell) -> int:
    """Execute a lifecycle spec inside the popup window (``hop __run-lifecycle``).

    Runs each step through the shared ``stream_step`` — same spinner + live,
    line-by-line output as the inline path — teeing the real output to the popup
    log while the spinner stays on the tty. A failing step prints a banner, holds
    an interactive shell so the user can read the error, then short-circuits with
    that step's exit code (which the parent hop reads off the popup process to
    raise its ``SessionBackendError``). ``hold`` is injected so tests exercise the
    failure path without spawning a real shell."""

    spec = json.loads(spec_path.read_text())
    kind = spec["kind"]
    cwd = Path(spec["cwd"])
    label = f"hop {kind} is running"
    log_path = Path(spec["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        out = _Tee(sys.stderr, log)
        out.write(f"{spec['verb']}\n")
        out.flush()
        for step in spec["steps"]:
            result = stream_step(
                step["argv"],
                cwd,
                announce=f"$ {step['display']}\n",
                label=label,
                out=out,
                status=sys.stderr,
            )
            if result.returncode != 0:
                out.write(f"\n{kind} failed (exit {result.returncode}). Press Ctrl-D to close.\n")
                out.flush()
                hold()
                return result.returncode
    return 0


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
