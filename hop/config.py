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


# Substitution placeholders supported in command lists.
PLACEHOLDER_LISTEN_ADDR = "{listen_addr}"
PLACEHOLDER_PROJECT_ROOT = "{project_root}"

# Reserved backend name — refers to the implicit host backend, never a
# configured one. Auto-detect always falls back to host when no configured
# backend's `default` command succeeds.
HOST_BACKEND_NAME = "host"


@dataclass(frozen=True, slots=True)
class BackendConfig:
    """A named backend declared in a hop config file (global or project).

    Every command list is optional. A backend without ``shell`` and ``editor``
    is not runnable and is dropped at use time — partial entries are normal in
    project config files where they layer fields onto a same-named global
    backend.

    ``editor`` may include the literal placeholder ``{listen_addr}`` which hop
    substitutes at call time. Any command list may use ``{project_root}``.

    ``default`` is the auto-detect probe. Hop runs it in the project root and
    selects this backend when it exits 0. Backends without ``default`` are not
    eligible for auto-detect — they can only be picked by name (``hop --backend``).

    ``workspace`` runs after ``prepare`` and its stdout (stripped) is the path
    inside the backend that maps to the host project root, used for cwd
    translation in the open_selection kitten dispatch.
    """

    name: str
    shell: tuple[str, ...] | None = None
    editor: tuple[str, ...] | None = None
    default: tuple[str, ...] | None = None
    prepare: tuple[str, ...] | None = None
    teardown: tuple[str, ...] | None = None
    workspace: tuple[str, ...] | None = None

    @property
    def is_runnable(self) -> bool:
        return self.shell is not None and self.editor is not None


@dataclass(frozen=True, slots=True)
class HopConfig:
    """Ordered backend declarations from one config file."""

    backends: tuple[BackendConfig, ...] = ()


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
        merged.append(_merge_pair(p, g) if g is not None else p)
    for g in global_.backends:
        if g.name in seen:
            continue
        merged.append(g)
    return tuple(merged)


def _merge_pair(project: BackendConfig, global_: BackendConfig) -> BackendConfig:
    return BackendConfig(
        name=project.name,
        shell=project.shell if project.shell is not None else global_.shell,
        editor=project.editor if project.editor is not None else global_.editor,
        default=project.default if project.default is not None else global_.default,
        prepare=project.prepare if project.prepare is not None else global_.prepare,
        teardown=project.teardown if project.teardown is not None else global_.teardown,
        workspace=project.workspace if project.workspace is not None else global_.workspace,
    )


_BACKEND_FIELDS = ("shell", "editor", "default", "prepare", "teardown", "workspace")
_TOP_LEVEL_KEYS = ("backends",)


def _load_config_file(path: Path) -> HopConfig:
    if not path.is_file():
        return HopConfig()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return HopConfig(backends=_parse_backends(data, source=path))


def _parse_backends(data: dict[str, Any], *, source: Path) -> tuple[BackendConfig, ...]:
    unknown_top = sorted(set(data) - set(_TOP_LEVEL_KEYS))
    if unknown_top:
        msg = f"{source}: unknown top-level key {unknown_top[0]!r}"
        raise HopConfigError(msg)

    if "backends" not in data:
        return ()
    backends_table = data["backends"]
    if not isinstance(backends_table, dict):
        msg = f"{source}: 'backends' must be a table, got {type(backends_table).__name__}"
        raise HopConfigError(msg)

    parsed: list[BackendConfig] = []
    for name, raw in cast(dict[str, Any], backends_table).items():
        if name == HOST_BACKEND_NAME:
            msg = f"{source}: backend name {HOST_BACKEND_NAME!r} is reserved for the implicit host backend"
            raise HopConfigError(msg)
        if not isinstance(raw, dict):
            msg = f"{source}: backend {name!r} must be a table, got {type(raw).__name__}"
            raise HopConfigError(msg)
        parsed.append(_parse_backend(name, cast(dict[str, Any], raw), source=source))
    return tuple(parsed)


def _parse_backend(name: str, table: dict[str, Any], *, source: Path) -> BackendConfig:
    unknown = sorted(set(table) - set(_BACKEND_FIELDS))
    if unknown:
        msg = f"{source}: backend {name!r} has unknown field {unknown[0]!r}"
        raise HopConfigError(msg)
    return BackendConfig(
        name=name,
        shell=_parse_str_list(table, key="shell", backend=name, source=source),
        editor=_parse_str_list(table, key="editor", backend=name, source=source),
        default=_parse_str_list(table, key="default", backend=name, source=source),
        prepare=_parse_str_list(table, key="prepare", backend=name, source=source),
        teardown=_parse_str_list(table, key="teardown", backend=name, source=source),
        workspace=_parse_str_list(table, key="workspace", backend=name, source=source),
    )


def _parse_str_list(
    table: dict[str, Any],
    *,
    key: str,
    backend: str,
    source: Path,
) -> tuple[str, ...] | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, list):
        msg = f"{source}: backend {backend!r} field {key!r} must be a list of strings, got {type(value).__name__}"
        raise HopConfigError(msg)
    items = cast(list[Any], value)
    if not items:
        msg = f"{source}: backend {backend!r} field {key!r} must not be empty"
        raise HopConfigError(msg)
    for item in items:
        if not isinstance(item, str):
            msg = f"{source}: backend {backend!r} field {key!r} entries must be strings, got {type(item).__name__}"
            raise HopConfigError(msg)
    return tuple(items)
