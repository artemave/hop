from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from hop.backends import (
    CommandBackend,
    CommandRunner,
    HostBackend,
    SessionBackend,
    backend_from_config,
    select_backend,
)
from hop.browser import SessionBrowserAdapter
from hop.commands import (
    BrowserCommand,
    Command,
    EditCommand,
    EnterSessionCommand,
    KillCommand,
    ListSessionsCommand,
    RunCommand,
    SwitchSessionCommand,
    TailCommand,
    TermCommand,
)
from hop.commands.browser import focus_browser
from hop.commands.edit import edit_in_session
from hop.commands.kill import kill_session
from hop.commands.run import run_command
from hop.commands.session import (
    enter_project_session,
    list_sessions,
    spawn_session_terminal,
    switch_session,
)
from hop.commands.tail import tail_command
from hop.commands.term import focus_terminal
from hop.config import (
    HopConfig,
    load_global_config,
    load_project_config,
    merge_backends,
    merge_configs,
)
from hop.layouts import WindowSpec, resolve_windows
from hop.editor import SharedNeovimEditorAdapter
from hop.kitty import KittyRemoteControlAdapter, KittyWindow, KittyWindowContext, KittyWindowState
from hop.session import ProjectSession, resolve_project_session
from hop.state import (
    BackendRecord,
    CommandBackendRecord,
    HostBackendRecord,
    SessionState,
    load_sessions,
    record_session,
)
from hop.sway import SwayIpcAdapter, SwayWindow


class SwayAdapter(Protocol):
    def switch_to_workspace(self, workspace_name: str) -> None: ...

    def list_session_workspaces(self, *, prefix: str = "p:") -> Sequence[str]: ...

    def list_windows(self) -> Sequence[SwayWindow]: ...

    def close_window(self, window_id: int) -> None: ...

    def remove_workspace(self, workspace_name: str) -> None: ...

    def get_focused_workspace(self) -> str: ...


class KittyAdapter(Protocol):
    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None: ...

    def run_in_terminal(
        self,
        session: ProjectSession,
        *,
        role: str,
        command: str,
    ) -> int: ...

    def inspect_window(self, window_id: int, *, listen_on: str | None = None) -> KittyWindowContext | None: ...

    def list_session_windows(self, session: ProjectSession) -> Sequence[KittyWindow]: ...

    def close_window(self, session_name: str, window_id: int) -> None: ...

    def get_window_state(self, session_name: str, window_id: int) -> KittyWindowState: ...

    def get_last_cmd_output(self, session_name: str, window_id: int) -> str: ...


class NeovimAdapter(Protocol):
    def ensure(self, session: ProjectSession) -> bool: ...

    def focus(self, session: ProjectSession) -> None: ...

    def open_target(self, session: ProjectSession, *, target: str) -> None: ...


class BrowserAdapter(Protocol):
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None: ...


SessionBackendFactory = Callable[[ProjectSession], SessionBackend]


class SessionBackendRegistry:
    """Resolves the SessionBackend for a given session.

    Lookup order:
      1. In-process override set by execute_command for the duration of an
         EnterSessionCommand (so first-entry resolution flows through to the
         kitty adapter without changing every adapter signature).
      2. Persisted session state at <sessions_dir>/<name>.json (set by
         record_session at bootstrap time).
      3. Otherwise fall back to host (no auto-detect outside session entry).

    All adapters that need a backend accept a
    ``Callable[[ProjectSession], SessionBackend]`` and call ``registry.for_session``.
    """

    def __init__(
        self,
        *,
        global_config_loader: Callable[[], HopConfig] = load_global_config,
        sessions_loader: Callable[[], dict[str, SessionState]] = load_sessions,
        runner: CommandRunner | None = None,
    ) -> None:
        self._global_config_loader = global_config_loader
        self._sessions_loader = sessions_loader
        self._runner = runner
        self._overrides: dict[str, SessionBackend] = {}

    def has_persisted_state(self, session: ProjectSession) -> bool:
        return self._sessions_loader().get(session.session_name) is not None

    def for_session(self, session: ProjectSession) -> SessionBackend:
        override = self._overrides.get(session.session_name)
        if override is not None:
            return override

        persisted = self._sessions_loader().get(session.session_name)
        if persisted is not None:
            return _backend_from_record(persisted.backend)

        # No persisted state and no in-process override: a command running
        # against a session that hop never bootstrapped (e.g. someone calling
        # hop run from a workspace created by hand). Fall back to host.
        return HostBackend()

    def resolve_for_entry(self, session: ProjectSession, *, backend_name: str | None) -> SessionBackend:
        # Persisted state still wins so we don't change a live session's
        # backend mid-flight; backend_name only matters for first entry.
        persisted = self._sessions_loader().get(session.session_name)
        if persisted is not None:
            return _backend_from_record(persisted.backend)

        configured = self._merged_config(session).backends

        if self._runner is not None:
            chosen = select_backend(
                session,
                configured,
                pinned_name=backend_name,
                runner=self._runner,
            )
        else:
            chosen = select_backend(
                session,
                configured,
                pinned_name=backend_name,
            )
        if chosen is None:
            return HostBackend()
        backend = (
            backend_from_config(chosen, runner=self._runner)
            if self._runner is not None
            else backend_from_config(chosen)
        )
        # Prepare the backend (e.g. compose up -d) and discover the workspace
        # path before any role terminals are launched. Both happen here so the
        # backend persisted in session state already carries workspace_path;
        # the bootstrap path doesn't have to repeat the discovery.
        backend.prepare(session)
        workspace_path = backend.discover_workspace(session)
        return backend.with_workspace_path(workspace_path)

    def resolve_windows_for_entry(self, session: ProjectSession) -> tuple[WindowSpec, ...]:
        from hop.backends import _default_runner  # local import to avoid cycle at module load.

        merged = self._merged_config(session)
        runner = self._runner if self._runner is not None else _default_runner
        return resolve_windows(merged, session, runner=runner)

    def _merged_config(self, session: ProjectSession) -> HopConfig:
        global_config = self._global_config_loader()
        project_config = load_project_config(session.project_root)
        return merge_configs(project_config, global_config)

    def set_override(self, session_name: str, backend: SessionBackend) -> None:
        self._overrides[session_name] = backend

    def clear_override(self, session_name: str) -> None:
        self._overrides.pop(session_name, None)


def _backend_from_record(record: BackendRecord) -> SessionBackend:
    if isinstance(record, CommandBackendRecord):
        return CommandBackend(
            name=record.name,
            command_prefix=record.command_prefix,
            prepare_command=record.prepare,
            teardown_command=record.teardown,
            workspace_command=record.workspace_command,
            workspace_path=record.workspace_path,
            port_translate_command=record.port_translate_command,
            host_translate_command=record.host_translate_command,
        )
    return HostBackend()


def _record_for_backend(backend: SessionBackend) -> BackendRecord:
    if isinstance(backend, CommandBackend):
        return CommandBackendRecord(
            name=backend.name,
            command_prefix=backend.command_prefix,
            prepare=backend.prepare_command,
            teardown=backend.teardown_command,
            workspace_command=backend.workspace_command,
            workspace_path=backend.workspace_path,
            port_translate_command=backend.port_translate_command,
            host_translate_command=backend.host_translate_command,
        )
    return HostBackendRecord()


@dataclass(frozen=True, slots=True)
class HopServices:
    sway: SwayAdapter
    kitty: KittyAdapter
    neovim: NeovimAdapter
    browser: BrowserAdapter
    session_backends: SessionBackendRegistry


def execute_command(
    command: Command,
    *,
    cwd: Path | str,
    services: HopServices,
) -> int:
    current_directory = Path(cwd).expanduser().resolve()

    match command:
        case EnterSessionCommand(backend=backend_name):
            session = resolve_project_session(current_directory)
            if services.sway.get_focused_workspace() == session.workspace_name:
                # Spawning an additional terminal in an already-live session:
                # the backend is fixed at session creation; --backend is ignored.
                # An editor is ensured alongside so a closed editor comes back
                # on the next `hop`.
                spawn_session_terminal(
                    current_directory,
                    terminals=services.kitty,
                    editor=services.neovim,
                )
            else:
                # First entry creates both shell and editor; re-entry from
                # another workspace just switches and ensures the shell —
                # we don't second-guess a deliberately-closed editor on
                # every `hop`.
                is_first_entry = not services.session_backends.has_persisted_state(session)
                backend = services.session_backends.resolve_for_entry(session, backend_name=backend_name)
                services.session_backends.set_override(session.session_name, backend)
                try:
                    windows = (
                        services.session_backends.resolve_windows_for_entry(session)
                        if is_first_entry
                        else ()
                    )
                    enter_project_session(
                        current_directory,
                        sway=services.sway,
                        terminals=services.kitty,
                        editor=services.neovim if is_first_entry else None,
                        browser=services.browser if is_first_entry else None,
                        windows=windows,
                    )
                finally:
                    services.session_backends.clear_override(session.session_name)
        case SwitchSessionCommand(session_name=session_name):
            switch_session(session_name, sway=services.sway)
        case ListSessionsCommand(as_json=as_json):
            listings = list_sessions(sway=services.sway)
            if as_json:
                payload = [
                    {
                        "name": listing.name,
                        "workspace": listing.workspace,
                        "project_root": str(listing.project_root) if listing.project_root else None,
                    }
                    for listing in listings
                ]
                print(json.dumps(payload, indent=2))
            else:
                for listing in listings:
                    print(listing.name)
        case EditCommand(target=target):
            edit_in_session(
                current_directory,
                neovim=services.neovim,
                target=target,
            )
        case TermCommand(role=role):
            focus_terminal(
                current_directory,
                terminals=services.kitty,
                role=role,
            )
        case RunCommand(role=role, command_text=command_text):
            dispatch = run_command(
                current_directory,
                terminals=services.kitty,
                role=role,
                command=command_text,
            )
            print(dispatch.run_id)
        case TailCommand(run_id=run_id):
            output = tail_command(run_id, kitty=services.kitty)
            sys.stdout.write(output)
        case BrowserCommand(url=url):
            focus_browser(
                current_directory,
                browser=services.browser,
                url=url,
            )
        case KillCommand():
            kill_session(
                current_directory,
                sway=services.sway,
                session_backend_for=services.session_backends.for_session,
            )
        case _:
            msg = f"Unsupported command {command!r}"
            raise ValueError(msg)

    return 0


def _persist_bootstrap_record(session: ProjectSession, backend: SessionBackend) -> None:
    record_session(session, backend=_record_for_backend(backend))


def build_default_services() -> HopServices:
    sway = SwayIpcAdapter()
    registry = SessionBackendRegistry()
    kitty = KittyRemoteControlAdapter(
        session_backend_for=registry.for_session,
        session_windows_for=registry.resolve_windows_for_entry,
        on_session_bootstrap=_persist_bootstrap_record,
    )
    neovim = SharedNeovimEditorAdapter(
        sway=sway,
        session_backend_for=registry.for_session,
        session_windows_for=registry.resolve_windows_for_entry,
    )
    return HopServices(
        sway=sway,
        kitty=kitty,
        neovim=neovim,
        browser=SessionBrowserAdapter(sway=sway, session_windows_for=registry.resolve_windows_for_entry),
        session_backends=registry,
    )


def build_kitten_services(boss: object) -> HopServices:
    """Same as ``build_default_services`` but wires the editor adapter to
    talk to kitty through the boss API directly. Use only from inside the
    kitty boss event loop (handle_result of a kitten) — synchronous IPC
    against the same kitty would deadlock the loop while the kitten runs.
    """
    from hop.editor import BossKittyEditorIO

    sway = SwayIpcAdapter()
    registry = SessionBackendRegistry()
    kitty = KittyRemoteControlAdapter(
        session_backend_for=registry.for_session,
        session_windows_for=registry.resolve_windows_for_entry,
        on_session_bootstrap=_persist_bootstrap_record,
    )
    neovim = SharedNeovimEditorAdapter(
        sway=sway,
        kitty_io=BossKittyEditorIO(boss),
        session_backend_for=registry.for_session,
        session_windows_for=registry.resolve_windows_for_entry,
    )
    return HopServices(
        sway=sway,
        kitty=kitty,
        neovim=neovim,
        browser=SessionBrowserAdapter(sway=sway, session_windows_for=registry.resolve_windows_for_entry),
        session_backends=registry,
    )
