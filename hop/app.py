from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from hop.commands import (
    BrowserCommand,
    Command,
    EditCommand,
    EnterSessionCommand,
    ListSessionsCommand,
    RunCommand,
    SwitchSessionCommand,
    TermCommand,
)
from hop.commands.edit import edit_in_session
from hop.commands.run import run_command
from hop.commands.session import enter_project_session, list_sessions, switch_session
from hop.commands.term import focus_terminal
from hop.editor import SharedNeovimEditorAdapter
from hop.errors import HopError, IntegrationNotImplementedError
from hop.kitty import KittyRemoteControlAdapter
from hop.session import ProjectSession, resolve_project_session
from hop.sway import SwayIpcAdapter

class SwayAdapter(Protocol):
    def switch_to_workspace(self, workspace_name: str) -> None: ...

    def list_session_workspaces(self, *, prefix: str = "p:") -> Sequence[str]: ...


class KittyAdapter(Protocol):
    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None: ...

    def run_in_terminal(
        self,
        session: ProjectSession,
        *,
        role: str,
        command: str,
    ) -> None: ...


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
            enter_project_session(
                current_directory,
                sway=services.sway,
                terminals=services.kitty,
            )
        case SwitchSessionCommand(session_name=session_name):
            switch_session(session_name, sway=services.sway)
        case ListSessionsCommand():
            for session_name in list_sessions(sway=services.sway):
                print(session_name)
        case EditCommand(target=target):
            edit_in_session(
                current_directory,
                sway=services.sway,
                neovim=services.neovim,
                target=target,
            )
        case TermCommand(role=role):
            focus_terminal(
                current_directory,
                sway=services.sway,
                terminals=services.kitty,
                role=role,
            )
        case RunCommand(role=role, command_text=command_text):
            run_command(
                current_directory,
                sway=services.sway,
                terminals=services.kitty,
                role=role,
                command=command_text,
            )
        case BrowserCommand(url=url):
            session = _switch_to_current_session(current_directory, services=services)
            services.browser.ensure_browser(session, url=url)

    return 0


def build_default_services() -> HopServices:
    return HopServices(
        sway=SwayIpcAdapter(),
        kitty=KittyRemoteControlAdapter(),
        neovim=SharedNeovimEditorAdapter(),
        browser=_MissingBrowserAdapter(),
    )


def _switch_to_current_session(
    cwd: Path,
    *,
    services: HopServices,
) -> ProjectSession:
    session = resolve_project_session(cwd)
    services.sway.switch_to_workspace(session.workspace_name)
    return session


class _MissingKittyAdapter:
    def ensure_terminal(self, session: ProjectSession, *, role: str) -> None:
        raise IntegrationNotImplementedError(
            f"Kitty terminal routing is not implemented yet for {session.session_name!r}:{role!r}."
        )

    def run_in_terminal(
        self,
        session: ProjectSession,
        *,
        role: str,
        command: str,
    ) -> None:
        raise IntegrationNotImplementedError(
            f"Kitty command dispatch is not implemented yet for {session.session_name!r}:{role!r}."
        )


class _MissingBrowserAdapter:
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        raise IntegrationNotImplementedError(
            f"Browser integration is not implemented yet for {session.session_name!r}."
        )
