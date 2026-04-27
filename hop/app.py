from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

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
from hop.editor import SharedNeovimEditorAdapter
from hop.kitty import KittyRemoteControlAdapter, KittyWindow, KittyWindowContext, KittyWindowState
from hop.session import ProjectSession, resolve_project_session
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

    def inspect_window(self, window_id: int) -> KittyWindowContext | None: ...

    def list_session_windows(self, session: ProjectSession) -> Sequence[KittyWindow]: ...

    def close_window(self, session_name: str, window_id: int) -> None: ...

    def get_window_state(self, session_name: str, window_id: int) -> KittyWindowState: ...

    def get_last_cmd_output(self, session_name: str, window_id: int) -> str: ...


class NeovimAdapter(Protocol):
    def focus(self, session: ProjectSession) -> None: ...

    def open_target(self, session: ProjectSession, *, target: str) -> None: ...


class BrowserAdapter(Protocol):
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None: ...


@dataclass(frozen=True, slots=True)
class HopServices:
    sway: SwayAdapter
    kitty: KittyAdapter
    neovim: NeovimAdapter
    browser: BrowserAdapter


def execute_command(
    command: Command,
    *,
    cwd: Path | str,
    services: HopServices,
) -> int:
    current_directory = Path(cwd).expanduser().resolve()

    match command:
        case EnterSessionCommand():
            session = resolve_project_session(current_directory)
            if services.sway.get_focused_workspace() == session.workspace_name:
                spawn_session_terminal(
                    current_directory,
                    terminals=services.kitty,
                )
            else:
                enter_project_session(
                    current_directory,
                    sway=services.sway,
                    terminals=services.kitty,
                )
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
                kitty=services.kitty,
            )
        case _:
            msg = f"Unsupported command {command!r}"
            raise ValueError(msg)

    return 0


def build_default_services() -> HopServices:
    from hop.state import record_session

    sway = SwayIpcAdapter()
    return HopServices(
        sway=sway,
        kitty=KittyRemoteControlAdapter(on_session_bootstrap=record_session),
        neovim=SharedNeovimEditorAdapter(),
        browser=SessionBrowserAdapter(sway=sway),
    )
