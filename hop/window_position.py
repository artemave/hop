from __future__ import annotations

from typing import Mapping, Protocol, Sequence, cast

from hop.config import BROWSER_ROLE
from hop.sway import SwayWindow

# A window's sticky ``position`` orders it like a signed index into the row
# of windows on a session's workspace: a positive position counts from the
# left (1 = leftmost), a negative position counts from the right (-1 =
# rightmost). ``None`` (no configured position) fills the middle, in
# creation order.
_LEFT_BUCKET = 0
_MIDDLE_BUCKET = 1
_RIGHT_BUCKET = 2

# Sway `app_id` prefix hop's kitty windows carry (`hop:<role>`). The session
# browser isn't a kitty window and carries no such app_id — it's identified
# by the session's browser mark instead (see `role_of_window`).
_ROLE_APP_ID_PREFIX = "hop:"


def compute_insert_index(
    existing_roles_in_order: Sequence[str],
    position_by_role: Mapping[str, int | None],
    new_position: int | None,
) -> int:
    """0-based index at which a newly-created window should land among
    ``existing_roles_in_order`` (left-to-right, not including the new
    window itself) so the row respects every role's sticky ``position``.

    Only counts where the new window sorts; it never reorders the existing
    windows relative to each other, so the caller can realize the result as
    a single insert (shifting everything from the target index onward by
    one slot) rather than a full re-sort of the row.
    """

    new_bucket, new_key = _bucket(new_position)
    index = 0
    for existing_role in existing_roles_in_order:
        bucket, key = _bucket(position_by_role.get(existing_role))
        if bucket == _MIDDLE_BUCKET and new_bucket == _MIDDLE_BUCKET:
            # Unpositioned windows keep creation order: an existing one
            # always sits before a new one.
            index += 1
            continue
        if bucket < new_bucket or (bucket == new_bucket and key <= new_key):
            index += 1
    return index


def _bucket(position: int | None) -> tuple[int, int]:
    if position is None:
        return (_MIDDLE_BUCKET, 0)
    if position > 0:
        return (_LEFT_BUCKET, position)
    return (_RIGHT_BUCKET, position)


def role_of_window(window: SwayWindow, *, browser_mark: str) -> str | None:
    """The hop role a Sway window plays in the sticky-position row, or
    ``None`` when it isn't one hop manages (a third-party window).

    Kitty role terminals (shell, editor, and any user-declared role) carry
    a ``hop:<role>`` app_id. The session browser is launched outside kitty
    and carries no such app_id, so it's identified by the session's browser
    mark instead — the same mark ``SessionBrowserAdapter`` uses to claim it.
    """

    if window.app_id is not None and window.app_id.startswith(_ROLE_APP_ID_PREFIX):
        return window.app_id.removeprefix(_ROLE_APP_ID_PREFIX)
    if browser_mark in window.marks:
        return BROWSER_ROLE
    return None


class StickyPositionSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...

    def move_container_left(self, window_id: int) -> None: ...

    def move_container_right(self, window_id: int) -> None: ...


def apply_sticky_position(
    sway: StickyPositionSwayAdapter,
    *,
    workspace_name: str,
    window_id: int,
    role: str,
    position_by_role: Mapping[str, int | None],
    browser_mark: str,
) -> None:
    """Move a just-created window into its sticky ``position`` slot.

    Shared by the kitty adapter (role terminals) and the browser adapter
    (the session browser) so both place newly-created windows into one
    consistent row spanning both window kinds. Only the new window
    (``window_id``) is moved — existing windows keep their relative order,
    shifted by one slot to make room. A no-op when nobody in the session has
    a configured position, so a config that never sets ``position`` leaves
    Sway's default tiling placement untouched.

    Best-effort, like the workspace-adopt steps that precede this: relies on
    ``move left`` / ``move right`` swapping with the adjacent sibling in the
    workspace's tiling container, which only lines up with the row model
    when the workspace holds just hop-managed windows (role terminals plus
    the session browser) — a stray third-party window interleaved in the
    tree can throw off the step count.
    """

    new_position = position_by_role.get(role)
    workspace_windows = [
        window
        for window in sway.list_windows()
        if window.workspace_name == workspace_name and role_of_window(window, browser_mark=browser_mark) is not None
    ]
    others = [window for window in workspace_windows if window.id != window_id]
    has_any_position = new_position is not None or any(
        position_by_role.get(cast(str, role_of_window(window, browser_mark=browser_mark))) is not None
        for window in others
    )
    if not has_any_position:
        return

    current_index = next((index for index, window in enumerate(workspace_windows) if window.id == window_id), None)
    if current_index is None:
        return

    other_roles = [cast(str, role_of_window(window, browser_mark=browser_mark)) for window in others]
    target_index = compute_insert_index(other_roles, position_by_role, new_position)
    delta = target_index - current_index
    if delta > 0:
        for _ in range(delta):
            sway.move_container_right(window_id)
    elif delta < 0:
        for _ in range(-delta):
            sway.move_container_left(window_id)
