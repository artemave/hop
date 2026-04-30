from __future__ import annotations

from pathlib import Path

import pytest

from hop.config import (
    BackendConfig,
    HopConfig,
    HopConfigError,
    default_global_config_path,
    load_global_config,
    load_project_config,
    merge_backends,
)


def write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


# --- parsing -------------------------------------------------------------


def test_load_global_config_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_global_config(tmp_path / "missing.toml") == HopConfig()


def test_load_global_config_returns_empty_when_backends_table_missing(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "# no backends declared\n")

    assert load_global_config(config_file) == HopConfig()


def test_load_global_config_parses_full_backend(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
default  = "test -f docker-compose.dev.yml"
prepare  = "podman-compose -f docker-compose.dev.yml up -d devcontainer"
shell    = "podman-compose -f docker-compose.dev.yml exec devcontainer /usr/bin/zsh"
editor   = "podman-compose -f docker-compose.dev.yml exec devcontainer nvim --listen {listen_addr}"
teardown = "podman-compose -f docker-compose.dev.yml down"
workspace = "podman-compose -f docker-compose.dev.yml exec devcontainer pwd"
""",
    )

    assert load_global_config(config_file).backends == (
        BackendConfig(
            name="devcontainer",
            default="test -f docker-compose.dev.yml",
            prepare="podman-compose -f docker-compose.dev.yml up -d devcontainer",
            shell="podman-compose -f docker-compose.dev.yml exec devcontainer /usr/bin/zsh",
            editor="podman-compose -f docker-compose.dev.yml exec devcontainer nvim --listen {listen_addr}",
            teardown="podman-compose -f docker-compose.dev.yml down",
            workspace="podman-compose -f docker-compose.dev.yml exec devcontainer pwd",
        ),
    )


def test_load_global_config_parses_triple_quoted_multiline_command(tmp_path: Path) -> None:
    """Triple-quoted strings preserve newlines verbatim — sh handles them as
    line-continuation-style scripts when the user wants to spread a pipeline
    across lines for readability."""
    config_file = write(
        tmp_path / "config.toml",
        '''
[backends.devcontainer]
shell = "zsh"
editor = "nvim"
port_translate = """
podman ps -q \\
  --filter label=service=devcontainer \\
  | head -1 \\
  | xargs -r -I@ podman port @ {port} \\
  | cut -d: -f2
"""
''',
    )

    backends = load_global_config(config_file).backends
    assert backends[0].port_translate is not None
    assert "podman ps" in backends[0].port_translate
    assert "{port}" in backends[0].port_translate


def test_load_global_config_accepts_partial_entries(tmp_path: Path) -> None:
    """A backend without shell/editor parses fine — validation lives at use time."""
    config_file = write(
        tmp_path / "config.toml",
        '[backends.partial]\ndefault = "true"\n',
    )

    config = load_global_config(config_file)

    assert config.backends == (BackendConfig(name="partial", default="true"),)
    assert config.backends[0].is_runnable is False


def test_load_global_config_preserves_declaration_order(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.alpha]
shell = "a"
editor = "a"

[backends.beta]
shell = "b"
editor = "b"
""",
    )

    config = load_global_config(config_file)

    assert tuple(b.name for b in config.backends) == ("alpha", "beta")


def test_load_global_config_rejects_explicit_host_backend(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.host]
shell = "bash"
editor = "nvim"
""",
    )

    with pytest.raises(HopConfigError, match="reserved"):
        load_global_config(config_file)


def test_load_project_config_returns_empty_when_no_file(tmp_path: Path) -> None:
    assert load_project_config(tmp_path) == HopConfig()


def test_load_project_config_uses_same_schema_as_global(tmp_path: Path) -> None:
    write(
        tmp_path / ".hop.toml",
        """
[backends.devcontainer]
default = "true"

[backends.lima]
shell = "lima shell default -- /usr/bin/zsh"
editor = "lima shell default -- nvim --listen {listen_addr}"
""",
    )

    config = load_project_config(tmp_path)

    assert config.backends == (
        BackendConfig(name="devcontainer", default="true"),
        BackendConfig(
            name="lima",
            shell="lima shell default -- /usr/bin/zsh",
            editor="lima shell default -- nvim --listen {listen_addr}",
        ),
    )


def test_load_project_config_rejects_explicit_host_backend(tmp_path: Path) -> None:
    write(tmp_path / ".hop.toml", '[backends.host]\nshell = "bash"\neditor = "nvim"\n')

    with pytest.raises(HopConfigError, match="reserved"):
        load_project_config(tmp_path)


# --- merge ---------------------------------------------------------------


def _backend(name: str, **fields: object) -> BackendConfig:
    return BackendConfig(name=name, **fields)  # type: ignore[arg-type]


def test_merge_appends_global_only_entries_after_project() -> None:
    project = HopConfig()
    global_ = HopConfig(backends=(_backend("alpha", shell="a", editor="a"),))

    merged = merge_backends(project, global_)

    assert tuple(b.name for b in merged) == ("alpha",)


def test_merge_project_only_entries_appear_first() -> None:
    project = HopConfig(backends=(_backend("project-only", shell="p", editor="p"),))
    global_ = HopConfig(backends=(_backend("alpha", shell="a", editor="a"),))

    merged = merge_backends(project, global_)

    assert tuple(b.name for b in merged) == ("project-only", "alpha")


def test_merge_field_merges_same_name_with_project_winning() -> None:
    project = HopConfig(backends=(_backend("alpha", default="true"),))
    global_ = HopConfig(
        backends=(
            _backend(
                "alpha",
                shell="a-shell",
                editor="a-editor",
                default="test -f marker",
                prepare="a-prepare",
            ),
        )
    )

    merged = merge_backends(project, global_)

    assert merged == (
        _backend(
            "alpha",
            shell="a-shell",  # inherited from global
            editor="a-editor",  # inherited from global
            default="true",  # overridden by project
            prepare="a-prepare",  # inherited from global
        ),
    )


def test_merge_project_mention_moves_backend_to_project_slot() -> None:
    """A backend the project file mentions — even just as a partial override —
    moves to the project's slot in the auto-detect order."""
    project = HopConfig(backends=(_backend("beta", default="true"),))
    global_ = HopConfig(
        backends=(
            _backend("alpha", shell="a", editor="a"),
            _backend("beta", shell="b", editor="b", default="test -f .beta"),
        )
    )

    merged = merge_backends(project, global_)

    # beta moves to position 0 because the project mentioned it; alpha follows.
    assert tuple(b.name for b in merged) == ("beta", "alpha")


def test_default_global_config_path_uses_xdg_config_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/etc/xdg")
    assert default_global_config_path() == Path("/etc/xdg/hop/config.toml")


def test_default_global_config_path_falls_back_to_home_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/tester")
    assert default_global_config_path() == Path("/home/tester/.config/hop/config.toml")


def test_load_global_config_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", '[bakends.devcontainer]\nshell = "zsh"\n')

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


def test_load_global_config_rejects_unknown_backend_field(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
shell = "zsh"
shel = "typo"
""",
    )

    with pytest.raises(HopConfigError, match="backend 'devcontainer' has unknown field 'shel'"):
        load_global_config(config_file)


def test_load_global_config_rejects_legacy_list_form(tmp_path: Path) -> None:
    """Lists were the old format — error with a helpful message instead of silently misbehaving."""
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
shell = ["zsh"]
""",
    )

    with pytest.raises(HopConfigError, match="commands are now strings"):
        load_global_config(config_file)


def test_load_global_config_rejects_non_string_field(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
shell = 42
""",
    )

    with pytest.raises(HopConfigError, match="field 'shell' must be a string"):
        load_global_config(config_file)


def test_load_global_config_rejects_empty_command_string(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
shell = "   "
""",
    )

    with pytest.raises(HopConfigError, match="field 'shell' must not be empty"):
        load_global_config(config_file)


def test_load_global_config_parses_translate_fields(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
shell = "zsh"
editor = "nvim"
port_translate = "compose port devcontainer {port}"
host_translate = "echo myserver"
""",
    )

    backends = load_global_config(config_file).backends

    assert backends == (
        BackendConfig(
            name="devcontainer",
            shell="zsh",
            editor="nvim",
            port_translate="compose port devcontainer {port}",
            host_translate="echo myserver",
        ),
    )


def test_merge_translate_fields_independently_with_project_winning() -> None:
    project = HopConfig(
        backends=(_backend("alpha", port_translate="project-port"),),
    )
    global_ = HopConfig(
        backends=(
            _backend(
                "alpha",
                shell="zsh",
                editor="nvim",
                port_translate="global-port",
                host_translate="global-host",
            ),
        )
    )

    merged = merge_backends(project, global_)

    assert merged == (
        _backend(
            "alpha",
            shell="zsh",
            editor="nvim",
            port_translate="project-port",  # project wins
            host_translate="global-host",  # inherited from global
        ),
    )


def test_merge_preserves_partial_entries_in_result() -> None:
    """Validation lives at use time — merge keeps non-runnable entries so callers
    can decide how to surface them."""
    project = HopConfig(backends=(_backend("partial", default="true"),))
    global_ = HopConfig()

    merged = merge_backends(project, global_)

    assert merged == (_backend("partial", default="true"),)
    assert merged[0].is_runnable is False
