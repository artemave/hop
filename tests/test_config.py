from __future__ import annotations

from pathlib import Path

import pytest

from hop.config import (
    BackendConfig,
    HopConfig,
    HopConfigError,
    LayoutConfig,
    WindowConfig,
    default_global_config_path,
    load_global_config,
    load_project_config,
    merge_backends,
    merge_configs,
    merge_layouts,
    merge_windows,
)


def write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


# --- backend parsing -----------------------------------------------------


def test_load_global_config_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_global_config(tmp_path / "missing.toml") == HopConfig()


def test_load_global_config_returns_empty_when_no_sections(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "# nothing here\n")

    assert load_global_config(config_file) == HopConfig()


def test_load_global_config_parses_full_backend(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
default        = "test -f docker-compose.dev.yml"
prepare        = "podman-compose -f docker-compose.dev.yml up -d devcontainer"
teardown       = "podman-compose -f docker-compose.dev.yml down"
workspace      = "podman-compose -f docker-compose.dev.yml exec devcontainer pwd"
command_prefix = "podman-compose -f docker-compose.dev.yml exec devcontainer"
""",
    )

    assert load_global_config(config_file).backends == (
        BackendConfig(
            name="devcontainer",
            default="test -f docker-compose.dev.yml",
            prepare="podman-compose -f docker-compose.dev.yml up -d devcontainer",
            teardown="podman-compose -f docker-compose.dev.yml down",
            workspace="podman-compose -f docker-compose.dev.yml exec devcontainer pwd",
            command_prefix="podman-compose -f docker-compose.dev.yml exec devcontainer",
        ),
    )


def test_load_global_config_rejects_legacy_flat_shell_field(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        '[backends.devcontainer]\nshell = "zsh"\n',
    )

    with pytest.raises(HopConfigError) as exc:
        load_global_config(config_file)

    message = str(exc.value)
    assert "removed" in message
    assert "command_prefix" in message
    assert "[windows.shell]" in message


def test_load_global_config_rejects_legacy_flat_editor_field(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        '[backends.devcontainer]\neditor = "nvim"\n',
    )

    with pytest.raises(HopConfigError) as exc:
        load_global_config(config_file)

    assert "[windows.editor]" in str(exc.value)


def test_load_global_config_rejects_per_backend_windows_subtable(tmp_path: Path) -> None:
    """The just-shipped per-backend windows shape was replaced by top-level
    [layouts.<name>] and [windows.<role>]. Surface that pivot to the user."""
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer.windows.shell]
command = "zsh"
""",
    )

    with pytest.raises(HopConfigError) as exc:
        load_global_config(config_file)

    message = str(exc.value)
    assert "[layouts.<name>]" in message
    assert "[windows.<role>]" in message


def test_load_global_config_rejects_explicit_host_backend(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        '[backends.host]\ncommand_prefix = "nope"\n',
    )

    with pytest.raises(HopConfigError, match="reserved"):
        load_global_config(config_file)


def test_load_global_config_rejects_unknown_backend_field(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
prepar = "typo"
""",
    )

    with pytest.raises(HopConfigError, match="backend 'devcontainer' has unknown field 'prepar'"):
        load_global_config(config_file)


def test_load_global_config_rejects_empty_command_prefix(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        '[backends.devcontainer]\ncommand_prefix = "   "\n',
    )

    with pytest.raises(HopConfigError, match="field 'command_prefix' must not be empty"):
        load_global_config(config_file)


# --- layout parsing ------------------------------------------------------


def test_load_global_config_parses_layout_with_windows(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
autostart = "test -f bin/rails"

[layouts.rails.windows.server]
command = "bin/dev"

[layouts.rails.windows.console]
command   = "bin/rails console"
autostart = "false"
""",
    )

    assert load_global_config(config_file).layouts == (
        LayoutConfig(
            name="rails",
            autostart="test -f bin/rails",
            windows=(
                WindowConfig(role="server", command="bin/dev"),
                WindowConfig(role="console", command="bin/rails console", autostart="false"),
            ),
        ),
    )


def test_load_global_config_rejects_unknown_layout_field(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
autostart = "true"
oops = "no"
""",
    )

    with pytest.raises(HopConfigError, match="layout 'rails' has unknown field 'oops'"):
        load_global_config(config_file)


def test_load_global_config_rejects_window_with_invalid_autostart(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
autostart = "true"

[layouts.rails.windows.server]
command   = "bin/dev"
autostart = "test -f bin/dev"
""",
    )

    with pytest.raises(HopConfigError, match="must be 'true' or 'false'"):
        load_global_config(config_file)


def test_load_global_config_rejects_unknown_window_field_in_layout(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
autostart = "true"

[layouts.rails.windows.server]
command = "bin/dev"
extra   = "nope"
""",
    )

    with pytest.raises(HopConfigError, match="window 'server' has unknown field 'extra'"):
        load_global_config(config_file)


# --- top-level window parsing -------------------------------------------


def test_load_global_config_parses_top_level_windows(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[windows.editor]
autostart = "false"

[windows.worker]
command = "bin/jobs"
""",
    )

    assert load_global_config(config_file).windows == (
        WindowConfig(role="editor", autostart="false"),
        WindowConfig(role="worker", command="bin/jobs"),
    )


def test_load_global_config_rejects_top_level_windows_invalid_autostart(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[windows.worker]
command   = "bin/jobs"
autostart = "test -f bin/jobs"
""",
    )

    with pytest.raises(HopConfigError, match="must be 'true' or 'false'"):
        load_global_config(config_file)


def test_load_global_config_parses_workspace_layout(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", 'workspace_layout = "tabbed"\n')

    assert load_global_config(config_file).workspace_layout == "tabbed"


def test_load_global_config_rejects_invalid_workspace_layout(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", 'workspace_layout = "tiled"\n')

    with pytest.raises(HopConfigError, match="must be one of"):
        load_global_config(config_file)


def test_load_global_config_rejects_non_string_workspace_layout(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "workspace_layout = 42\n")

    with pytest.raises(HopConfigError, match="must be a string"):
        load_global_config(config_file)


def test_load_global_config_workspace_layout_omitted_is_none(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "# nothing\n")

    assert load_global_config(config_file).workspace_layout is None


def test_merge_configs_workspace_layout_project_wins() -> None:
    project = HopConfig(workspace_layout="tabbed")
    global_ = HopConfig(workspace_layout="splith")

    assert merge_configs(project, global_).workspace_layout == "tabbed"


def test_merge_configs_workspace_layout_inherits_from_global() -> None:
    project = HopConfig()
    global_ = HopConfig(workspace_layout="stacking")

    assert merge_configs(project, global_).workspace_layout == "stacking"


def test_load_global_config_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        '[bakends.devcontainer]\ncommand_prefix = "x"\n',
    )

    with pytest.raises(HopConfigError, match="unknown top-level key 'bakends'"):
        load_global_config(config_file)


# --- project config uses identical schema --------------------------------


def test_project_config_supports_identical_schema(tmp_path: Path) -> None:
    write(
        tmp_path / ".hop.toml",
        """
[backends.lima]
command_prefix = "lima shell default --"

[layouts.rails]
autostart = "test -f bin/rails"

[layouts.rails.windows.server]
command = "bin/dev"

[windows.worker]
command = "bin/jobs"
""",
    )

    config = load_project_config(tmp_path)

    assert config.backends == (
        BackendConfig(name="lima", command_prefix="lima shell default --"),
    )
    assert config.layouts == (
        LayoutConfig(
            name="rails",
            autostart="test -f bin/rails",
            windows=(WindowConfig(role="server", command="bin/dev"),),
        ),
    )
    assert config.windows == (WindowConfig(role="worker", command="bin/jobs"),)


# --- merge ---------------------------------------------------------------


def _backend(name: str, **fields: object) -> BackendConfig:
    return BackendConfig(name=name, **fields)  # type: ignore[arg-type]


def test_merge_backends_appends_global_only_after_project() -> None:
    project = HopConfig()
    global_ = HopConfig(backends=(_backend("alpha", command_prefix="a-prefix"),))

    merged = merge_backends(project, global_)

    assert tuple(b.name for b in merged) == ("alpha",)


def test_merge_backends_field_merges_per_field() -> None:
    project = HopConfig(backends=(_backend("alpha", default="true"),))
    global_ = HopConfig(
        backends=(
            _backend("alpha", command_prefix="prefix", default="test -f marker", prepare="prep"),
        )
    )

    merged = merge_backends(project, global_)

    assert merged == (
        _backend(
            "alpha",
            command_prefix="prefix",  # inherited
            default="true",  # project wins
            prepare="prep",  # inherited
        ),
    )


def test_merge_layouts_per_window_field_precedence() -> None:
    """Project layout windows merge per-role with global; global-only roles
    are appended after project-declared ones."""
    project = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                autostart="test -f bin/rails",
                windows=(
                    WindowConfig(role="server", autostart="false"),
                    WindowConfig(role="extra", command="extra-cmd"),
                ),
            ),
        )
    )
    global_ = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                autostart="ignored",
                windows=(
                    WindowConfig(role="server", command="bin/dev"),
                    WindowConfig(role="console", command="bin/rails console"),
                ),
            ),
        )
    )

    merged = merge_layouts(project, global_)

    assert merged == (
        LayoutConfig(
            name="rails",
            autostart="test -f bin/rails",  # project wins
            windows=(
                WindowConfig(role="server", command="bin/dev", autostart="false"),
                WindowConfig(role="extra", command="extra-cmd"),
                WindowConfig(role="console", command="bin/rails console"),
            ),
        ),
    )


def test_merge_top_level_windows_per_field() -> None:
    project = HopConfig(
        windows=(
            WindowConfig(role="editor", autostart="false"),
            WindowConfig(role="worker", command="project-jobs"),
        )
    )
    global_ = HopConfig(
        windows=(
            WindowConfig(role="editor", command="vim"),
            WindowConfig(role="logger", command="tail -f log"),
        )
    )

    merged = merge_windows(project, global_)

    assert merged == (
        WindowConfig(role="editor", command="vim", autostart="false"),  # merged per-field
        WindowConfig(role="worker", command="project-jobs"),  # project-only
        WindowConfig(role="logger", command="tail -f log"),  # global-only, appended last
    )


def test_merge_configs_combines_all_three_sections() -> None:
    project = HopConfig(
        backends=(_backend("alpha", default="true"),),
        layouts=(LayoutConfig(name="rails", autostart="proj"),),
        windows=(WindowConfig(role="worker", command="bin/jobs"),),
    )
    global_ = HopConfig(
        backends=(_backend("alpha", command_prefix="prefix"),),
        layouts=(LayoutConfig(name="vite", autostart="test -f vite.config.ts"),),
        windows=(WindowConfig(role="editor", autostart="false"),),
    )

    merged = merge_configs(project, global_)

    assert tuple(b.name for b in merged.backends) == ("alpha",)
    assert tuple(layout.name for layout in merged.layouts) == ("rails", "vite")
    assert tuple(window.role for window in merged.windows) == ("worker", "editor")


def test_default_global_config_path_uses_xdg_config_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/etc/xdg")
    assert default_global_config_path() == Path("/etc/xdg/hop/config.toml")


def test_default_global_config_path_falls_back_to_home_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/tester")
    assert default_global_config_path() == Path("/home/tester/.config/hop/config.toml")
