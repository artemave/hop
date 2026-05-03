from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from hop.errors import HopError

PROJECT_CONFIG_FILE = ".hop.toml"


class HopConfigError(HopError):
    """Raised when a hop config file (global or project) has an invalid shape."""


# Substitution placeholders supported in command strings.
PLACEHOLDER_PROJECT_ROOT = "{project_root}"
PLACEHOLDER_PORT = "{port}"

# Reserved backend name — refers to the implicit host backend, never a
# configured one. Auto-detect always falls back to host when no configured
# backend's `default` command succeeds.
HOST_BACKEND_NAME = "host"

# Built-in roles. shell + editor autostart by default; browser does not.
SHELL_ROLE = "shell"
EDITOR_ROLE = "editor"
BROWSER_ROLE = "browser"

# Per-window autostart accepts only these literal values; the gate is the
# layout's autostart probe, the top-level always-on rule, or the built-in
# default — never a per-window probe.
AUTOSTART_TRUE = "true"
AUTOSTART_FALSE = "false"
_AUTOSTART_VALUES = frozenset({AUTOSTART_TRUE, AUTOSTART_FALSE})

# Sway workspace layout modes accepted by the top-level `workspace_layout`
# setting. These are the only values sway's IPC `layout <mode>` command
# accepts; we reject anything else at parse time.
_WORKSPACE_LAYOUTS = frozenset({"splith", "splitv", "stacking", "tabbed"})


@dataclass(frozen=True, slots=True)
class WindowConfig:
    """A `[layouts.<name>.windows.<role>]` or top-level `[windows.<role>]` entry.

    ``command`` and ``autostart`` are both optional at the config layer so
    project entries can override only the field they care about. Defaults
    are filled in by the resolver.
    """

    role: str
    command: str | None = None
    autostart: str | None = None


@dataclass(frozen=True, slots=True)
class LayoutConfig:
    """A named `[layouts.<name>]` entry.

    ``autostart`` is required — the layout's gate probe, run via ``sh -c``
    in the project root at session entry. Each declared window inherits
    the layout's gate; per-window ``autostart = "false"`` opts a single
    window out of the matched layout (declared but not auto-launched).
    """

    name: str
    autostart: str | None = None
    windows: tuple[WindowConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class BackendConfig:
    """A named backend declared in a hop config file.

    Backends carry only lifecycle commands (``prepare`` / ``teardown`` /
    ``workspace`` / translate helpers) and a ``command_prefix`` that wraps
    every window's command launched in this backend's environment.

    Per-role launch commands live in top-level ``[layouts.<name>]`` and
    ``[windows.<role>]`` declarations, not on the backend.

    ``default`` is the auto-detect probe. Hop runs it in the project root
    and selects this backend when it exits 0. Backends without ``default``
    are not eligible for auto-detect — they can only be picked by name
    (``hop --backend``).

    ``workspace`` runs after ``prepare`` and its stdout (stripped) is the
    path inside the backend that maps to the host project root.
    """

    name: str
    default: str | None = None
    prepare: str | None = None
    teardown: str | None = None
    workspace: str | None = None
    port_translate: str | None = None
    host_translate: str | None = None
    command_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class HopConfig:
    """Parsed contents of one config file.

    Backends, layouts, and top-level windows all share a flat declaration
    list (one tuple per kind) so merge can preserve declaration order and
    project-wins-per-field semantics across same-named entries.

    ``workspace_layout`` is the sway workspace layout mode hop sets on a
    session's workspace at first entry — one of ``splith`` / ``splitv`` /
    ``stacking`` / ``tabbed``. ``None`` leaves sway's default behavior alone.
    """

    backends: tuple[BackendConfig, ...] = ()
    layouts: tuple[LayoutConfig, ...] = ()
    windows: tuple[WindowConfig, ...] = ()
    workspace_layout: str | None = None


def default_global_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "hop" / "config.toml"


def load_global_config(path: Path | None = None) -> HopConfig:
    target = path if path is not None else default_global_config_path()
    return _load_config_file(target)


def load_project_config(project_root: Path | str) -> HopConfig:
    target = Path(project_root).expanduser() / PROJECT_CONFIG_FILE
    return _load_config_file(target)


def merge_configs(project: HopConfig, global_: HopConfig) -> HopConfig:
    """Merge a project and global HopConfig into one, project-wins-per-field."""

    return HopConfig(
        backends=merge_backends(project, global_),
        layouts=merge_layouts(project, global_),
        windows=merge_windows(project, global_),
        workspace_layout=(
            project.workspace_layout if project.workspace_layout is not None else global_.workspace_layout
        ),
    )


def merge_backends(project: HopConfig, global_: HopConfig) -> tuple[BackendConfig, ...]:
    """Merge project and global backend declarations into a single ordered tuple.

    Project entries come first in the order they appear. Same-named entries
    are field-merged with project fields winning; the merged entry takes the
    project's slot. Global entries whose names weren't covered are appended
    after, preserving their declaration order.
    """

    by_name = {b.name: b for b in global_.backends}
    seen: set[str] = set()
    merged: list[BackendConfig] = []
    for p in project.backends:
        seen.add(p.name)
        g = by_name.get(p.name)
        merged.append(_merge_backend_pair(p, g) if g is not None else p)
    for g in global_.backends:
        if g.name in seen:
            continue
        merged.append(g)
    return tuple(merged)


def merge_layouts(project: HopConfig, global_: HopConfig) -> tuple[LayoutConfig, ...]:
    """Merge layouts the same way as backends — by name, project-wins-per-field.

    Inside a same-named layout, windows are merged by role (per-field).
    """

    by_name = {layout.name: layout for layout in global_.layouts}
    seen: set[str] = set()
    merged: list[LayoutConfig] = []
    for p in project.layouts:
        seen.add(p.name)
        g = by_name.get(p.name)
        merged.append(_merge_layout_pair(p, g) if g is not None else p)
    for g in global_.layouts:
        if g.name in seen:
            continue
        merged.append(g)
    return tuple(merged)


def merge_windows(project: HopConfig, global_: HopConfig) -> tuple[WindowConfig, ...]:
    """Merge top-level windows by role, project-wins-per-field."""

    by_role = {window.role: window for window in global_.windows}
    seen: set[str] = set()
    merged: list[WindowConfig] = []
    for p in project.windows:
        seen.add(p.role)
        g = by_role.get(p.role)
        merged.append(_merge_window_pair(p, g) if g is not None else p)
    for g in global_.windows:
        if g.role in seen:
            continue
        merged.append(g)
    return tuple(merged)


def _merge_backend_pair(project: BackendConfig, global_: BackendConfig) -> BackendConfig:
    return BackendConfig(
        name=project.name,
        default=project.default if project.default is not None else global_.default,
        prepare=project.prepare if project.prepare is not None else global_.prepare,
        teardown=project.teardown if project.teardown is not None else global_.teardown,
        workspace=project.workspace if project.workspace is not None else global_.workspace,
        port_translate=(project.port_translate if project.port_translate is not None else global_.port_translate),
        host_translate=(project.host_translate if project.host_translate is not None else global_.host_translate),
        command_prefix=(project.command_prefix if project.command_prefix is not None else global_.command_prefix),
    )


def _merge_layout_pair(project: LayoutConfig, global_: LayoutConfig) -> LayoutConfig:
    return LayoutConfig(
        name=project.name,
        autostart=project.autostart if project.autostart is not None else global_.autostart,
        windows=_merge_layout_windows(project.windows, global_.windows),
    )


def _merge_layout_windows(
    project_windows: tuple[WindowConfig, ...],
    global_windows: tuple[WindowConfig, ...],
) -> tuple[WindowConfig, ...]:
    by_role = {w.role: w for w in global_windows}
    seen: set[str] = set()
    merged: list[WindowConfig] = []
    for p in project_windows:
        seen.add(p.role)
        g = by_role.get(p.role)
        merged.append(_merge_window_pair(p, g) if g is not None else p)
    for g in global_windows:
        if g.role in seen:
            continue
        merged.append(g)
    return tuple(merged)


def _merge_window_pair(project: WindowConfig, global_: WindowConfig) -> WindowConfig:
    return WindowConfig(
        role=project.role,
        command=project.command if project.command is not None else global_.command,
        autostart=project.autostart if project.autostart is not None else global_.autostart,
    )


_BACKEND_FIELDS = (
    "default",
    "prepare",
    "teardown",
    "workspace",
    "port_translate",
    "host_translate",
    "command_prefix",
)
_LAYOUT_FIELDS = ("autostart", "windows")
_WINDOW_FIELDS = ("command", "autostart")
_LEGACY_FLAT_BACKEND_FIELDS = ("shell", "editor")
_LEGACY_BACKEND_WINDOWS_FIELD = "windows"
_TOP_LEVEL_KEYS = ("backends", "layouts", "windows", "workspace_layout")


def _load_config_file(path: Path) -> HopConfig:
    if not path.is_file():
        return HopConfig()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return _parse_top_level(data, source=path)


def _parse_top_level(data: dict[str, Any], *, source: Path) -> HopConfig:
    unknown_top = sorted(set(data) - set(_TOP_LEVEL_KEYS))
    if unknown_top:
        msg = f"{source}: unknown top-level key {unknown_top[0]!r}"
        raise HopConfigError(msg)

    backends = _parse_backends(data.get("backends"), source=source)
    layouts = _parse_layouts(data.get("layouts"), source=source)
    windows = _parse_top_level_windows(data.get("windows"), source=source)
    workspace_layout = _parse_workspace_layout(data.get("workspace_layout"), source=source)
    return HopConfig(
        backends=backends,
        layouts=layouts,
        windows=windows,
        workspace_layout=workspace_layout,
    )


def _parse_workspace_layout(raw: object, *, source: Path) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        msg = f"{source}: top-level 'workspace_layout' must be a string, got {type(raw).__name__}"
        raise HopConfigError(msg)
    if raw not in _WORKSPACE_LAYOUTS:
        accepted = ", ".join(sorted(_WORKSPACE_LAYOUTS))
        msg = f"{source}: top-level 'workspace_layout' must be one of {accepted}, got {raw!r}"
        raise HopConfigError(msg)
    return raw


def _parse_backends(raw: object, *, source: Path) -> tuple[BackendConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        msg = f"{source}: 'backends' must be a table, got {type(raw).__name__}"
        raise HopConfigError(msg)

    parsed: list[BackendConfig] = []
    for name, value in cast(dict[str, Any], raw).items():
        if name == HOST_BACKEND_NAME:
            msg = f"{source}: backend name {HOST_BACKEND_NAME!r} is reserved for the implicit host backend"
            raise HopConfigError(msg)
        if not isinstance(value, dict):
            msg = f"{source}: backend {name!r} must be a table, got {type(value).__name__}"
            raise HopConfigError(msg)
        parsed.append(_parse_backend(name, cast(dict[str, Any], value), source=source))
    return tuple(parsed)


def _parse_backend(name: str, table: dict[str, Any], *, source: Path) -> BackendConfig:
    legacy = [field for field in _LEGACY_FLAT_BACKEND_FIELDS if field in table]
    if legacy:
        flat = legacy[0]
        msg = (
            f"{source}: backend {name!r} has top-level field {flat!r}; that field was removed. "
            f"Built-in {flat!r} runs through the active backend's command_prefix; "
            f'override it with [windows.{flat}] command = "..." if you need a custom one.'
        )
        raise HopConfigError(msg)
    if _LEGACY_BACKEND_WINDOWS_FIELD in table:
        msg = (
            f"{source}: backend {name!r} has a 'windows' sub-table; that shape was removed. "
            "Per-role launch commands now live in top-level [layouts.<name>] or [windows.<role>] "
            "tables; backends carry only a command_prefix."
        )
        raise HopConfigError(msg)

    unknown = sorted(set(table) - set(_BACKEND_FIELDS))
    if unknown:
        msg = f"{source}: backend {name!r} has unknown field {unknown[0]!r}"
        raise HopConfigError(msg)
    return BackendConfig(
        name=name,
        default=_parse_command(table, key="default", context=f"backend {name!r}", source=source),
        prepare=_parse_command(table, key="prepare", context=f"backend {name!r}", source=source),
        teardown=_parse_command(table, key="teardown", context=f"backend {name!r}", source=source),
        workspace=_parse_command(table, key="workspace", context=f"backend {name!r}", source=source),
        port_translate=_parse_command(table, key="port_translate", context=f"backend {name!r}", source=source),
        host_translate=_parse_command(table, key="host_translate", context=f"backend {name!r}", source=source),
        command_prefix=_parse_command(table, key="command_prefix", context=f"backend {name!r}", source=source),
    )


def _parse_layouts(raw: object, *, source: Path) -> tuple[LayoutConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        msg = f"{source}: 'layouts' must be a table, got {type(raw).__name__}"
        raise HopConfigError(msg)

    parsed: list[LayoutConfig] = []
    for name, value in cast(dict[str, Any], raw).items():
        if not isinstance(value, dict):
            msg = f"{source}: layout {name!r} must be a table, got {type(value).__name__}"
            raise HopConfigError(msg)
        parsed.append(_parse_layout(name, cast(dict[str, Any], value), source=source))
    return tuple(parsed)


def _parse_layout(name: str, table: dict[str, Any], *, source: Path) -> LayoutConfig:
    unknown = sorted(set(table) - set(_LAYOUT_FIELDS))
    if unknown:
        msg = f"{source}: layout {name!r} has unknown field {unknown[0]!r}"
        raise HopConfigError(msg)
    autostart = _parse_command(table, key="autostart", context=f"layout {name!r}", source=source)
    windows = _parse_layout_windows(table.get("windows"), layout=name, source=source)
    return LayoutConfig(name=name, autostart=autostart, windows=windows)


def _parse_layout_windows(
    raw: object,
    *,
    layout: str,
    source: Path,
) -> tuple[WindowConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        msg = f"{source}: layout {layout!r} field 'windows' must be a table, got {type(raw).__name__}"
        raise HopConfigError(msg)

    parsed: list[WindowConfig] = []
    for role, value in cast(dict[str, Any], raw).items():
        if not isinstance(value, dict):
            msg = f"{source}: layout {layout!r} window {role!r} must be a table, got {type(value).__name__}"
            raise HopConfigError(msg)
        parsed.append(
            _parse_window(
                role,
                cast(dict[str, Any], value),
                context=f"layout {layout!r} window {role!r}",
                source=source,
            )
        )
    return tuple(parsed)


def _parse_top_level_windows(raw: object, *, source: Path) -> tuple[WindowConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        msg = f"{source}: 'windows' must be a table, got {type(raw).__name__}"
        raise HopConfigError(msg)

    parsed: list[WindowConfig] = []
    for role, value in cast(dict[str, Any], raw).items():
        if not isinstance(value, dict):
            msg = f"{source}: window {role!r} must be a table, got {type(value).__name__}"
            raise HopConfigError(msg)
        parsed.append(
            _parse_window(
                role,
                cast(dict[str, Any], value),
                context=f"window {role!r}",
                source=source,
            )
        )
    return tuple(parsed)


def _parse_window(role: str, table: dict[str, Any], *, context: str, source: Path) -> WindowConfig:
    unknown = sorted(set(table) - set(_WINDOW_FIELDS))
    if unknown:
        msg = f"{source}: {context} has unknown field {unknown[0]!r}"
        raise HopConfigError(msg)
    command = _parse_command(table, key="command", context=context, source=source)
    autostart_raw = _parse_command(table, key="autostart", context=context, source=source)
    if autostart_raw is not None and autostart_raw not in _AUTOSTART_VALUES:
        msg = (
            f"{source}: {context} field 'autostart' must be {AUTOSTART_TRUE!r} or {AUTOSTART_FALSE!r}, "
            f"got {autostart_raw!r}"
        )
        raise HopConfigError(msg)
    return WindowConfig(role=role, command=command, autostart=autostart_raw)


def _parse_command(
    table: dict[str, Any],
    *,
    key: str,
    context: str,
    source: Path,
) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, list):
        msg = (
            f"{source}: {context} field {key!r} is a list; "
            "commands are now strings (write the value as a single shell command). "
            'TOML triple-quoted strings ("""…""") work for multi-line pipelines.'
        )
        raise HopConfigError(msg)
    if not isinstance(value, str):
        msg = f"{source}: {context} field {key!r} must be a string, got {type(value).__name__}"
        raise HopConfigError(msg)
    if not value.strip():
        msg = f"{source}: {context} field {key!r} must not be empty"
        raise HopConfigError(msg)
    return value
