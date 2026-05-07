from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from hop.config import (
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
# Third tuple element is the default `active` flag.
_BUILTIN_DEFAULTS: tuple[tuple[str, str, bool], ...] = (
    (SHELL_ROLE, "", True),
    (EDITOR_ROLE, "nvim", True),
    (BROWSER_ROLE, "", False),
)
_BUILTIN_ROLES = frozenset(role for role, _, _ in _BUILTIN_DEFAULTS)

CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class WindowSpec:
    """Resolved per-role window: command + activation decision.

    ``command`` may be empty for the built-in shell / browser sentinel — the
    launch path interprets that as "use platform default" (kitty's login shell
    or xdg-detected browser respectively).

    ``active`` is the final decision: layout probes have already
    been evaluated, top-level always-on rules applied, per-window opt-outs
    honored. Bootstrap iterates the resolved tuple and launches windows
    whose flag is true (with shell launching unconditionally regardless).
    """

    role: str
    command: str
    active: bool


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
    2. Each layout in declaration order whose ``activate`` probe exits 0,
       contributing its windows in declaration order.
    3. Top-level ``[windows.<role>]`` entries in declaration order.

    A per-window ``activate`` is a shell probe (same shape as the layout-
    level one); the window auto-launches when it exits 0. Windows whose
    merged ``command`` is empty are kept only for the built-in roles
    (where empty is a meaningful sentinel).
    """

    specs: dict[str, _MutableSpec] = {}
    # Pre-load built-in specs so layout / top-level windows that override
    # them by role find an existing spec to merge into. The order of
    # built-ins in the final output is decided after the config walk:
    # shell pinned slot 1, editor pinned slot 2, browser appended at the
    # end if the user never declared it.
    for role, command, active in _BUILTIN_DEFAULTS:
        specs[role] = _MutableSpec(role=role, command=command, active=active)

    declared_order: list[str] = []
    for layout in config.layouts:
        if not _layout_matches(layout, session=session, runner=runner):
            continue
        for window in layout.windows:
            _apply_layout_window(window, specs=specs, declared_order=declared_order, session=session, runner=runner)

    for window in config.windows:
        _apply_top_level_window(window, specs=specs, declared_order=declared_order, session=session, runner=runner)

    final_order: list[str] = [SHELL_ROLE, EDITOR_ROLE]
    final_order.extend(role for role in declared_order if role not in (SHELL_ROLE, EDITOR_ROLE))
    if BROWSER_ROLE not in final_order:
        final_order.append(BROWSER_ROLE)

    result: list[WindowSpec] = []
    for role in final_order:
        spec = specs[role]
        if spec.command is None and role not in _BUILTIN_ROLES:
            # A user-declared role with no resolved command — typically a
            # partial override that never picked up a command from any
            # layer. Skipping keeps `hop term --role X` from launching
            # something undefined. An explicit `command = ""` reaches
            # here as "" (not None) and is preserved as a shell-like spec.
            continue
        result.append(WindowSpec(role=spec.role, command=spec.command or "", active=spec.active))
    return tuple(result)


@dataclass
class _MutableSpec:
    role: str
    command: str | None
    active: bool


def _layout_matches(
    layout: LayoutConfig,
    *,
    session: ProjectSession,
    runner: CommandRunner,
) -> bool:
    if layout.activate is None:
        # A layout without an activate probe never activates. The parser
        # currently allows this (activate is optional at parse time so
        # project files can override only the windows of a same-named global
        # layout); a layout with no probe in either layer is effectively
        # off, which is safer than always-on.
        return False
    substituted = _substitute(layout.activate, session=session)
    result = runner(("sh", "-c", substituted), session.project_root)
    return result.returncode == 0


def _apply_layout_window(
    window: WindowConfig,
    *,
    specs: dict[str, _MutableSpec],
    declared_order: list[str],
    session: ProjectSession,
    runner: CommandRunner,
) -> None:
    if window.role not in declared_order:
        declared_order.append(window.role)
    existing = specs.get(window.role)
    active = _resolve_window_activate(window.activate, default=True, session=session, runner=runner)
    if existing is None:
        specs[window.role] = _MutableSpec(
            role=window.role,
            command=window.command,
            active=active,
        )
        return
    if window.command is not None:
        existing.command = window.command
    existing.active = active


def _apply_top_level_window(
    window: WindowConfig,
    *,
    specs: dict[str, _MutableSpec],
    declared_order: list[str],
    session: ProjectSession,
    runner: CommandRunner,
) -> None:
    if window.role not in declared_order:
        declared_order.append(window.role)
    existing = specs.get(window.role)
    if existing is None:
        active = _resolve_window_activate(window.activate, default=True, session=session, runner=runner)
        specs[window.role] = _MutableSpec(
            role=window.role,
            command=window.command,
            active=active,
        )
        return
    if window.command is not None:
        existing.command = window.command
    if window.activate is not None:
        existing.active = _resolve_window_activate(
            window.activate, default=existing.active, session=session, runner=runner
        )


def _resolve_window_activate(
    activate: str | None,
    *,
    default: bool,
    session: ProjectSession,
    runner: CommandRunner,
) -> bool:
    if activate is None:
        return default
    substituted = _substitute(activate, session=session)
    return runner(("sh", "-c", substituted), session.project_root).returncode == 0


def _substitute(template: str, *, session: ProjectSession) -> str:
    return template.replace(PLACEHOLDER_PROJECT_ROOT, shlex.quote(str(session.project_root)))


def find_window(windows: Sequence[WindowSpec], role: str) -> WindowSpec | None:
    for window in windows:
        if window.role == role:
            return window
    return None
