# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false

import runpy
from pathlib import Path
from typing import cast

import pytest
from hop.app import HopServices, build_default_services, execute_command
from hop.browser import SessionBrowserAdapter
from hop.cli import main, parse_command
from hop.commands import Command, EnterSessionCommand
from hop.editor import SharedNeovimEditorAdapter
from hop.errors import HopError
from hop.kitty import KittyRemoteControlAdapter
from hop.sway import SwayIpcAdapter


class ExplodingHopError(HopError):
    pass


def test_build_default_services_constructs_runtime_adapters() -> None:
    services = build_default_services()

    assert isinstance(services, HopServices)
    assert isinstance(services.sway, SwayIpcAdapter)
    assert isinstance(services.kitty, KittyRemoteControlAdapter)
    assert isinstance(services.neovim, SharedNeovimEditorAdapter)
    assert isinstance(services.browser, SessionBrowserAdapter)


def test_parse_command_raises_for_unsupported_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubParser:
        def parse_args(self, _argv: list[str] | None) -> object:
            return type("Namespace", (), {"command": "explode"})()

    monkeypatch.setattr("hop.cli.build_parser", lambda: StubParser())

    with pytest.raises(ValueError, match="Unsupported command 'explode'"):
        parse_command(["explode"])


def test_main_returns_execute_command_result(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel_services = object()
    execution: dict[str, object] = {}

    def fake_parse_command(argv: object | None = None) -> EnterSessionCommand:
        return EnterSessionCommand()

    def fake_build_default_services() -> object:
        return sentinel_services

    monkeypatch.setattr("hop.cli.parse_command", fake_parse_command)
    monkeypatch.setattr("hop.cli.build_default_services", fake_build_default_services)

    def fake_execute_command(command: object, *, cwd: Path, services: object) -> int:
        execution["command"] = command
        execution["cwd"] = cwd
        execution["services"] = services
        return 7

    monkeypatch.setattr("hop.cli.execute_command", fake_execute_command)

    assert main([]) == 7
    assert execution == {
        "command": EnterSessionCommand(),
        "cwd": Path.cwd(),
        "services": sentinel_services,
    }


def test_main_prints_hop_errors_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_parse_command(argv: object | None = None) -> EnterSessionCommand:
        return EnterSessionCommand()

    def fake_build_default_services() -> object:
        return object()

    monkeypatch.setattr("hop.cli.parse_command", fake_parse_command)
    monkeypatch.setattr("hop.cli.build_default_services", fake_build_default_services)

    def raise_hop_error(command: object, *, cwd: Path, services: object) -> int:
        raise ExplodingHopError("boom")

    monkeypatch.setattr("hop.cli.execute_command", raise_hop_error)

    assert main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "boom\n"


def test_execute_command_rejects_unsupported_commands() -> None:
    services = build_default_services()
    unsupported_command = cast(Command, object())

    with pytest.raises(ValueError, match="Unsupported command"):
        execute_command(unsupported_command, cwd=Path("/tmp"), services=services)


def test_module_main_raises_system_exit_with_cli_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hop.cli.main", lambda: 19)

    with pytest.raises(SystemExit, match="19"):
        runpy.run_module("hop.__main__", run_name="__main__")
