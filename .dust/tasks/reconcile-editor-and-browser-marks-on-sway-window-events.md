# Reconcile editor and browser marks on Sway window events

Subscribe `hopd` to Sway `window` events and clear editor/browser session marks whenever the marked window leaves `p:<session>`.

## Background

Today, the editor and browser are identified across `hop` runs by per-session Sway marks (`hop:editor:<session>`, `_hop_browser:<session>`). Once set at launch time (`hop/editor.py:_adopt_new_editor_window`, `hop/browser.py:_launch_session_browser`), the marks stick to the window forever — even if the user uses raw Sway keybindings to move the window onto an unrelated workspace. Subsequent `hop edit` / `hop browser` calls find the marked window from any workspace and **yank it back** onto `p:<session>` via `move_window_to_workspace` (`editor.py:386-390`, `browser.py:120-121,144-145`).

That makes raw Sway moves *non-final*: the user can't say "this editor isn't part of the session anymore" with the same keybinding they use for every other window. Hop fights them.

The desired model is the opposite. Marks should reflect current workspace placement: raw Sway move of the editor or browser off `p:<session>` clears the corresponding mark, and the window becomes a regular Sway window with no hop affiliation. Next `hop edit` / `hop browser` launches a fresh one.

This task wires that rule into the daemon. Marks remain set exclusively by `hop edit` / `hop browser` at launch time, as they are today.

## Codebase Context

- **Daemon main loop**: `hop/daemon.py:_run_main_loop` (line 137) subscribes to `workspace` events only via `sway.subscribe_to_workspace_events()` (sway.py:228). On each event it sweeps stale sessions and regenerates vicinae scripts.
- **Sway IPC adapter**: `hop/sway.py:SwayIpcAdapter` exposes `list_windows()`, `subscribe_to_workspace_events()`, and the new `subscribe_to_workspace_events` method body shows the subscribe protocol shape (line 228-231). Sway's `subscribe` payload accepts a JSON array of event types — to add `window` we'd either extend the existing method to subscribe to both, or add a separate `subscribe_to_window_events` and merge the two iterators.
- **Editor mark plumbing**: `hop/editor.py:EDITOR_MARK_PREFIX` (search the file — used by `_editor_mark`). Marks have the shape `hop:editor:<session_name>`.
- **Browser mark plumbing**: `hop/browser.py:DEFAULT_BROWSER_MARK_PREFIX = "_hop_browser:"` (line 22), `_session_browser_mark` produces `_hop_browser:<session_name>`.
- **Mark-removal IPC**: Sway supports `[con_id=N] unmark <mark>`. `SwayIpcAdapter` does not have an `unmark_window` method yet — add it analogous to `mark_window` (line 219).
- **`hop kill`** (`hop/commands/kill.py:53-54`) finds session windows via the same marks. With the new rule, a raw-moved editor/browser has lost its mark, so `hop kill` won't touch it. That's the right outcome — those windows are no longer session-affiliated.
- **Sibling cleanup task**: `drop-the-editor-unmarked-fallback-claim.md` removes the `_find_editor_window` fallback (editor.py:352-363) that auto-claims unmarked editor-class windows on the session ws. That fallback is independent of the reconciler — it ships separately. Together the two changes give: marks reflect current placement, and `hop edit` never silently adopts an unmarked candidate.

### What about role terminals?

Role terminals (`hop:<role>` app_id) are out of scope for this task. They have **no marks**; identity is `app_id == hop:<role>` filtered by `workspace_name == p:<session>` in `hop/commands/term.py:50-55`. The workspace filter already gives "moved out → orphaned, moved back in → reachable again" semantics for free. The new rule is mark-based and doesn't apply to role terminals.

## Design

### Subscribe to `window` events

Extend `SwayIpcAdapter` with a `subscribe_to_window_events` method (analogue of `subscribe_to_workspace_events`, payload `["window"]`). The hopd main loop subscribes to both event streams; each event from either stream triggers a reconcile sweep.

Two practical options for combining the streams:

1. **One subscription, multiple types**: change `subscribe_to_workspace_events` to accept a list of event types and subscribe to `["workspace", "window"]` in a single transport call. The yielded events carry the event type in the message header (sway sets `(EVENT_TYPE_FLAG | event_id)` in the response type byte) — needs a small `_read_message` change to surface the type to callers so hopd can branch.
2. **Two threads**: keep `subscribe_to_workspace_events` as-is and add a parallel thread for window events, mirroring `_start_bridge_acceptor` (`daemon.py:93-116`). Each thread invokes its own reconcile callback on the main `sway` adapter (the adapter is stateless on its IPC side; each method opens its own transport socket).

Recommended: option 2. Keeps the existing subscription untouched, isolates the new code path, and the threading shape is already used in hopd for the bridge acceptor. The window-event thread is a `daemon=True` thread so it dies with the process.

### Reconciler logic

A single function `reconcile_marks(sway)` walks `sway.list_windows()` and, for every window:

- For each mark on the window starting with `hop:editor:` or `_hop_browser:`, parse the session name out of the mark, compute the expected workspace `p:<session>`, and if `window.workspace_name != expected`, call `sway.unmark_window(window.id, mark)`.

This is workspace-event-trigger-safe (idempotent: marks already in the correct place stay; mismatched marks get cleared). The function is called on every window event.

Where to call it from hopd's main flow:

- Once at startup (mirrors the existing `regenerate(...)` startup call at `daemon.py:161-166`) — catches any drift from before the daemon was running.
- On every `window` event in the new thread.
- (Optional) Also on workspace events — cheap and catches edge cases. Probably not necessary if window-event coverage is complete.

### Adapter additions

`SwayIpcAdapter.unmark_window(window_id, mark)` — `[con_id=N] unmark <mark>`, analogous to `mark_window` (sway.py:219).

`SwayIpcAdapter.subscribe_to_window_events()` — like `subscribe_to_workspace_events` but with payload `["window"]`.

### Spec / docs

`hop_spec.md` needs a note that the session's editor and browser are identified by Sway marks, and that **raw Sway moves out of `p:<session>` clear those marks** (the window stops being the session's editor/browser). Cross-link from the `hop edit` / `hop browser` sections.

`README.md` — if it describes the editor/browser session-membership rule, update accordingly. If not, no change.

## Test Plan

Real Sway IPC integration is hard to test in unit form; the existing tests use stubs for the Sway adapter (e.g. `tests/test_editor.py:66`-ish, `tests/test_daemon.py`). Follow that pattern.

1. **Reconciler clears editor mark on move-out** (`tests/test_daemon.py` or new `tests/test_reconciler.py`): a `StubSwayAdapter` with a window holding mark `hop:editor:demo` and `workspace_name = "p:other"`. Call the reconcile function. Assert `unmark_window(window.id, "hop:editor:demo")` was issued.
2. **Reconciler clears browser mark on move-out**: same with `_hop_browser:demo`.
3. **Reconciler is a no-op when marks match workspace**: window with `hop:editor:demo` on `p:demo`. No unmark calls.
4. **Multiple sessions' marks are handled independently**: a single sway tree containing windows for sessions `demo` and `other`, some correctly placed, some drifted. Assert only the drifted ones get unmarked.
5. **Unrelated marks are left alone**: a window with a non-hop mark (e.g. `user-favorite`) on any workspace — never touched.
6. **`unmark_window` IPC**: `tests/test_sway.py` or `tests/test_sway_internals.py` — assert the issued command string is `[con_id=N] unmark "mark"`.
7. **Daemon startup runs the reconciler once** (`tests/test_daemon.py`): on `_run_main_loop` entry, the reconciler is invoked before the workspace-event loop begins.
8. **`hopd` window-event subscription receives events**: integration-light test using a fake transport that yields synthetic window events; assert the reconciler is called per event.

## Out of scope

- Role terminals (`hop:<role>` app_id). Their workspace filter already gives the right semantics; no marks involved.
- Auto-claiming a window that moves *onto* `p:<session>` (e.g. an arbitrary kitty launched with `--class hop:editor` arriving on the session ws). Explicitly excluded — only `hop edit` / `hop browser` set marks.
- Adding mark-based identity to role terminals. Bigger change, separate concern.
- Dropping the unmarked-app_id fallback in `editor.py:_find_editor_window`. Separate task (`drop-the-editor-unmarked-fallback-claim.md`). The reconciler is correct on its own; the fallback removal is an independent cleanup.
- Window-event filtering for performance. Reconciling on every window event is cheap (Sway IPC is fast, mark list is short) and correct.

## Task Type

implement

## Principles

- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [No defensive don'ts](../principles/no-defensive-donts.md)

## Blocked By

(none)

## Definition of Done

- `SwayIpcAdapter` gains `unmark_window(window_id, mark)` and `subscribe_to_window_events()` methods, both following the shape of their existing counterparts.
- `hopd` subscribes to `window` events in addition to `workspace` events (separate thread acceptable), and runs the mark-reconciler once at startup and on every window event.
- The reconciler clears `hop:editor:<session>` and `_hop_browser:<session>` marks from any window whose `workspace_name` is not `p:<session>`. The mark-prefix filter restricts it to those two namespaces.
- `hop_spec.md` documents the "marks reflect current placement" rule with cross-links from `hop edit` and `hop browser`.
- All test cases in the Test Plan pass.
- `make` is green.
