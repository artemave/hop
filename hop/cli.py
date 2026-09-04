from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from hop import debug
from hop.app import build_default_services, execute_command
from hop.commands import (
    BridgeShimCommand,
    BrowserCommand,
    Command,
    EnterSessionCommand,
    KillCommand,
    ListSessionsCommand,
    ListWindowsCommand,
    MoveCommand,
    OpenCommand,
    PathCommand,
    RunCommand,
    SshCommand,
    SwitchSessionCommand,
    TailCommand,
    TermCommand,
    TrustCommand,
)
from hop.commands.run import DEFAULT_RUN_ROLE
from hop.config import load_global_config
from hop.daemon_lock import installed_version, read_status
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

    move_parser = subparsers.add_parser("move")
    move_parser.add_argument("session_name")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true", dest="as_json")

    subparsers.add_parser("windows")

    open_parser = subparsers.add_parser("open")
    open_parser.add_argument("target")

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

    path_parser = subparsers.add_parser(
        "path",
        help="Print the absolute path to a bundled hop asset (e.g. kitten/hints, sway/term-or-kitty).",
    )
    path_parser.add_argument("name")

    ssh_parser = subparsers.add_parser(
        "ssh",
        help="Set up the ssh transport to a remote host, then drop into a shell where `hop` runs sessions there.",
    )
    ssh_parser.add_argument("host")

    trust_parser = subparsers.add_parser(
        "trust",
        help="Trust the current directory's .hop.toml so hop will run its commands.",
    )
    trust_group = trust_parser.add_mutually_exclusive_group()
    trust_group.add_argument(
        "--list",
        action="store_true",
        dest="trust_list",
        help="List trusted .hop.toml files.",
    )
    trust_group.add_argument(
        "--revoke",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Revoke trust for the current directory's .hop.toml, or an explicit PATH.",
    )

    bridge_parser = subparsers.add_parser("bridge")
    bridge_subparsers = bridge_parser.add_subparsers(dest="bridge_command", required=True)
    bridge_shim_parser = bridge_subparsers.add_parser("shim", help="Print the POSIX-sh bridge client to stdout.")
    bridge_shim_parser.add_argument(
        "--socket",
        default=None,
        metavar="PATH",
        help=(
            "Default socket path baked into the printed shim. Overridden at run time "
            "by $HOP_SOCKET. Defaults to /run/hop.sock."
        ),
    )

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
        case "move":
            return MoveCommand(session_name=namespace.session_name)
        case "list":
            return ListSessionsCommand(as_json=bool(namespace.as_json))
        case "windows":
            return ListWindowsCommand()
        case "open":
            return OpenCommand(target=namespace.target)
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
        case "bridge":
            # argparse enforces ``bridge_command == "shim"`` via the
            # ``required=True`` subparser + the single ``shim`` choice.
            return BridgeShimCommand(socket=namespace.socket)
        case "path":
            return PathCommand(name=namespace.name)
        case "ssh":
            return SshCommand(host=namespace.host)
        case "trust":
            if namespace.trust_list:
                return TrustCommand(mode="list")
            if namespace.revoke is not None:
                return TrustCommand(mode="revoke", path=namespace.revoke or None)
            return TrustCommand(mode="trust")
        case command_name:
            msg = f"Unsupported command {command_name!r}"
            raise ValueError(msg)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    # Internal entrypoint: the lifecycle popup window runs `hop __run-lifecycle
    # <spec>` (see hop.popup.run_popup_lifecycle). It's self-contained — it needs
    # no services and must not fall through to the normal session flow — so it's
    # intercepted before parsing and building services.
    if args[:1] == ["__run-lifecycle"]:
        from hop.popup import run_popup_lifecycle

        return run_popup_lifecycle(Path(args[1]))

    # Internal entrypoint: the clipboard-paste kitten launches `hop __paste-image
    # <session> <write-path> <mime>` via kitty's `run_background_process`, so the
    # `wl-paste` read and the (possibly slow, ssh-bound) backend write happen off
    # the kitty boss thread. Self-contained; needs no services.
    if args[:1] == ["__paste-image"]:
        from hop.paste import run_paste_helper

        return run_paste_helper(session_name=args[1], write_path=args[2], mime=args[3])

    if args[:1] == ["__trust-prompt"]:
        from hop import trust_prompt

        config_path = args[1]
        content = Path(args[2]).read_text()
        return 0 if trust_prompt.ask(config_path, content) else 1

    command = parse_command(argv)
    _warn_if_hopd_version_stale()

    services = build_default_services()
    try:
        debug.configure(load_global_config().debug_log)
        debug.log_invocation(args)
        return execute_command(
            command,
            cwd=Path.cwd(),
            services=services,
        )
    except HopError as error:
        print(str(error), file=sys.stderr)
        # Headless callers (vicinae's `setsid -f hop`, sway keybindings,
        # `nohup hop &`) have no terminal watching the stderr print — surface
        # the error in a kitten panel so it's actually visible. Lifecycle
        # popups already show their own command's failure inside the panel
        # via the held-open shell; their `SessionBackendError` carries
        # `surfaced_by_popup=True` so we don't pop a redundant second panel.
        if not error.surfaced_by_popup and not services.popup.is_interactive():
            services.popup.show_error(error)
        return 1


def _warn_if_hopd_version_stale() -> None:
    """If a hopd is running an older hop version than the CLI, hint to the
    user that they should restart it. The vicinae script set and any
    behavior changes baked into the daemon won't apply until then."""
    status = read_status()
    if status is None:
        # No daemon running, or status file unreadable — nothing to warn
        # about. The CLI works fine without hopd; some users may not use
        # the vicinae integration at all.
        return
    current = installed_version()
    if status.version == current:
        return
    print(
        f"note: hopd is running an older hop version ({status.version} → {current}); "
        "run `hopd --restart` to apply the upgrade",
        file=sys.stderr,
    )
