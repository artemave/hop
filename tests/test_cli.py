import pytest

from hop.cli import parse_command
from hop.commands import (
    BrowserCommand,
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


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], EnterSessionCommand()),
        (["switch", "demo"], SwitchSessionCommand(session_name="demo")),
        (["list"], ListSessionsCommand()),
        (["list", "--json"], ListSessionsCommand(as_json=True)),
        (["windows"], ListWindowsCommand()),
        (["edit"], EditCommand()),
        (["edit", "app/models/user.rb:42"], EditCommand(target="app/models/user.rb:42")),
        (["term"], EnterSessionCommand()),
        (["term", "--role", "test"], TermCommand(role="test")),
        (["run", "ls"], RunCommand(command_text="ls")),
        (
            ["run", "--role", "server", "bundle exec rails server"],
            RunCommand(role="server", command_text="bundle exec rails server"),
        ),
        (["run", "--focus", "ls"], RunCommand(command_text="ls", focus=True)),
        (
            ["run", "--role", "server", "--focus", "bin/dev"],
            RunCommand(role="server", command_text="bin/dev", focus=True),
        ),
        (
            ["run", "--focus", "--role", "server", "bin/dev"],
            RunCommand(role="server", command_text="bin/dev", focus=True),
        ),
        (["tail", "abc123"], TailCommand(run_id="abc123")),
        (["browser"], BrowserCommand()),
        (["browser", "https://example.com"], BrowserCommand(url="https://example.com")),
        (["kill"], KillCommand()),
    ],
)
def test_parse_command_maps_argv_to_typed_commands(argv: list[str], expected: object) -> None:
    assert parse_command(argv) == expected


def test_run_defaults_to_shell_role() -> None:
    command = parse_command(["run", "pytest -q"])

    assert command == RunCommand(command_text="pytest -q", role="shell")


def test_backend_flag_on_bare_hop() -> None:
    assert parse_command(["--backend", "devcontainer"]) == EnterSessionCommand(backend="devcontainer")


def test_backend_host_on_bare_hop() -> None:
    assert parse_command(["--backend", "host"]) == EnterSessionCommand(backend="host")


def test_backend_flag_on_bare_term_alias() -> None:
    assert parse_command(["--backend", "devcontainer", "term"]) == EnterSessionCommand(backend="devcontainer")


def test_backend_flag_rejected_on_term_with_role() -> None:
    with pytest.raises(ValueError, match="--backend"):
        parse_command(["--backend", "devcontainer", "term", "--role", "test"])


@pytest.mark.parametrize(
    "argv",
    [
        ["--backend", "host", "switch", "demo"],
        ["--backend", "host", "list"],
        ["--backend", "host", "edit"],
        ["--backend", "host", "run", "ls"],
        ["--backend", "host", "tail", "abc"],
        ["--backend", "host", "browser"],
        ["--backend", "host", "kill"],
    ],
)
def test_backend_flag_rejected_on_other_subcommands(argv: list[str]) -> None:
    with pytest.raises(ValueError, match="--backend"):
        parse_command(argv)


# --- hopd version-mismatch hint -------------------------------------------


def test_warn_if_hopd_version_stale_prints_note_on_mismatch(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hop import cli
    from hop.daemon_lock import HopdStatus

    monkeypatch.setattr(cli, "read_status", lambda: HopdStatus(pid=999, version="0.0.1"))
    monkeypatch.setattr(cli, "installed_version", lambda: "0.0.2")

    cli._warn_if_hopd_version_stale()  # pyright: ignore[reportPrivateUsage]

    stderr = capsys.readouterr().err
    assert "0.0.1" in stderr
    assert "0.0.2" in stderr
    assert "hopd --restart" in stderr


def test_warn_if_hopd_version_stale_silent_when_versions_match(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hop import cli
    from hop.daemon_lock import HopdStatus

    monkeypatch.setattr(cli, "read_status", lambda: HopdStatus(pid=999, version="1.2.3"))
    monkeypatch.setattr(cli, "installed_version", lambda: "1.2.3")

    cli._warn_if_hopd_version_stale()  # pyright: ignore[reportPrivateUsage]

    assert capsys.readouterr().err == ""


def test_warn_if_hopd_version_stale_silent_when_no_status_file(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No running hopd → nothing to warn about. Plenty of hop users don't
    run the vicinae integration at all."""
    from hop import cli

    monkeypatch.setattr(cli, "read_status", lambda: None)

    cli._warn_if_hopd_version_stale()  # pyright: ignore[reportPrivateUsage]

    assert capsys.readouterr().err == ""
