from __future__ import annotations

from pathlib import Path

from hop.config import (
    BackendConfig,
    HopConfig,
    load_global_config,
    load_project_config,
    merge_backends,
)

COMPOSE_PREFIX = ("podman-compose", "-f", "docker-compose.dev.yml")


def write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


# --- parsing -------------------------------------------------------------


def test_load_global_config_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_global_config(tmp_path / "missing.toml") == HopConfig()


def test_load_global_config_returns_empty_when_backends_table_missing(tmp_path: Path) -> None:
    config_file = write(tmp_path / "config.toml", "[other]\nkey = 'value'\n")

    assert load_global_config(config_file) == HopConfig()


def test_load_global_config_parses_full_backend(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.devcontainer]
default  = ["test", "-f", "docker-compose.dev.yml"]
prepare  = ["podman-compose", "-f", "docker-compose.dev.yml", "up", "-d", "devcontainer"]
shell    = ["podman-compose", "-f", "docker-compose.dev.yml", "exec", "devcontainer", "/usr/bin/zsh"]
editor   = ["podman-compose", "-f", "docker-compose.dev.yml", "exec",
            "devcontainer", "nvim", "--listen", "{listen_addr}"]
teardown = ["podman-compose", "-f", "docker-compose.dev.yml", "down"]
workspace = ["podman-compose", "-f", "docker-compose.dev.yml", "exec", "devcontainer", "pwd"]
""",
    )

    assert load_global_config(config_file).backends == (
        BackendConfig(
            name="devcontainer",
            default=("test", "-f", "docker-compose.dev.yml"),
            prepare=COMPOSE_PREFIX + ("up", "-d", "devcontainer"),
            shell=COMPOSE_PREFIX + ("exec", "devcontainer", "/usr/bin/zsh"),
            editor=COMPOSE_PREFIX + ("exec", "devcontainer", "nvim", "--listen", "{listen_addr}"),
            teardown=COMPOSE_PREFIX + ("down",),
            workspace=COMPOSE_PREFIX + ("exec", "devcontainer", "pwd"),
        ),
    )


def test_load_global_config_accepts_partial_entries(tmp_path: Path) -> None:
    """A backend without shell/editor parses fine — validation lives at use time."""
    config_file = write(
        tmp_path / "config.toml",
        "[backends.partial]\ndefault = ['true']\n",
    )

    config = load_global_config(config_file)

    assert config.backends == (
        BackendConfig(name="partial", default=("true",)),
    )
    assert config.backends[0].is_runnable is False


def test_load_global_config_preserves_declaration_order(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.alpha]
shell = ["a"]
editor = ["a"]

[backends.beta]
shell = ["b"]
editor = ["b"]
""",
    )

    config = load_global_config(config_file)

    assert tuple(b.name for b in config.backends) == ("alpha", "beta")


def test_load_global_config_ignores_explicit_host_backend(tmp_path: Path) -> None:
    config_file = write(
        tmp_path / "config.toml",
        """
[backends.host]
shell = ["bash"]
editor = ["nvim"]
""",
    )

    assert load_global_config(config_file) == HopConfig()


def test_load_project_config_returns_empty_when_no_file(tmp_path: Path) -> None:
    assert load_project_config(tmp_path) == HopConfig()


def test_load_project_config_uses_same_schema_as_global(tmp_path: Path) -> None:
    write(
        tmp_path / ".hop.toml",
        """
[backends.devcontainer]
default = ["true"]

[backends.lima]
shell = ["lima", "shell", "default", "--", "/usr/bin/zsh"]
editor = ["lima", "shell", "default", "--", "nvim", "--listen", "{listen_addr}"]
""",
    )

    config = load_project_config(tmp_path)

    assert config.backends == (
        BackendConfig(name="devcontainer", default=("true",)),
        BackendConfig(
            name="lima",
            shell=("lima", "shell", "default", "--", "/usr/bin/zsh"),
            editor=("lima", "shell", "default", "--", "nvim", "--listen", "{listen_addr}"),
        ),
    )


def test_load_project_config_ignores_explicit_host_backend(tmp_path: Path) -> None:
    write(tmp_path / ".hop.toml", "[backends.host]\nshell = ['bash']\neditor = ['nvim']\n")

    assert load_project_config(tmp_path) == HopConfig()


# --- merge ---------------------------------------------------------------


def _backend(name: str, **fields: object) -> BackendConfig:
    return BackendConfig(name=name, **fields)  # type: ignore[arg-type]


def test_merge_appends_global_only_entries_after_project() -> None:
    project = HopConfig()
    global_ = HopConfig(backends=(_backend("alpha", shell=("a",), editor=("a",)),))

    merged = merge_backends(project, global_)

    assert tuple(b.name for b in merged) == ("alpha",)


def test_merge_project_only_entries_appear_first() -> None:
    project = HopConfig(
        backends=(_backend("project-only", shell=("p",), editor=("p",)),)
    )
    global_ = HopConfig(
        backends=(_backend("alpha", shell=("a",), editor=("a",)),)
    )

    merged = merge_backends(project, global_)

    assert tuple(b.name for b in merged) == ("project-only", "alpha")


def test_merge_field_merges_same_name_with_project_winning() -> None:
    project = HopConfig(
        backends=(_backend("alpha", default=("true",)),)
    )
    global_ = HopConfig(
        backends=(
            _backend(
                "alpha",
                shell=("a-shell",),
                editor=("a-editor",),
                default=("test", "-f", "marker"),
                prepare=("a-prepare",),
            ),
        )
    )

    merged = merge_backends(project, global_)

    assert merged == (
        _backend(
            "alpha",
            shell=("a-shell",),       # inherited from global
            editor=("a-editor",),     # inherited from global
            default=("true",),         # overridden by project
            prepare=("a-prepare",),   # inherited from global
        ),
    )


def test_merge_project_mention_moves_backend_to_project_slot() -> None:
    """A backend the project file mentions — even just as a partial override —
    moves to the project's slot in the auto-detect order."""
    project = HopConfig(
        backends=(_backend("beta", default=("true",)),)
    )
    global_ = HopConfig(
        backends=(
            _backend("alpha", shell=("a",), editor=("a",)),
            _backend("beta", shell=("b",), editor=("b",), default=("test", "-f", ".beta")),
        )
    )

    merged = merge_backends(project, global_)

    # beta moves to position 0 because the project mentioned it; alpha follows.
    assert tuple(b.name for b in merged) == ("beta", "alpha")


def test_merge_preserves_partial_entries_in_result() -> None:
    """Validation lives at use time — merge keeps non-runnable entries so callers
    can decide how to surface them."""
    project = HopConfig(backends=(_backend("partial", default=("true",)),))
    global_ = HopConfig()

    merged = merge_backends(project, global_)

    assert merged == (_backend("partial", default=("true",)),)
    assert merged[0].is_runnable is False
