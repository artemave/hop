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
activate                       = "test -f docker-compose.dev.yml"
prepare                        = "podman-compose -f docker-compose.dev.yml up -d devcontainer"
teardown                       = "podman-compose -f docker-compose.dev.yml down"
interactive_prefix                 = "podman-compose -f docker-compose.dev.yml exec devcontainer"
noninteractive_prefix  = "podman-compose -f docker-compose.dev.yml exec -T devcontainer"
""",
    )

    assert load_global_config(config_file).backends == (
        BackendConfig(
            name="devcontainer",
            activate="test -f docker-compose.dev.yml",
            prepare="podman-compose -f docker-compose.dev.yml up -d devcontainer",
            teardown="podman-compose -f docker-compose.dev.yml down",
            interactive_prefix="podman-compose -f docker-compose.dev.yml exec devcontainer",
            noninteractive_prefix="podman-compose -f docker-compose.dev.yml exec -T devcontainer",
        ),
    )


def test_load_global_config_rejects_legacy_workspace_field(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
interactive_prefix = "compose exec devcontainer"
workspace      = "compose exec devcontainer pwd"
""",
    )

    with pytest.raises(HopConfigError) as exc:
        load_global_config(config_file)

    message = str(exc.value)
    assert "workspace" in message
    assert "removed" in message
    assert "noninteractive_prefix" in message


def test_load_global_config_rejects_legacy_flat_shell_field(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        '[backends.devcontainer]\nshell = "zsh"\n',
    )

    with pytest.raises(HopConfigError) as exc:
        load_global_config(config_file)

    message = str(exc.value)
    assert "removed" in message
    assert "interactive_prefix" in message
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


def test_load_global_config_accepts_explicit_host_backend_override(tmp_path: Path) -> None:
    """``[backends.host]`` is no longer reserved — users can override hop's
    built-in host backend to wrap host shells through a sandbox/launcher.
    After merge, the user's host fields win over the built-in defaults and
    the entry keeps its global-declared slot in the auto-detect order."""
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.host]
interactive_prefix                = "firejail --net=none"
noninteractive_prefix = "firejail --net=none"
""",
    )

    config = load_global_config(config_file)
    [host] = [b for b in config.backends if b.name == "host"]
    assert host.interactive_prefix == "firejail --net=none"
    assert host.noninteractive_prefix == "firejail --net=none"

    merged = merge_configs(HopConfig(), config)
    [merged_host] = [b for b in merged.backends if b.name == "host"]
    # User-declared fields survive the merge with the built-in.
    assert merged_host.interactive_prefix == "firejail --net=none"
    # Built-in fields the user didn't override (activate="true") are inherited.
    assert merged_host.activate == "true"
    # host keeps its single slot in the merged list — no duplicate from the builtin.
    assert sum(1 for b in merged.backends if b.name == "host") == 1


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


def test_load_global_config_rejects_empty_interactive_prefix(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        '[backends.devcontainer]\ninteractive_prefix = "   "\n',
    )

    with pytest.raises(HopConfigError, match="field 'interactive_prefix' must not be empty"):
        load_global_config(config_file)


# --- layout parsing ------------------------------------------------------


def test_load_global_config_parses_layout_with_windows(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
activate = "test -f bin/rails"

[layouts.rails.windows.server]
command = "bin/dev"

[layouts.rails.windows.console]
command  = "bin/rails console"
activate = "false"
""",
    )

    assert load_global_config(config_file).layouts == (
        LayoutConfig(
            name="rails",
            activate="test -f bin/rails",
            windows=(
                WindowConfig(role="server", command="bin/dev"),
                WindowConfig(role="console", command="bin/rails console", activate="false"),
            ),
        ),
    )


def test_load_global_config_rejects_unknown_layout_field(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
activate = "true"
oops = "no"
""",
    )

    with pytest.raises(HopConfigError, match="layout 'rails' has unknown field 'oops'"):
        load_global_config(config_file)


def test_load_global_config_accepts_arbitrary_activate_probe_on_layout_window(tmp_path: Path) -> None:
    """Window-level ``activate`` is a shell probe, just like at the layout
    level; ``"true"`` and ``"false"`` keep their always-on / never-on meaning
    (they're real shell built-ins) and arbitrary probes work too."""
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
activate = "true"

[layouts.rails.windows.server]
command  = "bin/dev"
activate = "test -f bin/dev"
""",
    )

    config = load_global_config(config_file)

    assert config.layouts[0].windows[0].activate == "test -f bin/dev"


def test_load_global_config_rejects_unknown_window_field_in_layout(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
activate = "true"

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
activate = "false"

[windows.worker]
command = "bin/jobs"
""",
    )

    assert load_global_config(config_file).windows == (
        WindowConfig(role="editor", activate="false"),
        WindowConfig(role="worker", command="bin/jobs"),
    )


def test_load_global_config_accepts_arbitrary_activate_probe_on_top_level_window(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[windows.worker]
command  = "bin/jobs"
activate = "test -f bin/jobs"
""",
    )

    config = load_global_config(config_file)

    assert config.windows[0].activate == "test -f bin/jobs"


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


def test_load_global_config_parses_debug_log_true(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "debug_log = true\n")

    assert load_global_config(config_file).debug_log is True


def test_load_global_config_parses_debug_log_string_path(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", 'debug_log = "/tmp/hop.log"\n')

    assert load_global_config(config_file).debug_log == "/tmp/hop.log"


def test_load_global_config_debug_log_omitted_is_none(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "# nothing\n")

    assert load_global_config(config_file).debug_log is None


def test_load_global_config_rejects_non_bool_or_string_debug_log(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "debug_log = 42\n")

    with pytest.raises(HopConfigError, match="must be a boolean or a string path"):
        load_global_config(config_file)


def test_load_global_config_rejects_empty_debug_log_string(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", 'debug_log = "   "\n')

    with pytest.raises(HopConfigError, match="must not be empty"):
        load_global_config(config_file)


def test_merge_configs_debug_log_project_wins() -> None:
    project = HopConfig(debug_log="/proj/log")
    global_ = HopConfig(debug_log=True)

    assert merge_configs(project, global_).debug_log == "/proj/log"


def test_merge_configs_debug_log_inherits_from_global() -> None:
    project = HopConfig()
    global_ = HopConfig(debug_log=True)

    assert merge_configs(project, global_).debug_log is True


def test_load_global_config_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        '[bakends.devcontainer]\ninteractive_prefix = "x"\n',
    )

    with pytest.raises(HopConfigError, match="unknown top-level key 'bakends'"):
        load_global_config(config_file)


def test_load_global_config_rejects_non_table_backends_value(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "backends = 'oops'\n")

    with pytest.raises(HopConfigError, match="'backends' must be a table"):
        load_global_config(config_file)


def test_load_global_config_rejects_non_table_backend_entry(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends]
bogus = "not-a-table"
""",
    )

    with pytest.raises(HopConfigError, match="backend 'bogus' must be a table"):
        load_global_config(config_file)


def test_load_global_config_rejects_non_table_layouts_value(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "layouts = 'oops'\n")

    with pytest.raises(HopConfigError, match="'layouts' must be a table"):
        load_global_config(config_file)


def test_load_global_config_rejects_non_table_layout_entry(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts]
bogus = "not-a-table"
""",
    )

    with pytest.raises(HopConfigError, match="layout 'bogus' must be a table"):
        load_global_config(config_file)


def test_load_global_config_parses_layout_without_windows_subtable(tmp_path: Path) -> None:
    """A layout can be declared with just an `activate` probe and no
    windows — useful in a project file overriding only the activate of a
    same-named global layout. The empty windows tuple merges naturally."""
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
activate = "false"
""",
    )

    layouts = load_global_config(config_file).layouts
    assert layouts == (LayoutConfig(name="rails", activate="false", windows=()),)


def test_load_global_config_rejects_non_table_layout_windows_value(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
activate = "true"
windows = "oops"
""",
    )

    with pytest.raises(HopConfigError, match="layout 'rails' field 'windows' must be a table"):
        load_global_config(config_file)


def test_load_global_config_rejects_non_table_layout_window_entry(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
activate = "true"

[layouts.rails.windows]
server = "not-a-table"
""",
    )

    with pytest.raises(HopConfigError, match="layout 'rails' window 'server' must be a table"):
        load_global_config(config_file)


def test_load_global_config_rejects_non_table_top_level_windows_value(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "windows = 'oops'\n")

    with pytest.raises(HopConfigError, match="'windows' must be a table"):
        load_global_config(config_file)


def test_load_global_config_rejects_non_table_top_level_window_entry(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[windows]
server = "not-a-table"
""",
    )

    with pytest.raises(HopConfigError, match="window 'server' must be a table"):
        load_global_config(config_file)


def test_load_global_config_rejects_legacy_list_command_form(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
prepare = ["compose", "up"]
""",
    )

    with pytest.raises(HopConfigError, match="commands are now strings"):
        load_global_config(config_file)


def test_load_global_config_rejects_non_string_command(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
prepare = 42
""",
    )

    with pytest.raises(HopConfigError, match="must be a string"):
        load_global_config(config_file)


def test_load_global_config_rejects_empty_command(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
prepare = "   "
""",
    )

    with pytest.raises(HopConfigError, match="must not be empty"):
        load_global_config(config_file)


def test_load_global_config_allows_empty_window_command(tmp_path: Path) -> None:
    """`command = ""` on a window is the explicit shell-like sentinel —
    accepted at parse time and surfaced as "" so the resolver can keep it."""
    config_file = write(
        tmp_path / "config.toml",
        """
[layouts.rails]
activate = "test -f bin/rails"

[layouts.rails.windows.test]
command = ""
""",
    )

    config = load_global_config(config_file)

    assert config.layouts[0].windows[0].command == ""


# --- project config uses identical schema --------------------------------


def test_project_config_supports_identical_schema(tmp_path: Path) -> None:
    write(
        tmp_path / ".hop.toml",
        """
[backends.lima]
interactive_prefix = "lima shell default --"

[layouts.rails]
activate = "test -f bin/rails"

[layouts.rails.windows.server]
command = "bin/dev"

[windows.worker]
command = "bin/jobs"
""",
    )

    config = load_project_config(tmp_path)

    assert config.backends == (BackendConfig(name="lima", interactive_prefix="lima shell default --"),)
    assert config.layouts == (
        LayoutConfig(
            name="rails",
            activate="test -f bin/rails",
            windows=(WindowConfig(role="server", command="bin/dev"),),
        ),
    )
    assert config.windows == (WindowConfig(role="worker", command="bin/jobs"),)


# --- merge ---------------------------------------------------------------


def _backend(name: str, **fields: object) -> BackendConfig:
    return BackendConfig(name=name, **fields)  # type: ignore[arg-type]


def test_merge_backends_appends_global_only_after_project() -> None:
    project = HopConfig()
    global_ = HopConfig(backends=(_backend("alpha", interactive_prefix="a-prefix"),))

    merged = merge_backends(project, global_)

    assert tuple(b.name for b in merged) == ("alpha",)


def test_merge_backends_field_merges_per_field() -> None:
    project = HopConfig(backends=(_backend("alpha", activate="true"),))
    global_ = HopConfig(
        backends=(_backend("alpha", interactive_prefix="prefix", activate="test -f marker", prepare="prep"),)
    )

    merged = merge_backends(project, global_)

    assert merged == (
        _backend(
            "alpha",
            interactive_prefix="prefix",  # inherited
            activate="true",  # project wins
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
                activate="test -f bin/rails",
                windows=(
                    WindowConfig(role="server", activate="false"),
                    WindowConfig(role="extra", command="extra-cmd"),
                ),
            ),
        )
    )
    global_ = HopConfig(
        layouts=(
            LayoutConfig(
                name="rails",
                activate="ignored",
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
            activate="test -f bin/rails",  # project wins
            windows=(
                WindowConfig(role="server", command="bin/dev", activate="false"),
                WindowConfig(role="extra", command="extra-cmd"),
                WindowConfig(role="console", command="bin/rails console"),
            ),
        ),
    )


def test_merge_top_level_windows_per_field() -> None:
    project = HopConfig(
        windows=(
            WindowConfig(role="editor", activate="false"),
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
        WindowConfig(role="editor", command="vim", activate="false"),  # merged per-field
        WindowConfig(role="worker", command="project-jobs"),  # project-only
        WindowConfig(role="logger", command="tail -f log"),  # global-only, appended last
    )


def test_merge_configs_combines_all_three_sections() -> None:
    project = HopConfig(
        backends=(_backend("alpha", activate="true"),),
        layouts=(LayoutConfig(name="rails", activate="proj"),),
        windows=(WindowConfig(role="worker", command="bin/jobs"),),
    )
    global_ = HopConfig(
        backends=(_backend("alpha", interactive_prefix="prefix"),),
        layouts=(LayoutConfig(name="vite", activate="test -f vite.config.ts"),),
        windows=(WindowConfig(role="editor", activate="false"),),
    )

    merged = merge_configs(project, global_)

    # Hop's built-in ``host`` always rides at the bottom of the merged list.
    assert tuple(b.name for b in merged.backends) == ("alpha", "host")
    assert tuple(layout.name for layout in merged.layouts) == ("rails", "vite")
    assert tuple(window.role for window in merged.windows) == ("worker", "editor")


def test_default_global_config_path_uses_xdg_config_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/etc/xdg")
    assert default_global_config_path() == Path("/etc/xdg/hop/config.toml")


def test_default_global_config_path_falls_back_to_home_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/tester")
    assert default_global_config_path() == Path("/home/tester/.config/hop/config.toml")
