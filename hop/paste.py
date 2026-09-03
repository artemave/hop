"""Clipboard-image paste: the read + backend write, run off the kitty boss.

The paste kitten (``hop/kitten/paste/main.py``) runs in the kitty boss thread.
It only inspects the clipboard's *MIME list* there (an in-process kitty call
that is safe and non-blocking) and, when an image is on offer, launches
``hop __paste-image <session> <write-path> <mime>`` via
``boss.run_background_process``. This module is that helper's body: it reads
the image bytes with ``wl-paste`` (a subprocess — blocking it can't freeze the
boss, and there is no self-deadlock because the boss has already returned),
writes them into the session backend's filesystem namespace, and maps a slow
or failing step to a non-zero exit plus one stderr line. The kitten's
``notify_on_death`` echoes that into the window.

No kitty imports, so it runs as a plain subprocess and tests with plain fakes.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from types import FrameType
from typing import Callable

from hop.backends import SessionBackend, SessionBackendError
from hop.session import ProjectSession
from hop.state import load_sessions, session_from_state

# Upper bound on the backend write. A dead ssh master or a wedged
# ``podman-compose exec`` would otherwise leave the helper (and the image
# paste) hanging with no feedback; on expiry the child is killed and the
# kitten reports the failure in the window.
WRITE_TIMEOUT_SECONDS = 20.0
# Upper bound on the ``wl-paste`` read of the clipboard image.
CLIPBOARD_READ_TIMEOUT_SECONDS = 10.0

# Resolve a session name to its ``(session, backend)`` pair, or ``None`` when
# the name isn't in hop state. Injected in tests; production rebuilds from the
# on-disk session record.
SessionResolver = Callable[[str], "tuple[ProjectSession, SessionBackend] | None"]
# Read ``mime`` off the host clipboard, or ``None`` when it holds nothing of
# that type. Injected in tests; production shells out to ``wl-paste``.
ClipboardReader = Callable[[str], "bytes | None"]


class _WriteTimeout(Exception):
    """Raised from the SIGALRM handler when the backend write runs too long."""


def _resolve_from_state(session_name: str) -> tuple[ProjectSession, SessionBackend] | None:
    state = load_sessions().get(session_name)
    if state is None:
        return None
    # Lazy, mirroring ``hop.focused``: ``hop.app`` pulls in the world and the
    # helper is a cold process that only sometimes needs it.
    from hop.app import backend_from_record

    backend = backend_from_record(state.backend, session_root=state.session_root)
    return session_from_state(state), backend


def _wl_paste_image(mime: str) -> bytes | None:
    """Read ``mime`` off the host clipboard via ``wl-paste``.

    ``None`` when ``wl-paste`` is missing, times out, exits non-zero, or the
    clipboard no longer holds that type (it can change between the kitten's
    MIME-list check and this read).
    """

    try:
        result = subprocess.run(
            ["wl-paste", "--type", mime, "--no-newline"],
            capture_output=True,
            check=False,
            timeout=CLIPBOARD_READ_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def run_paste_helper(
    *,
    session_name: str,
    write_path: str,
    mime: str,
    resolve: SessionResolver = _resolve_from_state,
    read_clipboard: ClipboardReader = _wl_paste_image,
    write_timeout: float = WRITE_TIMEOUT_SECONDS,
) -> int:
    """Read the clipboard image and write it at ``write_path`` in the backend.

    Returns a process exit code: ``0`` on success, ``1`` on a handled failure
    (unknown session, empty/unreadable clipboard, backend write error, or the
    write exceeding ``write_timeout``). The stderr line is what the kitten
    echoes into the window.
    """

    resolved = resolve(session_name)
    if resolved is None:
        print(f"hop paste: no active session {session_name!r}", file=sys.stderr)
        return 1
    session, backend = resolved

    data = read_clipboard(mime)
    if not data:
        print(f"hop paste: nothing of type {mime!r} on the clipboard", file=sys.stderr)
        return 1

    def _on_alarm(_signum: int, _frame: FrameType | None) -> None:
        raise _WriteTimeout

    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, write_timeout)
    try:
        backend.write_file(session, Path(write_path), data)
    except _WriteTimeout:
        print(f"hop paste: backend write exceeded {write_timeout:g}s", file=sys.stderr)
        return 1
    except SessionBackendError as exc:
        print(f"hop paste: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return 0
