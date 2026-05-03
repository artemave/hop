from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from hop.config import HopConfig, LayoutConfig, WindowConfig
from hop.layouts import WindowSpec, find_window, resolve_windows
from hop.session import ProjectSession


def build_session(project_root: Path) -> ProjectSession:
    return ProjectSession(
        project_root=project_root,
        session_name=project_root.name,
        workspace_name=f"p:{project_root.name}",
    )


@dataclass
class RecordingRunner:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    calls: list[tuple[tuple[str, ...], Path]] = field(default_factory=lambda: [])

    def __call__(self, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(args), cwd))
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def _real_runner(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, check=False)


# --- built-in defaults ---------------------------------------------------


def test_resolve_windows_defaults_when_config_is_empty(tmp_path: Path) -> None:
    runner = RecordingRunner()
    windows = resolve_windows(HopConfig(), build_session(tmp_path), runner=runner)

    assert windows == (
        WindowSpec(role="shell", command="", autostart_active=True),
        WindowSpec(role="editor", command="nvim", autostart_active=True),
        WindowSpec(role="browser", command="", autostart_active=False),
    )
    assert runner.calls == []


# --- top-level window overrides -----------------------------------------


def test_top_level_windows_can_opt_out_of_built_in_editor(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="editor", autostart="false"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    editor = find_window(windows, "editor")
    assert editor is not None
    assert editor.command == "nvim"  # built-in default still applied
    assert editor.autostart_active is False


def test_top_level_windows_can_opt_in_browser(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="browser", autostart="true"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    browser = find_window(windows, "browser")
    assert browser is not None
    assert browser.autostart_active is True


def test_top_level_windows_can_override_built_in_command(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="shell", command="/usr/bin/zsh"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    shell = find_window(windows, "shell")
    assert shell is not None
    assert shell.command == "/usr/bin/zsh"
    assert shell.autostart_active is True


def test_top_level_windows_add_custom_role_with_autostart_active(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="worker", command="bin/jobs"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    worker = find_window(windows, "worker")
    assert worker is not None
    assert worker.command == "bin/jobs"
    assert worker.autostart_active is True


def test_top_level_window_with_autostart_false_is_declared_but_inactive(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="console", command="bin/rails console", autostart="false"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    console = find_window(windows, "console")
    assert console is not None
    assert console.command == "bin/rails console"
    assert console.autostart_active is False


# --- layouts -------------------------------------------------------------


def test_layout_with_passing_probe_adds_its_windows(tmp_path: Path) -> None:
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                autostart="true",
                windows=(
                    WindowConfig(role="server", command="bin/dev"),
                    WindowConfig(role="console", command="bin/rails console"),
                ),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    server = find_window(windows, "server")
    console = find_window(windows, "console")
    assert server == WindowSpec(role="server", command="bin/dev", autostart_active=True)
    assert console == WindowSpec(role="console", command="bin/rails console", autostart_active=True)


def test_layout_with_failing_probe_does_not_contribute_windows(tmp_path: Path) -> None:
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                autostart="false",
                windows=(WindowConfig(role="server", command="bin/dev"),),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner(returncode=1))

    assert find_window(windows, "server") is None


def test_layout_window_with_autostart_false_opts_out_of_matched_layout(tmp_path: Path) -> None:
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                autostart="true",
                windows=(
                    WindowConfig(role="server", command="bin/dev"),
                    WindowConfig(role="console", command="bin/rails console", autostart="false"),
                ),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    server = find_window(windows, "server")
    console = find_window(windows, "console")
    assert server is not None and server.autostart_active is True
    assert console is not None and console.autostart_active is False


def test_multiple_matching_layouts_compose(tmp_path: Path) -> None:
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                autostart="true",
                windows=(WindowConfig(role="server", command="bin/dev"),),
            ),
            LayoutConfig(
                name="vite",
                autostart="true",
                windows=(WindowConfig(role="vite", command="bun run dev"),),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    assert find_window(windows, "server") is not None
    assert find_window(windows, "vite") is not None


def test_layout_probe_substitutes_project_root(tmp_path: Path) -> None:
    runner = RecordingRunner()
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                autostart="test -f {project_root}/bin/rails",
                windows=(WindowConfig(role="server", command="bin/dev"),),
            ),
        )
    )

    resolve_windows(config, build_session(tmp_path), runner=runner)

    assert runner.calls == [
        (("sh", "-c", f"test -f {shlex.quote(str(tmp_path))}/bin/rails"), tmp_path),
    ]


def test_layout_real_filesystem_probe(tmp_path: Path) -> None:
    """Real `test -f bin/rails` probe behaves correctly against the filesystem."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                autostart=f"test -f {tmp_path}/bin/rails",
                windows=(WindowConfig(role="server", command="bin/dev"),),
            ),
        )
    )

    # File missing → layout doesn't match.
    windows = resolve_windows(config, build_session(tmp_path), runner=_real_runner)
    assert find_window(windows, "server") is None

    # File present → layout matches.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rails").write_text("#!/bin/sh\n")

    windows = resolve_windows(config, build_session(tmp_path), runner=_real_runner)
    assert find_window(windows, "server") is not None


def test_layout_overrides_built_in_command(tmp_path: Path) -> None:
    """A layout that defines an editor window with a different command
    overrides the built-in `nvim`."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="vim-only",
                autostart="true",
                windows=(WindowConfig(role="editor", command="vim"),),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    editor = find_window(windows, "editor")
    assert editor is not None
    assert editor.command == "vim"


# --- ordering ------------------------------------------------------------


def test_resolve_windows_order_is_builtins_then_layouts_then_top_level(tmp_path: Path) -> None:
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                autostart="true",
                windows=(WindowConfig(role="server", command="bin/dev"),),
            ),
        ),
        windows=(WindowConfig(role="worker", command="bin/jobs"),),
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    assert tuple(window.role for window in windows) == ("shell", "editor", "browser", "server", "worker")


def test_layout_with_no_autostart_field_never_matches(tmp_path: Path) -> None:
    """A layout whose `autostart` field is None (e.g. a project-only override
    that didn't carry the global's probe) is treated as off — safer than
    always-on."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="orphan",
                autostart=None,
                windows=(WindowConfig(role="server", command="bin/dev"),),
            ),
        )
    )

    runner = RecordingRunner()
    windows = resolve_windows(config, build_session(tmp_path), runner=runner)

    assert find_window(windows, "server") is None
    # Probe never ran since the layout was skipped outright.
    assert runner.calls == []


def test_resolver_drops_user_window_with_no_command(tmp_path: Path) -> None:
    """A top-level window declared with only an `autostart` opt-out (no
    `command`) has no resolved command. Drop it so `hop term --role X`
    doesn't try to launch an undefined target."""
    config = HopConfig(windows=(WindowConfig(role="ghost", autostart="false"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    assert find_window(windows, "ghost") is None


def test_top_level_window_overrides_layout_window(tmp_path: Path) -> None:
    """When both an active layout and a top-level entry declare the same
    role, the top-level entry wins per-field (top-level resolves last)."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                autostart="true",
                windows=(WindowConfig(role="server", command="layout-bin/dev"),),
            ),
        ),
        windows=(WindowConfig(role="server", autostart="false"),),
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    server = find_window(windows, "server")
    assert server is not None
    assert server.command == "layout-bin/dev"  # command from layout, no override
    assert server.autostart_active is False  # autostart from top-level
