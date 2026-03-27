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
from hop.session import ProjectSession, derive_workspace_name, resolve_project_session


class HopError(RuntimeError):
    """Base error for hop command execution."""


class IntegrationNotImplementedError(HopError):
    """Raised when a command reaches an integration scaffold that is not wired yet."""


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
            session = resolve_project_session(current_directory)
            services.sway.switch_to_workspace(session.workspace_name)
            services.kitty.ensure_terminal(session, role="shell")
        case SwitchSessionCommand(session_name=session_name):
            services.sway.switch_to_workspace(derive_workspace_name(session_name))
        case ListSessionsCommand():
            for workspace in services.sway.list_session_workspaces():
                print(workspace.removeprefix("p:"))
        case EditCommand(target=target):
            session = _switch_to_current_session(current_directory, services=services)
            if target is None:
                services.neovim.focus(session)
            else:
                services.neovim.open_target(session, target=target)
        case TermCommand(role=role):
            session = _switch_to_current_session(current_directory, services=services)
            services.kitty.ensure_terminal(session, role=role)
        case RunCommand(role=role, command_text=command_text):
            session = _switch_to_current_session(current_directory, services=services)
            services.kitty.run_in_terminal(session, role=role, command=command_text)
        case BrowserCommand(url=url):
            session = _switch_to_current_session(current_directory, services=services)
            services.browser.ensure_browser(session, url=url)

    return 0


def build_default_services() -> HopServices:
    return HopServices(
        sway=_MissingSwayAdapter(),
        kitty=_MissingKittyAdapter(),
        neovim=_MissingNeovimAdapter(),
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


class _MissingSwayAdapter:
    def switch_to_workspace(self, workspace_name: str) -> None:
        raise IntegrationNotImplementedError(
            f"Sway workspace switching is not implemented yet for {workspace_name!r}."
        )

    def list_session_workspaces(self, *, prefix: str = "p:") -> Sequence[str]:
        raise IntegrationNotImplementedError(
            f"Sway workspace listing is not implemented yet for prefix {prefix!r}."
        )


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


class _MissingNeovimAdapter:
    def focus(self, session: ProjectSession) -> None:
        raise IntegrationNotImplementedError(
            f"Neovim focus is not implemented yet for {session.session_name!r}."
        )

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        raise IntegrationNotImplementedError(
            f"Neovim target opening is not implemented yet for {session.session_name!r}:{target!r}."
        )


class _MissingBrowserAdapter:
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        raise IntegrationNotImplementedError(
            f"Browser integration is not implemented yet for {session.session_name!r}."
        )
