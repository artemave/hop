from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, cast

from hop.backends import SHELL_FALLBACK, HostBackend, SessionBackend
from hop.config import EDITOR_ROLE as _EDITOR_ROLE_CONST
from hop.config import SHELL_ROLE
from hop.errors import HopError
from hop.kitty import HOP_ROLE_VAR, KittyTransport, SocketKittyTransport, session_socket_address
from hop.layouts import WindowSpec, find_window
from hop.session import ProjectSession
from hop.sway import SwayIpcAdapter, SwayWindow

EDITOR_ROLE = _EDITOR_ROLE_CONST
EDITOR_OS_WINDOW_NAME = f"hop:{EDITOR_ROLE}"
EDITOR_MARK_PREFIX = "_hop_editor:"
EDITOR_READY_TIMEOUT_SECONDS = 5.0
EDITOR_READY_POLL_INTERVAL_SECONDS = 0.05

# Keystroke building blocks used to drive the in-pty nvim.
# `<Esc>` (0x1b) is the prefix we send before `:drop`. Tempting to use
# `<C-\><C-n>` (the "force normal mode" idiom that bypasses user mappings)
# but its leading byte 0x1c is the tty's default `quit` control character.
# When the pty is still in cooked mode — which it is during slow command
# startup like `podman-compose exec devcontainer nvim` — the kernel
# intercepts 0x1c and sends SIGQUIT to the foreground process group,
# killing the launching process before nvim ever runs. `<Esc>` has no such
# tty significance and reliably puts a running nvim into normal mode.
_NORMAL_MODE = "\x1b"
_CR = "\r"

SessionBackendFactory = Callable[[ProjectSession], SessionBackend]
SessionWindowsFactory = Callable[[ProjectSession], Sequence[WindowSpec]]


class NeovimError(HopError):
    """Base error for Neovim lifecycle failures."""


class NeovimCommandError(NeovimError):
    """Raised when hop cannot start or control the shared Neovim instance."""


class EditorSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def focus_window(self, window_id: int) -> None: ...

    def mark_window(self, window_id: int, mark: str) -> None: ...

    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None: ...


class KittyEditorIO(Protocol):
    """How the editor adapter actually drives kitty.

    Two implementations live below: ``IpcKittyEditorIO`` for the host CLI
    (synchronous unix-socket IPC against the per-session kitty), and
    ``BossKittyEditorIO`` for use inside the kitty boss event loop (kittens),
    where IPC against ourselves would deadlock the loop.
    """

    def launch_editor(
        self,
        session: ProjectSession,
        *,
        args: Sequence[str],
        os_window_class: str,
        var: Sequence[str],
        keep_focus: bool,
    ) -> None: ...

    def send_text_to_editor(self, session: ProjectSession, text: str) -> None: ...


class IpcKittyEditorIO:
    """Talks to kitty over the per-session unix socket. Synchronous request /
    response. Use from outside the kitty boss process — host CLI, subprocess
    contexts, etc."""

    def __init__(
        self,
        transport_factory: Callable[[str], KittyTransport] | None = None,
    ) -> None:
        self._transport_factory: Callable[[str], KittyTransport] = transport_factory or (
            lambda listen_on: SocketKittyTransport(listen_on)
        )

    def launch_editor(
        self,
        session: ProjectSession,
        *,
        args: Sequence[str],
        os_window_class: str,
        var: Sequence[str],
        keep_focus: bool,
    ) -> None:
        self._transport(session).send_command(
            "launch",
            {
                "args": list(args),
                "cwd": str(session.project_root),
                "type": "os-window",
                "keep_focus": keep_focus,
                "allow_remote_control": True,
                "window_title": EDITOR_ROLE,
                "os_window_title": EDITOR_ROLE,
                # `os_window_class` sets Sway's `app_id` on Wayland;
                # `os_window_name` would only set the X11 WM_CLASS-name half
                # and leave Wayland's app_id at the default (`kitty`), which
                # would prevent Sway-side window discovery from matching.
                "os_window_class": os_window_class,
                # User-var so subsequent matches can find the window without
                # re-walking ls — newly launched editors carry it; older
                # editors fall back to wm_class matching.
                "var": list(var),
            },
        )

    def send_text_to_editor(self, session: ProjectSession, text: str) -> None:
        transport = self._transport(session)
        window_id = self._find_editor_kitty_window_id(transport, session)
        transport.send_command(
            "send-text",
            {
                "match": f"id:{window_id}",
                "data": f"text:{text}",
            },
        )

    def _transport(self, session: ProjectSession) -> KittyTransport:
        return self._transport_factory(session_socket_address(session.session_name))

    def _find_editor_kitty_window_id(self, transport: KittyTransport, session: ProjectSession) -> int:
        # Match by os_window_class so we find the editor regardless of when
        # it was launched — older hop versions didn't tag windows with the
        # `hop_role` user var, so we can't rely on `match: var:...` for
        # editors created before this codepath landed. The os_window_class
        # has been stable across hop's entire history.
        response = transport.send_command("ls", {"output_format": "json"})
        payload = _coerce_ls_payload(response)
        for os_window in payload:
            if not isinstance(os_window, Mapping):
                continue
            os_window_map = cast(Mapping[str, Any], os_window)
            if os_window_map.get("wm_class") != EDITOR_OS_WINDOW_NAME:
                continue
            for tab in os_window_map.get("tabs", ()):
                if not isinstance(tab, Mapping):
                    continue
                tab_map = cast(Mapping[str, Any], tab)
                for window_entry in tab_map.get("windows", ()):
                    if not isinstance(window_entry, Mapping):
                        continue
                    window_id = cast(Mapping[str, Any], window_entry).get("id")
                    if isinstance(window_id, int):
                        return window_id
        msg = f"No editor kitty window found for session {session.session_name!r}."
        raise NeovimCommandError(msg)


class BossKittyEditorIO:
    """Talks to kitty by directly using its in-process boss API. Use only
    from inside the kitty boss event loop (i.e. from kittens). Synchronous
    IPC against the same kitty would deadlock the loop while the kitten is
    running, but the boss's data structures are accessible right here.
    """

    def __init__(self, boss: Any) -> None:
        self._boss = boss

    def launch_editor(
        self,
        session: ProjectSession,
        *,
        args: Sequence[str],
        os_window_class: str,
        var: Sequence[str],
        keep_focus: bool,
    ) -> None:
        # Launching a new editor while the boss is busy running a kitten is
        # awkward — kitty's launch helpers want to dispatch into the event
        # loop. In practice the kitten only dispatches when an editor is
        # already running (the user has been editing for a while); if it
        # isn't, surface a clear error and let the user run `hop edit` from
        # a shell to bring it up.
        msg = (
            "No editor is running for this session. The kitten cannot launch "
            "one without blocking kitty; run `hop edit` from a shell first."
        )
        raise NeovimCommandError(msg)

    def send_text_to_editor(self, session: ProjectSession, text: str) -> None:
        window = self._find_editor_window()
        if window is None:
            msg = (
                f"No editor window for session {session.session_name!r} in this kitty boss. "
                "Run `hop edit` from a shell first."
            )
            raise NeovimCommandError(msg)
        window.write_to_child(text.encode("utf-8"))

    def _find_editor_window(self) -> Any:
        boss = self._boss
        windows: list[Any] = list(boss.window_id_map.values())
        # Prefer user_vars matching when present (newer launches).
        for window in windows:
            user_vars: Any = getattr(window, "user_vars", None) or {}
            if user_vars.get(HOP_ROLE_VAR) == EDITOR_ROLE:
                return window
        # Fall back to the os-window class — older launches set this but not
        # the user_var, so this keeps editors that pre-date the kitten
        # dispatch path workable without a forced relaunch.
        os_window_map: Any = getattr(boss, "os_window_map", None) or {}
        for window in windows:
            os_window_id: Any = getattr(window, "os_window_id", None)
            if os_window_id is None:
                continue
            tab_manager: Any = os_window_map.get(os_window_id)
            if tab_manager is None:
                continue
            wm_class: Any = getattr(tab_manager, "wm_class", None)
            if wm_class == EDITOR_OS_WINDOW_NAME:
                return window
        return None


class SharedNeovimEditorAdapter:
    def __init__(
        self,
        *,
        sway: EditorSwayAdapter | None = None,
        kitty_io: KittyEditorIO | None = None,
        session_backend_for: SessionBackendFactory | None = None,
        session_windows_for: SessionWindowsFactory | None = None,
        ready_timeout_seconds: float = EDITOR_READY_TIMEOUT_SECONDS,
        ready_poll_interval_seconds: float = EDITOR_READY_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._sway: EditorSwayAdapter = sway or SwayIpcAdapter()
        self._kitty_io: KittyEditorIO = kitty_io or IpcKittyEditorIO()
        self._session_backend_for: SessionBackendFactory = session_backend_for or (lambda _session: HostBackend())
        # Resolves the session's window list so the launch path can pick up
        # user overrides for editor / shell commands. Default returns no
        # windows so the built-in nvim + ${SHELL:-sh} fallback applies.
        self._session_windows_for: SessionWindowsFactory = session_windows_for or (lambda _session: ())
        self._ready_timeout_seconds = ready_timeout_seconds
        self._ready_poll_interval_seconds = ready_poll_interval_seconds

    def ensure(self, session: ProjectSession, *, keep_focus: bool = True) -> bool:
        # Bring up the editor. ``keep_focus`` controls whether the launch
        # steals focus to the new editor (False) or leaves it on the
        # currently-focused window (True). Returns True if a new editor
        # window was launched, False if an existing one was found —
        # callers (spawn_session_terminal) use this to decide whether
        # `hop` from within a session should also spawn an extra shell,
        # or whether resurrecting the editor was the whole job.
        #
        # The bootstrap path passes ``keep_focus=False`` so that the
        # activation sweep's subsequent terminal launches tab in *after*
        # the editor in sway's tabbed layout (sway inserts new tabs after
        # the focused one). With ``keep_focus=True`` the shell would stay
        # focused, terminals would slot in between shell and editor, and
        # the editor would walk to the end of the tab strip. End-of-
        # bootstrap ``_focus_shell_if_present`` still hands focus back
        # to the shell.
        _, was_launched = self._ensure_editor(session, keep_focus=keep_focus)
        return was_launched

    def focus(self, session: ProjectSession) -> None:
        # `focus()` re-focuses the editor unconditionally: an explicit
        # sway.focus_window after the launch handles both the "editor
        # already existed" and "editor was just launched" cases. Kitty's
        # keep_focus is irrelevant here because we override sway focus
        # afterwards anyway, so passing True keeps the launch-time focus
        # change from briefly flickering through another window.
        window, _ = self._ensure_editor(session, keep_focus=True)
        # Sway-driven focus (rather than Kitty's `focus-window`) so the focus
        # change escalates to a workspace switch when the editor lives on a
        # different Sway workspace than the caller — e.g. when the kitten
        # dispatches a file or URL from a terminal session.
        self._sway.focus_window(window.id)

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        window, _ = self._ensure_editor(session, keep_focus=True)
        self._sway.focus_window(window.id)
        path_text, line_number = _split_target(self._translate_target(session, target))
        self._kitty_io.send_text_to_editor(session, _build_open_keystrokes(path_text, line_number))

    def _translate_target(self, session: ProjectSession, target: str) -> str:
        # `target` is a host path (optionally with `:line`). For backends whose
        # nvim runs in a different filesystem namespace (e.g. devcontainer),
        # rewrite the path to its in-backend location so `:drop <path>` finds
        # the file. The line suffix is reattached unchanged.
        path_text, line_number = _split_target(target)
        backend = self._session_backend_for(session)
        translated = backend.translate_host_path(session, Path(path_text))
        if line_number is None:
            return str(translated)
        return f"{translated}:{line_number}"

    def _ensure_editor(self, session: ProjectSession, *, keep_focus: bool) -> tuple[SwayWindow, bool]:
        existing = self._find_editor_window(session)
        if existing is not None:
            return existing, False
        # Snapshot pre-launch Sway windows so we can pick out the freshly
        # created one by id, regardless of which workspace it lands on.
        # `hop edit` from a host shell triggers this path: kitty creates the
        # editor on whatever workspace was focused at launch time, and we
        # have to relocate it to the session workspace ourselves (Sway has
        # no per-app_id placement rule from hop's side).
        known_window_ids = {window.id for window in self._sway.list_windows()}
        backend = self._session_backend_for(session)
        self._kitty_io.launch_editor(
            session,
            args=list(self._editor_launch_args(session, backend=backend)),
            os_window_class=EDITOR_OS_WINDOW_NAME,
            var=[f"{HOP_ROLE_VAR}={EDITOR_ROLE}"],
            keep_focus=keep_focus,
        )
        return self._adopt_new_editor_window(session, known_window_ids=known_window_ids), True

    def _editor_launch_args(
        self,
        session: ProjectSession,
        *,
        backend: SessionBackend,
    ) -> Sequence[str]:
        # Compose `<editor>; <shell>` so the kitty window remains usable
        # after the editor exits — `nvim -S Session.vim` to restore buffers,
        # peek at git, etc. Each piece is wrapped through the backend's
        # inline() helper so the prefix runs each side as its own backend
        # exec, preserving today's two-call behavior. The post-exit shell
        # falls back to ${SHELL:-sh} when the resolver yields the empty
        # sentinel (built-in shell on host or backend default).
        windows = self._session_windows_for(session)
        editor_spec = find_window(windows, EDITOR_ROLE)
        editor_command = editor_spec.command if editor_spec is not None and editor_spec.command else "nvim"
        shell_spec = find_window(windows, SHELL_ROLE)
        shell_command = shell_spec.command if shell_spec is not None and shell_spec.command else SHELL_FALLBACK
        editor_inline = backend.inline(editor_command, session)
        shell_inline = backend.inline(shell_command, session)
        return ("sh", "-c", f"{editor_inline}; {shell_inline}")

    def _find_editor_window(self, session: ProjectSession) -> SwayWindow | None:
        # The session's editor is identified across hop runs by a Sway mark.
        # On first sighting (or after a hop crash that lost the mark) fall back
        # to discovering the unmarked editor on this session's workspace, then
        # re-mark it for fast lookup later — and to survive drift onto other
        # workspaces.
        mark = _editor_mark(session)
        windows = list(self._sway.list_windows())

        marked = [window for window in windows if mark in window.marks]
        if marked:
            return min(marked, key=lambda candidate: candidate.id)

        candidates = [
            window
            for window in windows
            if (window.app_id == EDITOR_OS_WINDOW_NAME or window.window_class == EDITOR_OS_WINDOW_NAME)
            and window.workspace_name == session.workspace_name
            and not any(other_mark.startswith(EDITOR_MARK_PREFIX) for other_mark in window.marks)
        ]
        if not candidates:
            return None

        window = min(candidates, key=lambda candidate: candidate.id)
        self._sway.mark_window(window.id, mark)
        return window

    def _adopt_new_editor_window(
        self,
        session: ProjectSession,
        *,
        known_window_ids: set[int],
    ) -> SwayWindow:
        # After kitty creates the window, Sway sees it via Wayland a moment
        # later. Poll for any new window with the editor's app_id /
        # window_class — workspace-agnostic, since the new window may have
        # landed on the caller's workspace rather than the session's. Once
        # found: relocate to the session workspace if it drifted, then mark
        # so subsequent lookups (kitten dispatch, repeat hop edit) find it
        # instantly via the mark across workspaces.
        deadline = time.monotonic() + self._ready_timeout_seconds
        while time.monotonic() < deadline:
            new_candidates = [
                window
                for window in self._sway.list_windows()
                if window.id not in known_window_ids
                and (window.app_id == EDITOR_OS_WINDOW_NAME or window.window_class == EDITOR_OS_WINDOW_NAME)
            ]
            if new_candidates:
                window = min(new_candidates, key=lambda candidate: candidate.id)
                if window.workspace_name != session.workspace_name:
                    self._sway.move_window_to_workspace(window.id, session.workspace_name)
                self._sway.mark_window(window.id, _editor_mark(session))
                return window
            time.sleep(self._ready_poll_interval_seconds)

        msg = f"Sway did not register an editor window for session {session.session_name!r}."
        raise NeovimCommandError(msg)


def _coerce_ls_payload(response: object) -> Sequence[object]:
    """Extract the list of OS windows from a kitty ``ls`` response.

    The kitty IPC response either is the raw payload, or wraps it in a
    ``{"data": ...}`` envelope where ``data`` may be a JSON string that
    needs decoding.
    """
    import json

    data: object = response
    if isinstance(response, Mapping):
        data = cast(Mapping[str, Any], response).get("data")
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, list):
        return cast(Sequence[object], data)
    return ()


def _build_open_keystrokes(path: str, line_number: int | None) -> str:
    """Build the keystroke sequence that opens ``path`` in nvim.

    Wrapping the path in ``fnameescape`` lets vim handle every metacharacter
    its command line cares about (spaces, ``%``, ``#``, ``\\`` , ``[``, etc.)
    without us reimplementing those rules in Python. The path itself is
    embedded as a vim single-quoted string, where the only escape needed is
    doubling internal single quotes.
    """

    quoted = path.replace("'", "''")
    sequence = f"{_NORMAL_MODE}:exec 'drop '.fnameescape('{quoted}'){_CR}"
    if line_number is not None:
        sequence += f":{line_number}{_CR}"
    return sequence


def _split_target(target: str) -> tuple[str, int | None]:
    path_text, separator, suffix = target.rpartition(":")
    if separator and suffix.isdigit() and path_text:
        return path_text, int(suffix)
    return target, None


def _editor_mark(session: ProjectSession) -> str:
    return f"{EDITOR_MARK_PREFIX}{session.session_name}"
