import os
import sys
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable, Sequence

from hop.browser import BrowserCommandError, BrowserLaunchSpec, SessionBrowserAdapter
from hop.session import ProjectSession
from hop.sway import SwayWindow


class StubSwayAdapter:
    def __init__(self, windows: list[SwayWindow]) -> None:
        self.windows = windows
        self.focused_window_ids: list[int] = []
        self.moves: list[tuple[int, str]] = []
        self.marks: list[tuple[int, str]] = []

    def list_windows(self) -> tuple[SwayWindow, ...]:
        return tuple(self.windows)

    def focus_window(self, window_id: int) -> None:
        self.focused_window_ids.append(window_id)

    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None:
        self.moves.append((window_id, workspace_name))
        self.windows = [
            window
            if window.id != window_id
            else SwayWindow(
                id=window.id,
                workspace_name=workspace_name,
                app_id=window.app_id,
                window_class=window.window_class,
                marks=window.marks,
                focused=window.focused,
            )
            for window in self.windows
        ]

    def mark_window(self, window_id: int, mark: str) -> None:
        self.marks.append((window_id, mark))
        self.windows = [
            window
            if window.id != window_id
            else SwayWindow(
                id=window.id,
                workspace_name=window.workspace_name,
                app_id=window.app_id,
                window_class=window.window_class,
                marks=tuple({*window.marks, mark}),
                focused=window.focused,
            )
            for window in self.windows
        ]


class StubBrowserLauncher:
    def __init__(self, *, on_launch: Callable[[tuple[str, ...]], None] | None = None) -> None:
        self.on_launch = on_launch
        self.commands: list[tuple[tuple[str, ...], Path]] = []

    def launch(self, args: Sequence[str], *, cwd: Path) -> None:
        command = tuple(args)
        self.commands.append((command, cwd))
        if self.on_launch is not None:
            self.on_launch(command)


class StubProcessRunner:
    def __init__(self, *, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.commands: list[tuple[str, ...]] = []

    def run(self, args: Sequence[str]) -> CompletedProcess[str]:
        command = tuple(args)
        self.commands.append(command)
        return CompletedProcess(command, self.returncode, self.stdout, "")


def build_session() -> ProjectSession:
    session_root = Path("/tmp/demo").resolve()
    return ProjectSession(
        session_root=session_root,
        session_name="demo",
        workspace_name="p:demo",
    )


def build_browser_spec() -> BrowserLaunchSpec:
    return BrowserLaunchSpec(
        command=("brave-browser",),
        window_identifiers=frozenset({"brave-browser", "brave-browser-stable"}),
        new_window_flag="--new-window",
    )


def test_ensure_browser_focuses_existing_session_window() -> None:
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=23,
                workspace_name="p:demo",
                app_id="brave-browser",
                window_class=None,
                marks=("_hop_browser:demo",),
            )
        ]
    )
    launcher = StubBrowserLauncher()
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        browser_spec=build_browser_spec(),
    )

    adapter.ensure_browser(build_session(), url=None)

    assert launcher.commands == []
    assert sway.moves == []
    assert sway.focused_window_ids == [23]


def test_ensure_browser_reattaches_drifted_session_window_before_focusing() -> None:
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=23,
                workspace_name="scratch",
                app_id="brave-browser",
                window_class=None,
                marks=("_hop_browser:demo",),
            )
        ]
    )
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=StubBrowserLauncher(),
        browser_spec=build_browser_spec(),
    )

    adapter.ensure_browser(build_session(), url=None)

    assert sway.moves == [(23, "p:demo")]
    assert sway.focused_window_ids == [23]


def test_ensure_browser_opens_url_in_existing_session_window() -> None:
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=23,
                workspace_name="p:demo",
                app_id="brave-browser",
                window_class=None,
                marks=("_hop_browser:demo",),
            )
        ]
    )
    launcher = StubBrowserLauncher()
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        browser_spec=build_browser_spec(),
    )

    adapter.ensure_browser(build_session(), url="https://example.com")

    assert sway.focused_window_ids == [23]
    assert launcher.commands == [(("brave-browser", "https://example.com"), build_session().session_root)]


def test_ensure_browser_promotes_an_unclaimed_browser_window_on_the_session_workspace() -> None:
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=17,
                workspace_name="p:demo",
                app_id="brave-browser",
                window_class=None,
                marks=(),
            )
        ]
    )
    launcher = StubBrowserLauncher()
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        browser_spec=build_browser_spec(),
    )

    adapter.ensure_browser(build_session(), url=None)

    assert launcher.commands == []
    assert sway.marks == [(17, "_hop_browser:demo")]
    assert sway.moves == []
    assert sway.focused_window_ids == [17]


def test_ensure_browser_ignores_browser_windows_on_other_workspaces() -> None:
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=17,
                workspace_name="scratch",
                app_id="brave-browser",
                window_class=None,
                marks=(),
            )
        ]
    )

    def add_new_window(_command: tuple[str, ...]) -> None:
        sway.windows.append(
            SwayWindow(
                id=41,
                workspace_name="p:demo",
                app_id="brave-browser",
                window_class=None,
                marks=(),
            )
        )

    launcher = StubBrowserLauncher(on_launch=add_new_window)
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        browser_spec=build_browser_spec(),
    )

    adapter.ensure_browser(build_session(), url=None)

    assert sway.marks == [(41, "_hop_browser:demo")]
    assert sway.focused_window_ids == [41]


def test_ensure_browser_does_not_promote_another_sessions_browser_window() -> None:
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=17,
                workspace_name="p:demo",
                app_id="brave-browser",
                window_class=None,
                marks=("_hop_browser:other",),
            )
        ]
    )

    def add_new_window(_command: tuple[str, ...]) -> None:
        sway.windows.append(
            SwayWindow(
                id=41,
                workspace_name="p:demo",
                app_id="brave-browser",
                window_class=None,
                marks=(),
            )
        )

    launcher = StubBrowserLauncher(on_launch=add_new_window)
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        browser_spec=build_browser_spec(),
    )

    adapter.ensure_browser(build_session(), url=None)

    assert sway.marks == [(41, "_hop_browser:demo")]
    assert sway.focused_window_ids == [41]


def test_ensure_browser_promotes_a_window_whose_process_matches_but_name_does_not() -> None:
    # Firefox Developer Edition's generated `userapp-*.desktop` entry carries no
    # StartupWMClass, so the derived identifiers say `firefox-bin` while the
    # window says `app_id=firefox-dev`. Nothing matches by name - the owning
    # process is the only signal that the window is the browser hop launches.
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=17,
                workspace_name="p:demo",
                app_id="firefox-dev",
                window_class=None,
                marks=(),
                pid=os.getpid(),
            )
        ]
    )
    launcher = StubBrowserLauncher()
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        browser_spec=BrowserLaunchSpec(
            command=(sys.executable,),
            window_identifiers=frozenset({"firefox-bin"}),
            new_window_flag="--new-window",
        ),
    )

    adapter.ensure_browser(build_session(), url=None)

    assert launcher.commands == []
    assert sway.marks == [(17, "_hop_browser:demo")]
    assert sway.focused_window_ids == [17]


def test_ensure_browser_does_not_promote_a_window_whose_process_is_unreadable() -> None:
    # pid 0 has no /proc entry - the same shape as a window whose process exited
    # between the Sway query and the lookup, or a sandboxed browser that hides
    # its exe. With no name match either, hop launches rather than guesses.
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=17,
                workspace_name="p:demo",
                app_id="firefox-dev",
                window_class=None,
                marks=(),
                pid=0,
            )
        ]
    )

    def add_new_window(_command: tuple[str, ...]) -> None:
        sway.windows.append(
            SwayWindow(
                id=41,
                workspace_name="p:demo",
                app_id="brave-browser",
                window_class=None,
                marks=(),
            )
        )

    launcher = StubBrowserLauncher(on_launch=add_new_window)
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        browser_spec=build_browser_spec(),
    )

    adapter.ensure_browser(build_session(), url=None)

    assert sway.marks == [(41, "_hop_browser:demo")]


def test_ensure_browser_does_not_promote_a_non_browser_window() -> None:
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=17,
                workspace_name="p:demo",
                app_id="hop:shell",
                window_class=None,
                marks=(),
                pid=os.getpid(),
            )
        ]
    )

    def add_new_window(_command: tuple[str, ...]) -> None:
        sway.windows.append(
            SwayWindow(
                id=41,
                workspace_name="p:demo",
                app_id="brave-browser",
                window_class=None,
                marks=(),
            )
        )

    launcher = StubBrowserLauncher(on_launch=add_new_window)
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        browser_spec=build_browser_spec(),
    )

    adapter.ensure_browser(build_session(), url=None)

    assert sway.marks == [(41, "_hop_browser:demo")]


def test_ensure_browser_promotes_by_window_class_and_opens_the_url() -> None:
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=17,
                workspace_name="p:demo",
                app_id=None,
                window_class="Brave-browser",
                marks=(),
            )
        ]
    )
    launcher = StubBrowserLauncher()
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        browser_spec=build_browser_spec(),
    )

    adapter.ensure_browser(build_session(), url="https://example.com")

    assert sway.marks == [(17, "_hop_browser:demo")]
    assert launcher.commands == [
        (("brave-browser", "https://example.com"), build_session().session_root),
    ]


def test_ensure_browser_launches_new_window_marks_it_and_focuses_it() -> None:
    sway = StubSwayAdapter([])

    def add_new_window(_command: tuple[str, ...]) -> None:
        sway.windows.append(
            SwayWindow(
                id=41,
                workspace_name="scratch",
                app_id="brave-browser",
                window_class=None,
                marks=(),
            )
        )

    launcher = StubBrowserLauncher(on_launch=add_new_window)
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        browser_spec=build_browser_spec(),
    )

    adapter.ensure_browser(build_session(), url="https://example.com")

    assert launcher.commands == [
        (
            ("brave-browser", "--new-window", "https://example.com"),
            build_session().session_root,
        )
    ]
    assert sway.moves == [(41, "p:demo")]
    assert sway.marks == [(41, "_hop_browser:demo")]
    assert sway.focused_window_ids == [41]


def test_ensure_browser_launches_from_home_for_a_remote_session() -> None:
    # The browser is a host GUI, but its launch cwd must be a real *local* dir.
    # A remote session's session_root only exists on the remote, so launching
    # there raises FileNotFoundError (the translated-URL bug); use the host home.
    sway = StubSwayAdapter([])

    def add_new_window(_command: tuple[str, ...]) -> None:
        sway.windows.append(
            SwayWindow(id=41, workspace_name="scratch", app_id="brave-browser", window_class=None, marks=())
        )

    launcher = StubBrowserLauncher(on_launch=add_new_window)
    adapter = SessionBrowserAdapter(sway=sway, launcher=launcher, browser_spec=build_browser_spec())
    remote = ProjectSession(
        session_root=Path("/home/admin/projects/thonon-les-pains"),
        session_name="thonon-les-pains",
        workspace_name="p:thonon-les-pains",
        host="devbox",
    )

    adapter.ensure_browser(remote, url="http://devbox.local:54321")

    (_command, cwd) = launcher.commands[0]
    assert cwd == Path.home()


def test_ensure_browser_raises_when_launch_does_not_create_a_new_window() -> None:
    adapter = SessionBrowserAdapter(
        sway=StubSwayAdapter([]),
        launcher=StubBrowserLauncher(),
        browser_spec=build_browser_spec(),
        discovery_timeout_seconds=0.01,
        discovery_poll_interval_seconds=0.001,
    )

    try:
        adapter.ensure_browser(build_session(), url=None)
    except BrowserCommandError:
        pass
    else:
        raise AssertionError("Expected BrowserCommandError when no new browser window appears")


def test_adapter_resolves_default_browser_desktop_entry(tmp_path: Path) -> None:
    applications_directory = tmp_path / "applications"
    applications_directory.mkdir()
    desktop_entry = applications_directory / "brave-browser.desktop"
    desktop_entry.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                "Exec=/usr/bin/brave-browser-stable %U",
                "StartupWMClass=Brave-browser",
            ]
        )
    )

    process_runner = StubProcessRunner(stdout="brave-browser.desktop\n")
    sway = StubSwayAdapter(
        [
            SwayWindow(
                id=23,
                workspace_name="p:demo",
                app_id="brave-browser",
                window_class=None,
                marks=("_hop_browser:demo",),
            )
        ]
    )
    launcher = StubBrowserLauncher()
    adapter = SessionBrowserAdapter(
        sway=sway,
        launcher=launcher,
        process_runner=process_runner,
        environ={
            "XDG_DATA_HOME": str(tmp_path),
            "XDG_DATA_DIRS": "",
        },
    )

    adapter.ensure_browser(build_session(), url="https://example.com")

    assert process_runner.commands == [("xdg-settings", "get", "default-web-browser")]
    assert launcher.commands == [
        (("/usr/bin/brave-browser-stable", "https://example.com"), build_session().session_root)
    ]
