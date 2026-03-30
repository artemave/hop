from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from hop.errors import HopError
from hop.session import ProjectSession
from hop.sway import SwayWindow

DEFAULT_BROWSER_DISCOVERY_TIMEOUT_SECONDS = 5.0
DEFAULT_BROWSER_DISCOVERY_POLL_INTERVAL_SECONDS = 0.05
DEFAULT_BROWSER_SETTINGS_COMMAND = ("xdg-settings", "get", "default-web-browser")
DEFAULT_BROWSER_MARK_PREFIX = "hop_browser:"
DEFAULT_BLANK_BROWSER_URL = "about:blank"
DEFAULT_NEW_WINDOW_FLAG = "--new-window"
DEFAULT_XDG_DATA_DIRS = ("/usr/local/share", "/usr/share")


class BrowserError(HopError):
    """Base error for browser lifecycle failures."""


class BrowserCommandError(BrowserError):
    """Raised when hop cannot launch or rediscover the session browser."""


class BrowserSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def focus_window(self, window_id: int) -> None: ...

    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None: ...

    def mark_window(self, window_id: int, mark: str) -> None: ...


class BrowserLauncher(Protocol):
    def launch(self, args: Sequence[str], *, cwd: Path) -> None: ...


class ProcessRunner(Protocol):
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class BrowserLaunchSpec:
    command: tuple[str, ...]
    window_identifiers: frozenset[str]
    new_window_flag: str | None = None


class SessionBrowserAdapter:
    def __init__(
        self,
        *,
        sway: BrowserSwayAdapter,
        launcher: BrowserLauncher | None = None,
        process_runner: ProcessRunner | None = None,
        browser_spec: BrowserLaunchSpec | None = None,
        environ: Mapping[str, str] | None = None,
        discovery_timeout_seconds: float = DEFAULT_BROWSER_DISCOVERY_TIMEOUT_SECONDS,
        discovery_poll_interval_seconds: float = DEFAULT_BROWSER_DISCOVERY_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._sway = sway
        self._launcher = launcher or _SubprocessBrowserLauncher()
        self._process_runner = process_runner or _SubprocessRunner()
        self._browser_spec = browser_spec
        self._environ = dict(environ or os.environ)
        self._discovery_timeout_seconds = discovery_timeout_seconds
        self._discovery_poll_interval_seconds = discovery_poll_interval_seconds

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        window = self._find_session_window(session)
        if window is None:
            window = self._launch_session_browser(session, url=url)
            self._sway.focus_window(window.id)
            return

        if window.workspace_name != session.workspace_name:
            self._sway.move_window_to_workspace(window.id, session.workspace_name)

        self._sway.focus_window(window.id)

        if url is not None:
            self._launcher.launch(
                _build_browser_command(self._browser_spec_for_session(), url=url),
                cwd=session.project_root,
            )

    def _launch_session_browser(
        self,
        session: ProjectSession,
        *,
        url: str | None,
    ) -> SwayWindow:
        browser_spec = self._browser_spec_for_session()
        known_window_ids = {window.id for window in self._sway.list_windows()}
        self._launcher.launch(
            _build_browser_command(browser_spec, url=url, new_window=True),
            cwd=session.project_root,
        )
        window = self._wait_for_new_window(known_window_ids=known_window_ids)
        if window.workspace_name != session.workspace_name:
            self._sway.move_window_to_workspace(window.id, session.workspace_name)
        self._sway.mark_window(window.id, _session_browser_mark(session))
        return window

    def _wait_for_new_window(
        self,
        *,
        known_window_ids: set[int],
    ) -> SwayWindow:
        deadline = time.monotonic() + self._discovery_timeout_seconds
        while time.monotonic() < deadline:
            windows = [window for window in self._sway.list_windows() if window.id not in known_window_ids]
            if windows:
                return max(windows, key=lambda window: window.id)
            time.sleep(self._discovery_poll_interval_seconds)

        msg = "The default browser did not create a new window that hop could attach to."
        raise BrowserCommandError(msg)

    def _find_session_window(self, session: ProjectSession) -> SwayWindow | None:
        session_mark = _session_browser_mark(session)
        windows = [window for window in self._sway.list_windows() if session_mark in window.marks]
        if not windows:
            return None

        workspace_windows = [window for window in windows if window.workspace_name == session.workspace_name]
        if workspace_windows:
            return min(workspace_windows, key=lambda window: window.id)

        return min(windows, key=lambda window: window.id)

    def _browser_spec_for_session(self) -> BrowserLaunchSpec:
        if self._browser_spec is None:
            self._browser_spec = _resolve_default_browser_spec(
                self._process_runner,
                environ=self._environ,
            )
        return self._browser_spec


class _SubprocessBrowserLauncher:
    def launch(self, args: Sequence[str], *, cwd: Path) -> None:
        subprocess.Popen(
            list(args),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


class _SubprocessRunner:
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            check=False,
        )


def _resolve_default_browser_spec(
    process_runner: ProcessRunner,
    *,
    environ: Mapping[str, str],
) -> BrowserLaunchSpec:
    result = process_runner.run(DEFAULT_BROWSER_SETTINGS_COMMAND)
    desktop_entry_name = result.stdout.strip()
    if result.returncode != 0 or not desktop_entry_name:
        msg = "Could not resolve the default browser desktop entry via xdg-settings."
        raise BrowserCommandError(msg)

    desktop_entry_path = _find_desktop_entry(desktop_entry_name, environ=environ)
    if desktop_entry_path is None:
        msg = f"Could not find desktop entry {desktop_entry_name!r} for the default browser."
        raise BrowserCommandError(msg)

    exec_line, startup_wm_class = _read_desktop_entry(desktop_entry_path)
    if exec_line is None:
        msg = f"The desktop entry at {desktop_entry_path!s} does not define an Exec line."
        raise BrowserCommandError(msg)

    command = _parse_desktop_exec(exec_line)
    if not command:
        msg = f"The desktop entry at {desktop_entry_path!s} resolved to an empty command."
        raise BrowserCommandError(msg)

    window_identifiers = _build_window_identifiers(
        desktop_entry_name=desktop_entry_name,
        command=command,
        startup_wm_class=startup_wm_class,
    )
    return BrowserLaunchSpec(
        command=command,
        window_identifiers=window_identifiers,
        new_window_flag=DEFAULT_NEW_WINDOW_FLAG,
    )


def _find_desktop_entry(
    desktop_entry_name: str,
    *,
    environ: Mapping[str, str],
) -> Path | None:
    xdg_data_home = environ.get("XDG_DATA_HOME")
    search_roots = [Path(xdg_data_home)] if xdg_data_home else [Path.home() / ".local" / "share"]
    xdg_data_dirs = environ.get("XDG_DATA_DIRS", ":".join(DEFAULT_XDG_DATA_DIRS)).split(":")
    search_roots.extend(Path(directory) for directory in xdg_data_dirs if directory)

    for root in search_roots:
        candidate = root / "applications" / desktop_entry_name
        if candidate.exists():
            return candidate.resolve()

    return None


def _read_desktop_entry(desktop_entry_path: Path) -> tuple[str | None, str | None]:
    exec_line: str | None = None
    startup_wm_class: str | None = None
    in_desktop_entry_section = False

    for raw_line in desktop_entry_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_desktop_entry_section = line == "[Desktop Entry]"
            continue
        if not in_desktop_entry_section:
            continue
        if exec_line is None and line.startswith("Exec="):
            exec_line = line.removeprefix("Exec=")
        elif startup_wm_class is None and line.startswith("StartupWMClass="):
            startup_wm_class = line.removeprefix("StartupWMClass=")

    return exec_line, startup_wm_class


def _parse_desktop_exec(exec_line: str) -> tuple[str, ...]:
    return tuple(token for token in shlex.split(exec_line) if not token.startswith("%") and "%" not in token)


def _build_window_identifiers(
    *,
    desktop_entry_name: str,
    command: Sequence[str],
    startup_wm_class: str | None,
) -> frozenset[str]:
    identifiers: set[str] = set()

    for value in (desktop_entry_name, startup_wm_class, _actual_browser_executable(command)):
        identifiers.update(_identifier_variants(value))

    return frozenset(identifier for identifier in identifiers if identifier)


def _actual_browser_executable(command: Sequence[str]) -> str | None:
    if not command:
        return None

    if command[0] != "env":
        return command[0]

    for token in command[1:]:
        if "=" in token:
            continue
        return token

    return None


def _identifier_variants(value: str | None) -> set[str]:
    if value is None:
        return set()

    normalized = Path(value).name.strip().lower()
    if not normalized:
        return set()

    variants = {normalized}
    if normalized.endswith(".desktop"):
        normalized = normalized.removesuffix(".desktop")
        variants.add(normalized)
    if normalized.endswith("-stable"):
        variants.add(normalized.removesuffix("-stable"))
    return variants


def _build_browser_command(
    browser_spec: BrowserLaunchSpec,
    *,
    url: str | None,
    new_window: bool = False,
) -> tuple[str, ...]:
    command = list(browser_spec.command)

    if new_window and browser_spec.new_window_flag is not None:
        command.append(browser_spec.new_window_flag)

    if url is not None:
        command.append(url)
    elif new_window:
        command.append(DEFAULT_BLANK_BROWSER_URL)

    return tuple(command)


def _window_matches_browser(window: SwayWindow, window_identifiers: frozenset[str]) -> bool:
    app_id = window.app_id.lower() if window.app_id is not None else None
    window_class = window.window_class.lower() if window.window_class is not None else None
    return app_id in window_identifiers or window_class in window_identifiers


def _session_browser_mark(session: ProjectSession) -> str:
    return f"{DEFAULT_BROWSER_MARK_PREFIX}{session.session_name}"
