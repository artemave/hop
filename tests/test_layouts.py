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
        # `true` / `false` short-circuit so activate probes resolve without
        # wiring a real subprocess; other probes use the configured returncode.
        rc = self.returncode
        if len(args) == 3 and args[0] == "sh" and args[1] == "-c":
            if args[2] == "true":
                rc = 0
            elif args[2] == "false":
                rc = 1
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=rc,
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
        WindowSpec(role="shell", command="", active=True),
        WindowSpec(role="editor", command="nvim", active=True),
        WindowSpec(role="browser", command="", active=False),
    )
    assert runner.calls == []


# --- top-level window overrides -----------------------------------------


def test_top_level_windows_can_opt_out_of_built_in_editor(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="editor", activate="false"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    editor = find_window(windows, "editor")
    assert editor is not None
    assert editor.command == "nvim"  # built-in default still applied
    assert editor.active is False


def test_top_level_windows_can_opt_in_browser(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="browser", activate="true"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    browser = find_window(windows, "browser")
    assert browser is not None
    assert browser.active is True


def test_top_level_windows_can_override_built_in_command(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="shell", command="/usr/bin/zsh"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    shell = find_window(windows, "shell")
    assert shell is not None
    assert shell.command == "/usr/bin/zsh"
    assert shell.active is True


def test_top_level_windows_add_custom_role_with_active(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="worker", command="bin/jobs"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    worker = find_window(windows, "worker")
    assert worker is not None
    assert worker.command == "bin/jobs"
    assert worker.active is True


def test_top_level_window_with_activate_false_is_declared_but_inactive(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="console", command="bin/rails console", activate="false"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    console = find_window(windows, "console")
    assert console is not None
    assert console.command == "bin/rails console"
    assert console.active is False


# --- layouts -------------------------------------------------------------


def test_layout_with_passing_probe_adds_its_windows(tmp_path: Path) -> None:
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="true",
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
    assert server == WindowSpec(role="server", command="bin/dev", active=True)
    assert console == WindowSpec(role="console", command="bin/rails console", active=True)


def test_layout_with_failing_probe_does_not_contribute_windows(tmp_path: Path) -> None:
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="false",
                windows=(WindowConfig(role="server", command="bin/dev"),),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner(returncode=1))

    assert find_window(windows, "server") is None


def test_layout_window_with_activate_false_opts_out_of_matched_layout(tmp_path: Path) -> None:
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="true",
                windows=(
                    WindowConfig(role="server", command="bin/dev"),
                    WindowConfig(role="console", command="bin/rails console", activate="false"),
                ),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    server = find_window(windows, "server")
    console = find_window(windows, "console")
    assert server is not None and server.active is True
    assert console is not None and console.active is False


def test_multiple_matching_layouts_compose(tmp_path: Path) -> None:
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="true",
                windows=(WindowConfig(role="server", command="bin/dev"),),
            ),
            LayoutConfig(
                name="vite",
                activate="true",
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
                activate="test -f {project_root}/bin/rails",
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
                activate=f"test -f {tmp_path}/bin/rails",
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
                activate="true",
                windows=(WindowConfig(role="editor", command="vim"),),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    editor = find_window(windows, "editor")
    assert editor is not None
    assert editor.command == "vim"


# --- ordering ------------------------------------------------------------


def test_resolve_windows_order_pins_shell_editor_then_declared_then_browser(tmp_path: Path) -> None:
    """Shell first, editor second, then user-declared roles in the order
    they appear in the config (layout windows then top-level windows).
    Built-in browser, when not user-declared, is appended last so the
    spec stays addressable without disturbing declaration order."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="true",
                windows=(WindowConfig(role="server", command="bin/dev"),),
            ),
        ),
        windows=(WindowConfig(role="worker", command="bin/jobs"),),
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    assert tuple(window.role for window in windows) == ("shell", "editor", "server", "worker", "browser")


def test_resolve_windows_keeps_shell_and_editor_pinned_when_user_declares_them(tmp_path: Path) -> None:
    """A user-declared shell or editor (e.g. as a layout window) doesn't
    move them out of slots 1 and 2 — the pinning rule wins over the
    declaration position."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="true",
                windows=(
                    WindowConfig(role="server", command="bin/dev"),
                    WindowConfig(role="editor", command="hx"),
                    WindowConfig(role="shell", command="/usr/bin/zsh"),
                ),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    assert tuple(window.role for window in windows) == ("shell", "editor", "server", "browser")


def test_layout_window_activate_runs_as_shell_probe(tmp_path: Path) -> None:
    """Window-level ``activate`` is a shell probe — when it exits 0 the
    window auto-launches; non-zero opts it out (declared but inactive).
    Same shape as the layout-level activate probe."""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    (log_dir / "dev.log").write_text("logged")

    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="true",
                windows=(
                    WindowConfig(
                        role="present_log",
                        command="less log/dev.log",
                        activate="test -s log/dev.log",
                    ),
                    WindowConfig(
                        role="missing_log",
                        command="less log/missing.log",
                        activate="test -s log/missing.log",
                    ),
                ),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=_real_runner)

    present = find_window(windows, "present_log")
    missing = find_window(windows, "missing_log")
    assert present is not None and present.active is True
    assert missing is not None and missing.active is False


def test_top_level_window_activate_runs_as_shell_probe(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("present")

    config = HopConfig(windows=(WindowConfig(role="conditional", command="bin/jobs", activate="test -f sentinel"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=_real_runner)

    conditional = find_window(windows, "conditional")
    assert conditional is not None and conditional.active is True


def test_resolve_windows_role_declared_in_two_matching_layouts_keeps_first_position(tmp_path: Path) -> None:
    """A role contributed by two matching layouts keeps the slot from its
    first appearance (the second layout's entry merges into the existing
    spec instead of bumping the role to a later position)."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="true",
                windows=(
                    WindowConfig(role="server", command="bin/dev"),
                    WindowConfig(role="worker", command="bin/jobs"),
                ),
            ),
            LayoutConfig(
                name="vite",
                activate="true",
                windows=(WindowConfig(role="server", command="bin/vite"),),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    assert tuple(window.role for window in windows) == ("shell", "editor", "server", "worker", "browser")
    server = find_window(windows, "server")
    assert server is not None
    assert server.command == "bin/vite"  # second layout wins per-field


def test_resolve_windows_browser_takes_declared_position_when_user_declares_it(tmp_path: Path) -> None:
    config = HopConfig(
        windows=(
            WindowConfig(role="worker", command="bin/jobs"),
            WindowConfig(role="browser", activate="true"),
            WindowConfig(role="logs", command="tail -f log/dev.log"),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    assert tuple(window.role for window in windows) == ("shell", "editor", "worker", "browser", "logs")


def test_layout_with_no_activate_field_never_matches(tmp_path: Path) -> None:
    """A layout whose `activate` field is None (e.g. a project-only override
    that didn't carry the global's probe) is treated as off — safer than
    always-on."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="orphan",
                activate=None,
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
    """A top-level window declared with only an `activate` opt-out (no
    `command`) has no resolved command. Drop it so `hop term --role X`
    doesn't try to launch an undefined target."""
    config = HopConfig(windows=(WindowConfig(role="ghost", activate="false"),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    assert find_window(windows, "ghost") is None


def test_layout_window_with_explicit_empty_command_resolves_as_shell_like(tmp_path: Path) -> None:
    """`command = ""` is an explicit "just start kitty / empty shell"
    sentinel — distinct from omitting the field, which drops the role."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="true",
                windows=(WindowConfig(role="test", command=""),),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    test = find_window(windows, "test")
    assert test is not None
    assert test.command == ""
    assert test.active is True


def test_top_level_window_with_explicit_empty_command_resolves_as_shell_like(tmp_path: Path) -> None:
    config = HopConfig(windows=(WindowConfig(role="scratch", command=""),))

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    scratch = find_window(windows, "scratch")
    assert scratch is not None
    assert scratch.command == ""
    assert scratch.active is True


def test_layout_window_without_command_only_flips_activate_on_existing_spec(tmp_path: Path) -> None:
    """A layout window may carry just an activate opt-out (no `command`) to
    flip the matched layout's behavior for a role that's already been resolved
    from a prior layer (built-in, earlier layout). The command must survive."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="true",
                windows=(WindowConfig(role="editor", activate="false"),),
            ),
        )
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    editor = find_window(windows, "editor")
    assert editor is not None
    assert editor.command == "nvim"  # built-in command preserved
    assert editor.active is False


def test_top_level_window_overrides_layout_window(tmp_path: Path) -> None:
    """When both an active layout and a top-level entry declare the same
    role, the top-level entry wins per-field (top-level resolves last)."""
    config = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="true",
                windows=(WindowConfig(role="server", command="layout-bin/dev"),),
            ),
        ),
        windows=(WindowConfig(role="server", activate="false"),),
    )

    windows = resolve_windows(config, build_session(tmp_path), runner=RecordingRunner())

    server = find_window(windows, "server")
    assert server is not None
    assert server.command == "layout-bin/dev"  # command from layout, no override
    assert server.active is False  # activate from top-level
