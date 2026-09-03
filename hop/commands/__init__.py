from __future__ import annotations

from dataclasses import dataclass

from hop.commands.run import DEFAULT_RUN_ROLE


@dataclass(frozen=True, slots=True)
class EnterSessionCommand:
    backend: str | None = None


@dataclass(frozen=True, slots=True)
class SwitchSessionCommand:
    session_name: str


@dataclass(frozen=True, slots=True)
class MoveCommand:
    session_name: str


@dataclass(frozen=True, slots=True)
class ListSessionsCommand:
    as_json: bool = False


@dataclass(frozen=True, slots=True)
class ListWindowsCommand:
    pass


@dataclass(frozen=True, slots=True)
class OpenCommand:
    target: str


@dataclass(frozen=True, slots=True)
class TermCommand:
    role: str


@dataclass(frozen=True, slots=True)
class RunCommand:
    command_text: str
    role: str = DEFAULT_RUN_ROLE
    focus: bool = False
    wait: bool = False


@dataclass(frozen=True, slots=True)
class TailCommand:
    run_id: str


@dataclass(frozen=True, slots=True)
class InstallCommand:
    """`hop install --agents` — install the hop-run skill for Claude Code and
    Codex. `hop install` is the umbrella for local integration installs; it
    takes a target flag (only `--agents` today) and errors without one."""


@dataclass(frozen=True, slots=True)
class BrowserCommand:
    url: str | None = None


@dataclass(frozen=True, slots=True)
class KillCommand:
    pass


@dataclass(frozen=True, slots=True)
class BridgeShimCommand:
    socket: str | None = None


@dataclass(frozen=True, slots=True)
class PathCommand:
    name: str


@dataclass(frozen=True, slots=True)
class SshCommand:
    host: str


Command = (
    EnterSessionCommand
    | SwitchSessionCommand
    | MoveCommand
    | ListSessionsCommand
    | ListWindowsCommand
    | OpenCommand
    | TermCommand
    | RunCommand
    | TailCommand
    | InstallCommand
    | BrowserCommand
    | KillCommand
    | BridgeShimCommand
    | PathCommand
    | SshCommand
)
