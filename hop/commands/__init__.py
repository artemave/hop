from __future__ import annotations

from dataclasses import dataclass

from hop.commands.run import DEFAULT_RUN_ROLE


@dataclass(frozen=True, slots=True)
class EnterSessionCommand:
    pass


@dataclass(frozen=True, slots=True)
class SwitchSessionCommand:
    session_name: str


@dataclass(frozen=True, slots=True)
class ListSessionsCommand:
    pass


@dataclass(frozen=True, slots=True)
class EditCommand:
    target: str | None = None


@dataclass(frozen=True, slots=True)
class TermCommand:
    role: str


@dataclass(frozen=True, slots=True)
class RunCommand:
    command_text: str
    role: str = DEFAULT_RUN_ROLE


@dataclass(frozen=True, slots=True)
class BrowserCommand:
    url: str | None = None


Command = (
    EnterSessionCommand
    | SwitchSessionCommand
    | ListSessionsCommand
    | EditCommand
    | TermCommand
    | RunCommand
    | BrowserCommand
)
