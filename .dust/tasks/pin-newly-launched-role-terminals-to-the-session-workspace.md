# Pin newly-launched role terminals to the session workspace

Move freshly-launched kitty role terminals to `p:<session>` after they appear in Sway. Focus drift during `prepare` currently strands them on whatever workspace was focused at launch time.

## Background

When the user runs `hop` to create or enter a session, hop:

1. Switches Sway to the session workspace `p:<session>` (`hop/commands/session.py`).
2. Runs the backend's `prepare` (e.g. `podman-compose up`), which can take 5–60 seconds.
3. Spawns the session's kitty process (or `kitty @ launch`'s into an existing one) per role.

During step 2 the user often focuses out of `p:<session>` to do something else. When the role terminals finally appear (step 3) they land on the **currently focused** Sway workspace, not the session's. The user sees their test/server/console windows scattered across whatever workspace they happened to be on.

The editor (`hop/editor.py:_adopt_new_editor_window` lines 371–401) and browser (`hop/browser.py:_launch_session_browser` line 145) already handle this — both snapshot Sway window IDs before launch, poll for the new window post-launch, and call `sway.move_window_to_workspace(window.id, session.workspace_name)` if it drifted. The fix here is to apply the same pattern to role terminals.

## Codebase Context

- **Where role terminals launch**:
  - `hop/kitty.py:_launch_window` (line 327) — IPC-side path: `kitty @ launch` into the session's existing kitty process.
  - `hop/kitty.py:_bootstrap_session_kitty` (line 349) — first-launch path: `subprocess.Popen(["kitty", ...])` spawns a new kitty process whose first OS window is the role terminal.
- **Sway-side identity**: kitty role terminals are launched with `--class hop:<role>` (line 376 via `_os_window_name`). The `app_id` on Sway is `hop:<role>` — session-agnostic. No Sway marks are set on them (marks are reserved for editor and browser — see `hop/editor.py:17`, `hop/browser.py`).
- **Per-session disambiguation**: hop currently tracks role terminals only via kitty's per-session socket (`unix:@hop-<session_name>`) and the `hop_role` kitty user-var. There is **no Sway-side mark or property** that ties a `hop:<role>` window to a specific session. This means a snapshot-then-diff approach is the only way to identify "which `hop:shell` window did kitty just launch for *this* session".
- **Existing reference pattern** (already battle-tested for editor + browser):
  1. Before launch: `pre = {window.id for window in sway.list_windows() if window.app_id == target_app_id}`.
  2. Launch via kitty.
  3. Poll `sway.list_windows()` until a window with the target `app_id` and `id not in pre` appears (or timeout).
  4. If `window.workspace_name != session.workspace_name`: `sway.move_window_to_workspace(window.id, session.workspace_name)`.
- **Sway adapter capability**: `SwayIpcAdapter.move_window_to_workspace(window_id, workspace_name)` (line 216) — synchronous, already used by editor + browser.
- **Wiring**: `KittyRemoteControlAdapter` is constructed at `hop/app.py:446` and `:476`. Neither currently passes a `sway` dependency in — both have a `sway = SwayIpcAdapter()` already in scope just above the constructor call.

## Design

### KittyRemoteControlAdapter gains a Sway dep

Add `sway: SwayIpcAdapter | None = None` to `KittyRemoteControlAdapter.__init__` (mirroring how the editor adapter takes a Sway dep). Default `None` for backward compatibility with tests that construct the adapter without one; when `None`, the move step is a no-op. Both `build_default_services` and `build_kitten_services` pass the existing local `sway` instance.

### Snapshot-and-move helper

Add a private helper inside `hop/kitty.py`:

```python
def _adopt_role_terminal(
    self,
    session: ProjectSession,
    role: str,
    *,
    pre_snapshot_ids: set[int],
) -> None:
    if self._sway is None:
        return
    target_app_id = _os_window_name(role)
    deadline = self._clock() + SESSION_KITTY_READY_TIMEOUT_SECONDS
    while self._clock() < deadline:
        for window in self._sway.list_windows():
            if window.id not in pre_snapshot_ids and window.app_id == target_app_id:
                if window.workspace_name != session.workspace_name:
                    self._sway.move_window_to_workspace(window.id, session.workspace_name)
                return
        self._sleep(SESSION_KITTY_READY_POLL_INTERVAL_SECONDS)
```

Best-effort: if the timeout elapses with no matching new window (kitty silently failed, or the window appeared and was already closed), log via `debug.log` and return. Don't raise — the role terminal might still be functional via kitty IPC even if Sway didn't see it in time.

### Call sites

- `_launch_window` (line 327): Before the IPC launch, snapshot `pre_snapshot_ids = {w.id for w in self._sway.list_windows() if w.app_id == _os_window_name(role)}` if `self._sway` is set. After the launch succeeds, call `_adopt_role_terminal(session, role, pre_snapshot_ids=...)`.
- `_bootstrap_session_kitty` (line 349): Same pattern. The bootstrap's first kitty window is the role terminal too (the role passed in is the bootstrap role, typically `shell`). Snapshot before `self._launcher(...)`, adopt after `self._wait_for_session_kitty(addr)`.

### Race-condition notes

- The pre-snapshot includes any concurrent session's `hop:<role>` windows; their IDs are in `pre_snapshot_ids` so they aren't considered "new". Only this launch's new window passes the filter.
- Two concurrent `hop` bootstraps for *different* sessions both call `list_windows()` and both see each other's eventual windows as "new" *if* either's snapshot was taken before the other's launch completed. In practice hop's per-session `flock` serializes prepares, so two bootstraps don't overlap. Document this assumption; don't engineer for the concurrent case.
- After timeout with no match, log and return. Don't poll indefinitely.

### What this task does *not* do

- Doesn't touch editor or browser adoption paths (already correct).
- Doesn't add Sway marks to role terminals. Per-session role-terminal identity stays in kitty's per-session socket + `hop_role` user-var.
- Doesn't change the kitty launch protocol — the move happens via Sway IPC after kitty signals readiness.
- Doesn't address windows that the user explicitly moves to a different workspace *after* launch. Only the launch-time drift.

## Test Plan

Per `tests/test_kitty.py` patterns. `StubSwayAdapter` already records `move_window_to_workspace` calls (`tests/test_editor.py:66` shape) — extend `tests/test_kitty.py`'s existing kitty test helpers similarly, or add a fresh `StubSwayAdapter` per-test.

1. **Newly-launched role terminal on the wrong workspace gets moved.** Stub Sway reports a new `app_id="hop:shell"` window on `p:other` after launch; assert `move_window_to_workspace(new_id, "p:demo")` was called.
2. **Already on the right workspace → no move.** Stub Sway reports the new window's `workspace_name == session.workspace_name`; assert no `move_window_to_workspace` call.
3. **Pre-existing same-app_id window is not moved.** Pre-snapshot contains a window with `app_id="hop:shell"` on `p:other`. The launch creates no new sway window (e.g. kitty IPC was a no-op because window already existed). Assert no move.
4. **No Sway dep → no-op.** Adapter constructed without `sway`; `_launch_window` succeeds and never tries to call sway. Equivalent to today's behavior; ensures backward compat.
5. **Bootstrap path: first-kitty-launch window is moved.** `_bootstrap_session_kitty` test asserts the new window discovered post-spawn is moved to `p:<session>`.
6. **Timeout with no matching window → debug-log + return, no raise.** Stub Sway never reports a matching new window; assert the launch returns normally and `debug.log` was called.

## Task Type

implement

## Principles

- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)

## Blocked By

(none)

## Definition of Done

- `hop/kitty.py:_launch_window` and `_bootstrap_session_kitty` both run the snapshot-and-move dance for the newly-appeared `hop:<role>` window when a Sway adapter is wired in.
- `KittyRemoteControlAdapter.__init__` accepts a `sway: SwayIpcAdapter | None` parameter; `build_default_services` and `build_kitten_services` pass the existing `sway` instance.
- All test cases above pass; existing kitty tests still pass without a Sway dep (backward compat).
- `make` is green.
- No regressions in editor or browser launch behavior (they already self-correct and shouldn't be touched).
