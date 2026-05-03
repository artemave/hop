from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from hop.config import (
    AUTOSTART_FALSE,
    AUTOSTART_TRUE,
    BROWSER_ROLE,
    EDITOR_ROLE,
    PLACEHOLDER_PROJECT_ROOT,
    SHELL_ROLE,
    HopConfig,
    LayoutConfig,
    WindowConfig,
)
from hop.session import ProjectSession

# Built-in defaults applied before any layout / top-level merge runs.
# - shell command "" is the platform-default sentinel (kitty's login shell on
#   host; ${SHELL:-sh} fallback when wrapped through a backend prefix).
# - editor command is plain `nvim`; the backend prefix wraps it.
# - browser command "" leaves SessionBrowserAdapter to xdg-detect a default.
_BUILTIN_DEFAULTS: tuple[tuple[str, str, bool], ...] = (
    (SHELL_ROLE, "", True),
    (EDITOR_ROLE, "nvim", True),
    (BROWSER_ROLE, "", False),
)
_BUILTIN_ROLES = frozenset(role for role, _, _ in _BUILTIN_DEFAULTS)

CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class WindowSpec:
    """Resolved per-role window: command + autostart-active decision.

    ``command`` may be empty for the built-in shell / browser sentinel — the
    launch path interprets that as "use platform default" (kitty's login shell
    or xdg-detected browser respectively).

    ``autostart_active`` is the final decision: layout probes have already
    been evaluated, top-level always-on rules applied, per-window opt-outs
    honored. Bootstrap iterates the resolved tuple and launches windows
    whose flag is true (with shell launching unconditionally regardless).
    """

    role: str
    command: str
    autostart_active: bool


def resolve_windows(
    config: HopConfig,
    session: ProjectSession,
    *,
    runner: CommandRunner,
) -> tuple[WindowSpec, ...]:
    """Compute the ordered windows for ``session`` from ``config``.

    Resolution order, layered with later sources overriding earlier ones for
    the same role:

    1. Built-in defaults (shell, editor, browser).
    2. Each layout in declaration order whose ``autostart`` probe exits 0,
       contributing its windows in declaration order.
    3. Top-level ``[windows.<role>]`` entries in declaration order.

    Within each step, a per-window ``autostart = "false"`` opts the window
    out of the autostart sweep (declared but inactive); ``"true"`` flips the
    default for browser. Windows whose merged ``command`` is empty are kept
    only for the built-in roles (where empty is a meaningful sentinel).
    """

    specs: dict[str, _MutableSpec] = {}
    order: list[str] = []
    for role, command, autostart_active in _BUILTIN_DEFAULTS:
        specs[role] = _MutableSpec(role=role, command=command, autostart_active=autostart_active)
        order.append(role)

    for layout in config.layouts:
        if not _layout_matches(layout, session=session, runner=runner):
            continue
        for window in layout.windows:
            _apply_layout_window(window, specs=specs, order=order)

    for window in config.windows:
        _apply_top_level_window(window, specs=specs, order=order)

    result: list[WindowSpec] = []
    for role in order:
        spec = specs[role]
        if spec.command is None and role not in _BUILTIN_ROLES:
            # A user-declared role with no resolved command — typically a
            # partial override that never picked up a command from any
            # layer. Skipping keeps `hop term --role X` from launching
            # something undefined. An explicit `command = ""` reaches
            # here as "" (not None) and is preserved as a shell-like spec.
            continue
        result.append(WindowSpec(role=spec.role, command=spec.command or "", autostart_active=spec.autostart_active))
    return tuple(result)


@dataclass
class _MutableSpec:
    role: str
    command: str | None
    autostart_active: bool


def _layout_matches(
    layout: LayoutConfig,
    *,
    session: ProjectSession,
    runner: CommandRunner,
) -> bool:
    if layout.autostart is None:
        # A layout without an autostart probe never activates. The parser
        # currently allows this (autostart is optional at parse time so
        # project files can override only the windows of a same-named global
        # layout); a layout with no probe in either layer is effectively
        # off, which is safer than always-on.
        return False
    substituted = _substitute(layout.autostart, session=session)
    result = runner(("sh", "-c", substituted), session.project_root)
    return result.returncode == 0


def _apply_layout_window(
    window: WindowConfig,
    *,
    specs: dict[str, _MutableSpec],
    order: list[str],
) -> None:
    existing = specs.get(window.role)
    autostart_active = window.autostart != AUTOSTART_FALSE
    if existing is None:
        specs[window.role] = _MutableSpec(
            role=window.role,
            command=window.command,
            autostart_active=autostart_active,
        )
        order.append(window.role)
        return
    if window.command is not None:
        existing.command = window.command
    existing.autostart_active = autostart_active


def _apply_top_level_window(
    window: WindowConfig,
    *,
    specs: dict[str, _MutableSpec],
    order: list[str],
) -> None:
    existing = specs.get(window.role)
    if existing is None:
        autostart_active = window.autostart != AUTOSTART_FALSE
        specs[window.role] = _MutableSpec(
            role=window.role,
            command=window.command,
            autostart_active=autostart_active,
        )
        order.append(window.role)
        return
    if window.command is not None:
        existing.command = window.command
    if window.autostart is not None:
        existing.autostart_active = window.autostart == AUTOSTART_TRUE


def _substitute(template: str, *, session: ProjectSession) -> str:
    return template.replace(PLACEHOLDER_PROJECT_ROOT, shlex.quote(str(session.project_root)))


def find_window(windows: Sequence[WindowSpec], role: str) -> WindowSpec | None:
    for window in windows:
        if window.role == role:
            return window
    return None
