from __future__ import annotations

from typing import Protocol

from hop.kitty import KittyWindowContext
from hop.session import ProjectSession, resolve_project_session
from hop.targets import ResolvedFileTarget, ResolvedUrlTarget, resolve_visible_output_target


class OpenSelectionSwayAdapter(Protocol):
    def switch_to_workspace(self, workspace_name: str) -> None: ...


class OpenSelectionKittyAdapter(Protocol):
    def inspect_window(self, window_id: int) -> KittyWindowContext | None: ...


class OpenSelectionNeovimAdapter(Protocol):
    def open_target(self, session: ProjectSession, *, target: str) -> None: ...


class OpenSelectionBrowserAdapter(Protocol):
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None: ...


def open_selection_in_window(
    selection: str,
    *,
    source_window_id: int,
    sway: OpenSelectionSwayAdapter,
    kitty: OpenSelectionKittyAdapter,
    neovim: OpenSelectionNeovimAdapter,
    browser: OpenSelectionBrowserAdapter,
) -> ProjectSession | None:
    source_window = kitty.inspect_window(source_window_id)
    if source_window is None:
        return None

    project_root = source_window.project_root or source_window.cwd
    terminal_cwd = source_window.cwd or project_root
    if project_root is None or terminal_cwd is None:
        return None

    session = resolve_project_session(project_root)
    resolved_target = resolve_visible_output_target(
        selection,
        terminal_cwd=terminal_cwd,
        project_root=session.project_root,
    )
    if resolved_target is None:
        return None

    sway.switch_to_workspace(session.workspace_name)

    if isinstance(resolved_target, ResolvedUrlTarget):
        browser.ensure_browser(session, url=resolved_target.url)
    elif isinstance(resolved_target, ResolvedFileTarget):
        neovim.open_target(session, target=resolved_target.editor_target)

    return session
