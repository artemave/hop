import pytest

from hop.cli import parse_command
from hop.commands import (
    BrowserCommand,
    EditCommand,
    EnterSessionCommand,
    ListSessionsCommand,
    RunCommand,
    SwitchSessionCommand,
    TermCommand,
)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], EnterSessionCommand()),
        (["switch", "demo"], SwitchSessionCommand(session_name="demo")),
        (["list"], ListSessionsCommand()),
        (["edit"], EditCommand()),
        (["edit", "app/models/user.rb:42"], EditCommand(target="app/models/user.rb:42")),
        (["term", "--role", "test"], TermCommand(role="test")),
        (["run", "ls"], RunCommand(command_text="ls")),
        (
            ["run", "--role", "server", "bundle exec rails server"],
            RunCommand(role="server", command_text="bundle exec rails server"),
        ),
        (["browser"], BrowserCommand()),
        (["browser", "https://example.com"], BrowserCommand(url="https://example.com")),
    ],
)
def test_parse_command_maps_argv_to_typed_commands(argv: list[str], expected: object) -> None:
    assert parse_command(argv) == expected


def test_run_defaults_to_shell_role() -> None:
    command = parse_command(["run", "pytest -q"])

    assert command == RunCommand(command_text="pytest -q", role="shell")
