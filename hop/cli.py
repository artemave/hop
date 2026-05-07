from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from hop import debug
from hop.app import build_default_services, execute_command
from hop.commands import (
    BrowserCommand,
    Command,
    EditCommand,
    EnterSessionCommand,
    KillCommand,
    ListSessionsCommand,
    ListWindowsCommand,
    RunCommand,
    SwitchSessionCommand,
    TailCommand,
    TermCommand,
)
from hop.commands.run import DEFAULT_RUN_ROLE
from hop.config import load_global_config
from hop.errors import HopError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hop")
    parser.add_argument(
        "--backend",
        default=None,
        dest="backend",
        metavar="NAME",
        help=(
            "Pin a specific backend when creating a new session "
            "(only valid without a subcommand). Use 'host' to opt out of auto-detect."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    switch_parser = subparsers.add_parser("switch")
    switch_parser.add_argument("session_name")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true", dest="as_json")

    subparsers.add_parser("windows")

    edit_parser = subparsers.add_parser("edit")
    edit_parser.add_argument("target", nargs="?")

    term_parser = subparsers.add_parser("term")
    term_parser.add_argument("--role", default=None)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--role", default=DEFAULT_RUN_ROLE)
    run_parser.add_argument("--focus", action="store_true", dest="focus")
    run_parser.add_argument("command_text")

    tail_parser = subparsers.add_parser("tail")
    tail_parser.add_argument("run_id")

    browser_parser = subparsers.add_parser("browser")
    browser_parser.add_argument("url", nargs="?")

    subparsers.add_parser("kill")

    return parser


def parse_command(argv: Sequence[str] | None = None) -> Command:
    namespace = build_parser().parse_args(list(argv) if argv is not None else None)
    backend: str | None = getattr(namespace, "backend", None)

    if backend is not None and namespace.command not in (None, "term"):
        raise ValueError("--backend is only valid without a subcommand")

    match namespace.command:
        case None:
            return EnterSessionCommand(backend=backend)
        case "switch":
            return SwitchSessionCommand(session_name=namespace.session_name)
        case "list":
            return ListSessionsCommand(as_json=bool(namespace.as_json))
        case "windows":
            return ListWindowsCommand()
        case "edit":
            return EditCommand(target=namespace.target)
        case "term":
            if namespace.role is None:
                return EnterSessionCommand(backend=backend)
            if backend is not None:
                raise ValueError("--backend is only valid without a subcommand")
            return TermCommand(role=namespace.role)
        case "run":
            return RunCommand(
                role=namespace.role,
                command_text=namespace.command_text,
                focus=bool(namespace.focus),
            )
        case "tail":
            return TailCommand(run_id=namespace.run_id)
        case "browser":
            return BrowserCommand(url=namespace.url)
        case "kill":
            return KillCommand()
        case command_name:
            msg = f"Unsupported command {command_name!r}"
            raise ValueError(msg)


def main(argv: Sequence[str] | None = None) -> int:
    command = parse_command(argv)

    try:
        debug.configure(load_global_config().debug_log)
        return execute_command(
            command,
            cwd=Path.cwd(),
            services=build_default_services(),
        )
    except HopError as error:
        print(str(error), file=sys.stderr)
        return 1
