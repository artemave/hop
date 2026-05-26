import pytest

from hop.cli import parse_command
from hop.commands import (
    BridgeShimCommand,
    BrowserCommand,
    EnterSessionCommand,
    KillCommand,
    ListSessionsCommand,
    ListWindowsCommand,
    MoveCommand,
    OpenCommand,
    PathCommand,
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
        (["move", "demo"], MoveCommand(session_name="demo")),
        (["list"], ListSessionsCommand()),
        (["list", "--json"], ListSessionsCommand(as_json=True)),
        (["windows"], ListWindowsCommand()),
        (["open", "app/models/user.rb:42"], OpenCommand(target="app/models/user.rb:42")),
        (["open", "UsersController#index"], OpenCommand(target="UsersController#index")),
        (["open", "https://example.com"], OpenCommand(target="https://example.com")),
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
        (["bridge", "shim"], BridgeShimCommand()),
        (
            ["bridge", "shim", "--socket", "/run/user/1000/hop/api.sock"],
            BridgeShimCommand(socket="/run/user/1000/hop/api.sock"),
        ),
        (["path", "kitten/hints"], PathCommand(name="kitten/hints")),
        (["path", "sway/term-or-kitty"], PathCommand(name="sway/term-or-kitty")),
    ],
)
def test_parse_command_maps_argv_to_typed_commands(argv: list[str], expected: object) -> None:
    assert parse_command(argv) == expected


def test_run_defaults_to_shell_role() -> None:
    command = parse_command(["run", "pytest -q"])

    assert command == RunCommand(command_text="pytest -q", role="shell")


def test_hop_open_requires_target() -> None:
    # No-arg `hop open` is gone — focusing the editor lives on
    # `hop term --role editor`. argparse emits its standard "the following
    # arguments are required" message and exits non-zero.
    with pytest.raises(SystemExit):
        parse_command(["open"])


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
        ["--backend", "host", "move", "demo"],
        ["--backend", "host", "list"],
        ["--backend", "host", "open", "foo.rb"],
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


# --- Error popup wrapper -----------------------------------------------------


class _CapturingPopup:
    def __init__(self, *, interactive: bool) -> None:
        self._interactive = interactive
        self.shown_errors: list[object] = []

    def is_interactive(self) -> bool:
        return self._interactive

    def show_error(self, error: object) -> None:
        self.shown_errors.append(error)


def _install_cli_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    popup: _CapturingPopup,
    raise_exc: BaseException,
) -> None:
    from hop import cli
    from hop.commands import EnterSessionCommand

    class _Services:
        def __init__(self) -> None:
            self.popup = popup

    def fake_parse(_argv: list[str] | None = None) -> EnterSessionCommand:
        return EnterSessionCommand()

    monkeypatch.setattr(cli, "parse_command", fake_parse)
    monkeypatch.setattr(cli, "build_default_services", _Services)
    monkeypatch.setattr(cli, "_warn_if_hopd_version_stale", lambda: None)

    def raise_during_execute(_command: object, *, cwd: object, services: object) -> int:
        del cwd, services
        raise raise_exc

    monkeypatch.setattr(cli, "execute_command", raise_during_execute)


def test_main_invokes_popup_show_error_when_headless(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hop import cli
    from hop.errors import HopError

    error = HopError("no active session named 'nonexistent'")
    popup = _CapturingPopup(interactive=False)
    _install_cli_doubles(monkeypatch, popup=popup, raise_exc=error)

    assert cli.main([]) == 1

    # Popup surfaces the error to the user.
    assert popup.shown_errors == [error]
    # Stderr print is unchanged (additive — captured-stderr callers still see it).
    assert "no active session" in capsys.readouterr().err


def test_main_skips_popup_when_interactive(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from hop import cli
    from hop.errors import HopError

    error = HopError("no active session named 'nonexistent'")
    popup = _CapturingPopup(interactive=True)
    _install_cli_doubles(monkeypatch, popup=popup, raise_exc=error)

    assert cli.main([]) == 1

    # Interactive caller's terminal already shows the stderr print — no popup.
    assert popup.shown_errors == []
    assert "no active session" in capsys.readouterr().err


def test_main_skips_popup_when_error_already_surfaced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hop import cli
    from hop.backends import SessionBackendError

    error = SessionBackendError("prepare failed", surfaced_by_popup=True)
    popup = _CapturingPopup(interactive=False)
    _install_cli_doubles(monkeypatch, popup=popup, raise_exc=error)

    assert cli.main([]) == 1

    # The lifecycle popup already displayed this failure inline; cli.main
    # must not pop a second, redundant error panel.
    assert popup.shown_errors == []
    assert "prepare failed" in capsys.readouterr().err
