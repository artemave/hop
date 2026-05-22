# Drop the editor unmarked-fallback claim

Remove the `_find_editor_window` fallback that auto-claims an unmarked editor-class window on the session's workspace. Editor lookup becomes mark-only.

## Background

`hop/editor.py:_find_editor_window` (currently lines 340-364) does two things:

1. Look up the session's editor by Sway mark `hop:editor:<session>`.
2. If no marked window is found, fall back to claiming any unmarked window whose `app_id == hop:editor` (or `window_class == hop:editor`) on `p:<session>`, then re-marking it.

Step 2 is recovery for "the editor exists but somehow lost its mark." The comment cites two scenarios: "first sighting" and "after a hop crash that lost the mark."

- "First sighting" is largely already covered by `_adopt_new_editor_window` (`editor.py:366-396`), which marks the window inside the same launch flow. The fallback only fires if that polling step times out or somehow misses the window.
- "After a hop crash that lost the mark" is misleading — Sway marks live in Sway's runtime state, not in hop. A hop crash doesn't drop them. A full Sway restart would, but in that case the kitty editor processes are also gone.
- The remaining scenario is an external launch of `kitty --class hop:editor`. Niche.

The fallback was introduced in commit `d10e2b1` alongside the move from `app_id == hop:<session>:editor` to mark-based identity. No commit since has tuned its timeout or referenced an observed launch race. There is no incident evidence behind it — it is defensive code for a hypothetical state.

This is at odds with the project's "Avoid defensive code. Prefer to fail with exception." rule (`/home/artem/CLAUDE.md`). And it conflicts with the broader mark model being established in `reconcile-editor-and-browser-marks-on-sway-window-events.md`: marks reflect current session membership, set only by hop's launch paths and cleared only by the reconciler. Letting `hop edit` silently re-mark a candidate that the reconciler had previously demarked (because the user raw-moved it out and back in) reintroduces "second-chance adoption" through the back door.

Drop the fallback. Lookup becomes mark-only. If the launch race ever bites in practice, the right fix is at `_adopt_new_editor_window` (tag inside the launch path so the post-launch poll can't lose the window), not a recovery branch in the lookup function.

## Codebase Context

- **Editor lookup**: `hop/editor.py:_find_editor_window` (lines 340-364). The mark-based branch is lines 345-350; the fallback branch is lines 352-363.
- **Editor mark prefix**: `EDITOR_MARK_PREFIX` (search `editor.py`) — `hop:editor:`. `_editor_mark(session)` formats `hop:editor:<session_name>`.
- **Launch-time marking**: `hop/editor.py:_adopt_new_editor_window` (lines 366-396) handles freshly-launched editors. It polls for a window with `app_id == hop:editor`, moves it to `p:<session>` if drifted, and calls `self._sway.mark_window(window.id, _editor_mark(session))`. Timeout is `EDITOR_READY_TIMEOUT_SECONDS = 5.0` (editor.py:18). On timeout it raises `NeovimCommandError` — the launch fails loud, no silent state.
- **Browser symmetry**: `hop/browser.py:_find_session_window` (lines 164-174) is already mark-only — no fallback to remove. The browser side stays unchanged.
- **`hop kill`**: `hop/commands/kill.py:53-54` filters by editor/browser marks. With the fallback gone, an unmarked editor-class window is no longer "the session's editor," so `hop kill` correctly skips it. Matches the intended new semantics.

## Design

Replace the body of `_find_editor_window`:

```python
def _find_editor_window(self, session: ProjectSession) -> SwayWindow | None:
    mark = _editor_mark(session)
    marked = [window for window in self._sway.list_windows() if mark in window.marks]
    if not marked:
        return None
    return min(marked, key=lambda candidate: candidate.id)
```

That's the entire change in `editor.py`. Drop the comment block above the function describing "first sighting / after a hop crash" — the new lookup is self-explanatory.

### Consequence for `hop edit`

Before: `hop edit` on a session with an unmarked editor-class window on `p:<session>` adopted it.

After: `hop edit` on the same state launches a fresh editor. The unmarked window stays where it is, untouched. The user can close it manually if they don't want it.

This is the desired behavior under the mark rules. If the user wants the unmarked editor to be the session's editor again, they can kill it and re-run `hop edit`.

## Test Plan

1. **Mark-only lookup**: a `StubSwayAdapter` reports one window with `mark = hop:editor:demo` and one with `app_id == hop:editor` but no editor mark, both on `p:demo`. `_find_editor_window(session=demo_session)` returns the marked one.
2. **No marked window → None**: a `StubSwayAdapter` reports only an unmarked `app_id == hop:editor` window on `p:demo`. `_find_editor_window(session=demo_session)` returns `None`. (Previously claimed and re-marked; now untouched.)
3. **`hop edit` launches a fresh editor when no marked window exists** (`tests/test_edit_commands.py` or similar): pre-state has the unmarked editor-class window on `p:demo`. `hop edit` invocation produces a launch call on `StubKittyAdapter`.
4. **Existing marked-editor tests still pass**: the mark-based branch is preserved verbatim; existing assertions about marked-window discovery, focus, and drift recovery remain green.

## Out of scope

- Reconciler / `window`-event subscription. Sibling task `reconcile-editor-and-browser-marks-on-sway-window-events.md`.
- Tagging the editor inside the launch path to eliminate the polling race entirely. Possible future work if a race is ever observed; not needed today.

## Task Type

implement

## Principles

- [One shared editor per session](../principles/one-shared-editor-per-session.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [No defensive don'ts](../principles/no-defensive-donts.md)

## Blocked By

(none)

## Definition of Done

- `hop/editor.py:_find_editor_window` is mark-only — the unmarked-app_id fallback branch (current lines 352-363) and its descriptive comment block are deleted.
- `hop edit` on a session with no marked editor launches a fresh one, even if an unmarked editor-class window happens to be on `p:<session>`.
- All listed tests pass; existing editor tests stay green.
- `make` is green.
