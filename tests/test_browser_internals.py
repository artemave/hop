# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false

import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from typing import Sequence

import pytest

from hop.browser import (
    BrowserCommandError,
    BrowserLaunchSpec,
    SessionBrowserAdapter,
    _actual_browser_executable,
    _build_browser_command,
    _build_window_identifiers,
    _find_desktop_entry,
    _identifier_variants,
    _parse_desktop_exec,
    _read_desktop_entry,
    _resolve_default_browser_spec,
    _SubprocessBrowserLauncher,
    _SubprocessRunner,
)
from hop.session import ProjectSession
from hop.sway import SwayWindow


class StubBrowserLauncher:
    def __init__(self) -> None:
        self.commands: list[tuple[tuple[str, ...], Path]] = []

    def launch(self, args: Sequence[str], *, cwd: Path) -> None:
        self.commands.append((tuple(args), cwd))


class StubProcessRunner:
    def __init__(self, result: CompletedProcess[str]) -> None:
        self.result = result
        self.commands: list[tuple[str, ...]] = []

    def run(self, args: Sequence[str]) -> CompletedProcess[str]:
        command = tuple(args)
        self.commands.append(command)
        return self.result


class StubSwayAdapter:
    def __init__(self, windows: list[SwayWindow]) -> None:
        self.windows = windows
        self.moves: list[tuple[int, str]] = []
        self.focused_window_ids: list[int] = []
        self.marks: list[tuple[int, str]] = []

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return tuple(self.windows)

    def focus_window(self, window_id: int) -> None:
        self.focused_window_ids.append(window_id)

    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None:
        self.moves.append((window_id, workspace_name))

    def mark_window(self, window_id: int, mark: str) -> None:
        self.marks.append((window_id, mark))


def build_session() -> ProjectSession:
    project_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        project_root=project_root,
        session_name="demo",
        workspace_name="p:demo",
    )


def build_browser_spec(
    *, identifiers: frozenset[str] | None = None, new_window_flag: str | None = "--new-window"
) -> BrowserLaunchSpec:
    return BrowserLaunchSpec(
        command=("brave-browser",),
        window_identifiers=identifiers or frozenset({"brave-browser", "brave-browser-stable"}),
        new_window_flag=new_window_flag,
    )


def test_launch_session_browser_keeps_already_attached_workspace_without_move() -> None:
    sway = StubSwayAdapter([])

    def launch(args: Sequence[str], *, cwd: Path) -> None:
        sway.windows.append(
            SwayWindow(
                id=99,
                workspace_name="p:demo",
                app_id="brave-browser",
                window_class=None,
                marks=(),
            )
        )

    launcher = StubBrowserLauncher()
    launcher.launch = launch  # type: ignore[method-assign]
    adapter = SessionBrowserAdapter(sway=sway, launcher=launcher, browser_spec=build_browser_spec())

    adapter.ensure_browser(build_session(), url=None)

    assert sway.moves == []
    assert sway.marks == [(99, "_hop_browser:demo")]
    assert sway.focused_window_ids == [99]


@pytest.mark.parametrize(
    ("returncode", "stdout", "message"),
    [
        (1, "brave-browser.desktop\n", "Could not resolve the default browser"),
        (0, "", "Could not resolve the default browser"),
    ],
)
def test_resolve_default_browser_spec_rejects_missing_default_browser(
    returncode: int,
    stdout: str,
    message: str,
) -> None:
    process_runner = StubProcessRunner(CompletedProcess(("xdg-settings",), returncode, stdout, ""))

    with pytest.raises(BrowserCommandError, match=message):
        _resolve_default_browser_spec(process_runner, environ={"XDG_DATA_DIRS": ""})


def test_resolve_default_browser_spec_rejects_missing_desktop_entry_file(tmp_path: Path) -> None:
    process_runner = StubProcessRunner(CompletedProcess(("xdg-settings",), 0, "missing.desktop\n", ""))

    with pytest.raises(BrowserCommandError, match="missing.desktop"):
        _resolve_default_browser_spec(
            process_runner,
            environ={"XDG_DATA_HOME": str(tmp_path), "XDG_DATA_DIRS": ""},
        )


def test_resolve_default_browser_spec_rejects_missing_exec_line(tmp_path: Path) -> None:
    applications = tmp_path / "applications"
    applications.mkdir()
    desktop_entry = applications / "browser.desktop"
    desktop_entry.write_text("[Desktop Entry]\nStartupWMClass=Browser\n")
    process_runner = StubProcessRunner(CompletedProcess(("xdg-settings",), 0, "browser.desktop\n", ""))

    with pytest.raises(BrowserCommandError, match="does not define an Exec line"):
        _resolve_default_browser_spec(
            process_runner,
            environ={"XDG_DATA_HOME": str(tmp_path), "XDG_DATA_DIRS": ""},
        )


def test_resolve_default_browser_spec_rejects_empty_exec_command(tmp_path: Path) -> None:
    applications = tmp_path / "applications"
    applications.mkdir()
    desktop_entry = applications / "browser.desktop"
    desktop_entry.write_text("[Desktop Entry]\nExec=%U\n")
    process_runner = StubProcessRunner(CompletedProcess(("xdg-settings",), 0, "browser.desktop\n", ""))

    with pytest.raises(BrowserCommandError, match="resolved to an empty command"):
        _resolve_default_browser_spec(
            process_runner,
            environ={"XDG_DATA_HOME": str(tmp_path), "XDG_DATA_DIRS": ""},
        )


def test_find_desktop_entry_checks_multiple_search_roots(tmp_path: Path) -> None:
    home_root = tmp_path / "home"
    shared_root = tmp_path / "shared"
    candidate = shared_root / "applications" / "browser.desktop"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("[Desktop Entry]\nExec=browser\n")

    resolved = _find_desktop_entry(
        "browser.desktop",
        environ={
            "XDG_DATA_HOME": str(home_root),
            "XDG_DATA_DIRS": f"{shared_root}:{tmp_path / 'unused'}",
        },
    )

    assert resolved == candidate.resolve()


def test_read_desktop_entry_ignores_comments_and_other_sections(tmp_path: Path) -> None:
    desktop_entry = tmp_path / "browser.desktop"
    desktop_entry.write_text(
        "\n".join(
            [
                "[Other]",
                "Exec=ignored",
                "",
                "# comment",
                "[Desktop Entry]",
                "Exec=/usr/bin/firefox %U",
                "StartupWMClass=Firefox",
                "Name=Firefox",
            ]
        )
    )

    assert _read_desktop_entry(desktop_entry) == ("/usr/bin/firefox %U", "Firefox")


def test_parse_desktop_exec_strips_percent_placeholders() -> None:
    assert _parse_desktop_exec("/usr/bin/firefox --name demo %U %f") == ("/usr/bin/firefox", "--name", "demo")


def test_build_window_identifiers_includes_desktop_entry_command_and_class_variants() -> None:
    identifiers = _build_window_identifiers(
        desktop_entry_name="Brave-Browser.desktop",
        command=("env", "MOZ_ENABLE_WAYLAND=1", "/usr/bin/brave-browser-stable"),
        startup_wm_class="Brave-Browser",
    )

    assert identifiers == frozenset({"brave-browser.desktop", "brave-browser", "brave-browser-stable"})


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ((), None),
        (("firefox",), "firefox"),
        (("env", "A=1", "B=2"), None),
        (("env", "A=1", "/usr/bin/firefox"), "/usr/bin/firefox"),
    ],
)
def test_actual_browser_executable_handles_env_wrappers(
    command: tuple[str, ...],
    expected: str | None,
) -> None:
    assert _actual_browser_executable(command) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, set()),
        ("   ", set()),
        ("Brave-Browser.desktop", {"brave-browser.desktop", "brave-browser"}),
        ("brave-browser-stable", {"brave-browser-stable", "brave-browser"}),
    ],
)
def test_identifier_variants_normalize_common_browser_names(value: str | None, expected: set[str]) -> None:
    assert _identifier_variants(value) == expected


def test_build_browser_command_handles_new_window_without_url() -> None:
    browser_spec = build_browser_spec(new_window_flag="--new-window")

    assert _build_browser_command(browser_spec, url=None, new_window=True) == (
        "brave-browser",
        "--new-window",
        "about:blank",
    )
    assert _build_browser_command(browser_spec, url=None, new_window=False) == ("brave-browser",)


def test_subprocess_browser_launcher_invokes_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_popen(args: list[str], **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    _SubprocessBrowserLauncher().launch(("firefox", "https://example.com"), cwd=Path("/tmp/demo"))

    assert captured == {
        "args": ["firefox", "https://example.com"],
        "kwargs": {
            "cwd": "/tmp/demo",
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "start_new_session": True,
        },
    }


def test_subprocess_runner_invokes_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = CompletedProcess(("firefox",), 0, "ok", "")

    def fake_run(args: list[str], **kwargs: object) -> CompletedProcess[str]:
        assert args == ["firefox"]
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        return expected

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _SubprocessRunner().run(("firefox",)) == expected
