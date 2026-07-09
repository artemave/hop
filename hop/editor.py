from __future__ import annotations

import os
import subprocess
from typing import Any, Callable, Mapping, Protocol, Sequence, cast

from hop.config import EDITOR_ROLE as _EDITOR_ROLE_CONST
from hop.errors import HopError
from hop.kitty import HOP_ROLE_VAR, KittyTransport, SocketKittyTransport, session_socket_address
from hop.layouts import WindowSpec, find_window
from hop.session import ProjectSession
from hop.sway import SwayIpcAdapter, SwayWindow

EDITOR_ROLE = _EDITOR_ROLE_CONST
EDITOR_OS_WINDOW_NAME = f"hop:{EDITOR_ROLE}"

# Keystroke building blocks used to drive the in-pty editor.
# `<Esc>` (0x1b) is the prefix we send before `:drop`. Tempting to use
# `<C-\><C-n>` (the "force normal mode" idiom that bypasses user mappings)
# but its leading byte 0x1c is the tty's default `quit` control character.
# When the pty is still in cooked mode — which it is during slow command
# startup like `podman-compose exec devcontainer nvim` — the kernel
# intercepts 0x1c and sends SIGQUIT to the foreground process group,
# killing the launching process before nvim ever runs. `<Esc>` has no such
# tty significance and reliably puts a running editor into normal mode.
_NORMAL_MODE = "\x1b"
_CR = "\r"

# Default open-file keystroke templates — vim/nvim shaped. Users on other
# TUI editors override these via ``[windows.editor]`` ``open_keys`` and
# ``open_keys_with_line``. ``{path}`` substitutes the target path with
# any literal single quotes doubled (vim's single-quoted string escape);
# ``{line}`` substitutes the decimal line number.
DEFAULT_OPEN_KEYS = f"{_NORMAL_MODE}:exec 'drop '.fnameescape('{{path}}'){_CR}"
DEFAULT_OPEN_KEYS_WITH_LINE = f"{DEFAULT_OPEN_KEYS}:{{line}}{_CR}"

SessionWindowsFactory = Callable[[ProjectSession], Sequence[WindowSpec]]


class NeovimError(HopError):
    """Base error for Neovim lifecycle failures."""


class NeovimCommandError(NeovimError):
    """Raised when hop cannot control the shared Neovim instance."""


class EditorSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def focus_window(self, window_id: int) -> None: ...


class EditorTerminalAdapter(Protocol):
    def ensure_terminal(self, session: ProjectSession, *, role: str, already_prepared: bool = False) -> None: ...


class KittyEditorIO(Protocol):
    """How the editor adapter types keystrokes into the running nvim.

    Two implementations live below: ``IpcKittyEditorIO`` for the host CLI
    (synchronous unix-socket IPC against the per-session kitty), and
    ``BossKittyEditorIO`` for use inside the kitty boss event loop (kittens),
    where IPC against ourselves would deadlock the loop.
    """

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

    def send_text_to_editor(self, session: ProjectSession, text: str) -> None:
        window = self._find_editor_window()
        if window is None:
            msg = (
                f"No editor window for session {session.session_name!r} in this kitty boss. "
                "Run `hop term --role editor` from a shell first."
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
    """Routes ``hop open <file>`` into the session's editor role window.

    The editor is a plain role terminal (a shell launched with ``nvim`` typed
    in, exactly like ``server``/``console``) — there is no bespoke editor
    launch path. This adapter only *drives* the running nvim: it brings the
    editor up if it's gone, focuses it, and types the ``:drop`` open-file
    keystrokes into it.
    """

    def __init__(
        self,
        *,
        kitty_io: KittyEditorIO | None = None,
        terminals: EditorTerminalAdapter | None = None,
        sway: EditorSwayAdapter | None = None,
        session_windows_for: SessionWindowsFactory | None = None,
        editor_respawn: Callable[[ProjectSession, str], None] | None = None,
    ) -> None:
        self._kitty_io: KittyEditorIO = kitty_io or IpcKittyEditorIO()
        # The terminal adapter that launches/focuses role windows. ``None``
        # inside the kitty boss (kitten) — there we can't launch without
        # blocking the loop, so a missing editor is re-spawned out of process
        # via ``editor_respawn`` instead.
        self._terminals: EditorTerminalAdapter | None = terminals
        # Brings a missing editor up on the boss (kitten) path — a detached
        # ``hop open`` that re-spawns the editor and opens the file in its own
        # process, sidestepping the boss-loop deadlock.
        self._editor_respawn: Callable[[ProjectSession, str], None] = editor_respawn or _default_editor_respawn
        self._sway: EditorSwayAdapter = sway or SwayIpcAdapter()
        # Resolves the session's window list so ``open_target`` can pick up a
        # user override for the editor's ``open_keys``. Default returns no
        # windows so the built-in vim templates apply.
        self._session_windows_for: SessionWindowsFactory = session_windows_for or (lambda _session: ())

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        # ``target`` is in the active backend's namespace already (the kitten
        # resolves candidates against the source window's in-shell cwd via
        # OSC 7, and the open path filter runs through ``backend.paths_exist``
        # without translating namespaces). Pass it through unchanged.
        if self._terminals is not None:
            # CLI path: bring the editor up like any role terminal if it's gone.
            self._terminals.ensure_terminal(session, role=EDITOR_ROLE)
        elif not self._editor_candidates(session):
            # Boss (kitten) path with no editor: we can't launch synchronously
            # without deadlocking the boss loop, so hand the open to a detached
            # ``hop open`` that re-spawns the editor and opens the file itself.
            self._editor_respawn(session, target)
            return
        self._focus_editor(session)
        path_text, line_number = _split_target(target)
        editor_spec = find_window(self._session_windows_for(session), EDITOR_ROLE)
        open_keys = (editor_spec.open_keys if editor_spec is not None else None) or DEFAULT_OPEN_KEYS
        open_keys_with_line = (
            editor_spec.open_keys_with_line if editor_spec is not None else None
        ) or DEFAULT_OPEN_KEYS_WITH_LINE
        self._kitty_io.send_text_to_editor(
            session,
            _build_open_keystrokes(
                path_text,
                line_number,
                open_keys=open_keys,
                open_keys_with_line=open_keys_with_line,
            ),
        )

    def _focus_editor(self, session: ProjectSession) -> None:
        # Sway-driven focus (rather than Kitty's `focus-window`) so opening a
        # file from another workspace escalates to a workspace switch — e.g.
        # when the kitten dispatches a file from a terminal session.
        candidates = self._editor_candidates(session)
        if candidates:
            self._sway.focus_window(min(candidates, key=lambda window: window.id).id)

    def _editor_candidates(self, session: ProjectSession) -> list[SwayWindow]:
        # The session's editor role window(s) on `p:<session>`, matched by
        # app_id exactly like every other role (see term.py's
        # `_find_role_window`). Empty means the editor is gone.
        return [
            window
            for window in self._sway.list_windows()
            if (window.app_id == EDITOR_OS_WINDOW_NAME or window.window_class == EDITOR_OS_WINDOW_NAME)
            and window.workspace_name == session.workspace_name
        ]


def _default_editor_respawn(session: ProjectSession, target: str) -> None:
    """Re-spawn a missing editor and open ``target`` in it via a detached
    ``hop open`` — the boss (kitten) path can't launch synchronously without
    deadlocking the kitty loop. Fire-and-forget, like ``SubprocessHostOpener``:
    a fresh ``hop`` re-spawns the editor role terminal and buffers the open
    keystrokes until nvim is up. ``hop`` is on PATH in the kitty/Sway env.

    Identity is handed to the child the way an in-session ``hop`` resolves it:
    a local session through its ``cwd``, a remote one through the
    ``HOP_REMOTE_HOST`` / ``HOP_REMOTE_CWD`` env vars (``session_root`` is a
    remote path, meaningless as a local ``cwd``) — see ``remote_session_from_env``.
    """
    env = dict(os.environ)
    cwd: str | None
    if session.host is None:
        cwd = str(session.session_root)
    else:
        cwd = None
        env["HOP_REMOTE_HOST"] = session.host
        env["HOP_REMOTE_CWD"] = str(session.session_root)
    subprocess.Popen(  # noqa: S603
        ["hop", "open", target],  # noqa: S607
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


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


def _build_open_keystrokes(
    path: str,
    line_number: int | None,
    *,
    open_keys: str = DEFAULT_OPEN_KEYS,
    open_keys_with_line: str = DEFAULT_OPEN_KEYS_WITH_LINE,
) -> str:
    """Substitute ``path`` (and optionally ``line_number``) into the editor
    template.

    The path's literal single quotes are doubled before substitution so the
    default vim template (which embeds ``{path}`` inside a single-quoted vim
    string) handles paths containing ``'`` without breaking out of the
    string. The doubling is a no-op for paths without ``'``, which is the
    overwhelmingly common case; non-vim templates that don't wrap ``{path}``
    in ``'...'`` are unaffected.
    """

    quoted = path.replace("'", "''")
    if line_number is None:
        return open_keys.format(path=quoted)
    return open_keys_with_line.format(path=quoted, line=line_number)


def _split_target(target: str) -> tuple[str, int | None]:
    path_text, separator, suffix = target.rpartition(":")
    if separator and suffix.isdigit() and path_text:
        return path_text, int(suffix)
    return target, None
