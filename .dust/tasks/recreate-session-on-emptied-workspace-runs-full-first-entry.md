# Recreate session on emptied workspace runs full first entry

Run a cold first entry when bare `hop` fires from `p:<session>` with the session's kitty dead. Today this hits the "spawn extra shell" branch and bootstraps one host-backend terminal, skipping prepare, backend selection, the editor, and any other configured windows.

## Background

After `hop kill`, sway's normal behavior is to leave the user on the (now-empty) session workspace — sway only switches workspaces in response to explicit user / IPC action, not because the last window died. `hop kill` itself does not switch away from `p:<session>` either; it just closes the windows, runs `backend.teardown`, and calls `forget_session` to drop the persisted state file.

If the user then runs `hop` again from the project root to recreate the session, `execute_command` takes the wrong branch:

`hop/app.py:324-334`

```python
case EnterSessionCommand(backend=backend_name):
    session = resolve_project_session(current_directory)
    if services.sway.get_focused_workspace() == session.workspace_name:
        # Spawning an additional terminal in an already-live session:
        # ...
        spawn_session_terminal(
            current_directory,
            terminals=services.kitty,
        )
    else:
        # First entry creates both shell and editor; ...
        kitty_alive = services.kitty.is_alive(session)
        is_first_entry = not kitty_alive
        ...
        backend = services.session_backends.resolve_for_entry(
            session,
            backend_name=backend_name,
            kitty_alive=kitty_alive,
            skip_prepare=headless_first_entry,
        )
        ...
```

The if-branch is gated only on the focused workspace name matching `p:<session>`. It does not check `kitty.is_alive(session)`. So after a kill that left us on the workspace, we go into `spawn_session_terminal`, which has its own dead-kitty fallback (`hop/commands/session.py:163-169`):

```python
if not terminals.is_alive(session):
    # The session's kitty has died but the workspace is still alive
    # (e.g. only a browser window remains). Bootstrap a fresh kitty +
    # canonical shell via the terminal adapter ...
    terminals.ensure_terminal(session, role=SHELL_TERMINAL_ROLE)
    return session
```

That branch was designed for "kitty died but a non-kitty window kept the workspace alive" (the comment names the browser case). It calls `ensure_terminal` directly, with no backend resolution: `SessionBackendRegistry.for_session` then has no persisted record (forgotten by `hop kill`) and no override (only `resolve_for_entry` sets one), so it returns the built-in host backend. The terminal launches `${SHELL:-sh}` on the host instead of running through the project's configured backend (compose / devcontainer / ssh). Prepare never runs. The editor and any other configured windows never come up.

The user-visible symptom: kill a containerized session while standing on its workspace, run `hop`, get a single host shell with no container and no editor.

## Design

Move the "kitty alive?" check to where it controls the branch decision, not where it picks a fallback. Bare `hop` from `p:<session>` should:

- spawn an additional shell *only when the session is actually live* — kitty reachable.
- otherwise run the full first-entry bootstrap, identical to bare `hop` from any other workspace.

Concretely, in `hop/app.py` (the `EnterSessionCommand` arm):

```python
session = resolve_project_session(current_directory)
on_session_workspace = services.sway.get_focused_workspace() == session.workspace_name
kitty_alive = services.kitty.is_alive(session)

if on_session_workspace and kitty_alive:
    spawn_session_terminal(
        current_directory,
        terminals=services.kitty,
    )
else:
    is_first_entry = not kitty_alive
    headless_first_entry = is_first_entry and not services.popup.is_interactive()
    backend = services.session_backends.resolve_for_entry(...)
    # ... unchanged ...
```

This collapses three cases into the right branches:

| focused workspace == p:session | kitty alive | branch                                                     |
| ------------------------------ | ----------- | ---------------------------------------------------------- |
| yes                            | yes         | `spawn_session_terminal` (today: ✓, after fix: ✓)          |
| yes                            | no          | full first-entry bootstrap (today: ✗ bug, after fix: ✓)    |
| no                             | yes         | re-entry, ensure shell only (today: ✓, after fix: ✓)       |
| no                             | no          | full first-entry bootstrap (today: ✓, after fix: ✓)        |

The dead-kitty fallback in `spawn_session_terminal` (`hop/commands/session.py:163-169`) becomes unreachable through this entry path. The comment names the "non-kitty window kept the workspace alive" case — most plausibly a session browser that drifted onto `p:<session>` and was not closed by `hop kill` (it is, because `kill_session` includes the marked browser regardless of workspace). Audit whether anything else can leave the workspace populated with a non-kitty window after a kill; if not, delete the dead-kitty fallback inside `spawn_session_terminal` too, so the function has one job (spawn the next ad-hoc shell into an *already-live* kitty) and the bootstrap path is the single owner of cold-bootstrap behavior. The existing `belongs_to_session` predicate in `kill_session` is workspace + browser-mark + editor-mark — anything else on `p:<session>` (a manually-opened terminal of an unrelated app) is *not* closed by `hop kill`, so the "workspace stayed alive because of a non-hop window" case is real and the fallback should stay; clarify the comment to that effect.

### Files to change

- `hop/app.py:324-387` — restructure the EnterSessionCommand arm as above. Probe `kitty_alive` once before the if; reuse the result in the else branch instead of probing again.
- `hop/commands/session.py:158-169` — update the comment that says "kitty has died but the workspace is still alive (e.g. only a browser window remains)" to reflect that hop's own browser is closed by `hop kill` (via the mark sweep), so the surviving case is a non-hop window on the workspace. This branch is no longer reached through the `hop` entry path; the function comment should make clear it exists for that edge.

### Tests

In `tests/test_app.py`, add:

- `test_execute_command_recreates_killed_session_when_on_emptied_workspace` — focused workspace is `p:demo`, kitty is *not* alive, no persisted state. Assert it runs the first-entry path: editor ensured, shell ensured, prepare called (via the configured backend), workspace not switched (we are already on it).
- Keep `test_execute_command_spawns_extra_shell_when_focused_on_session_workspace` — its `alive_session_names=("demo",)` setup represents the "yes/yes" branch and is unchanged.

A regression test in `tests/test_window_reuse.py` or a new integration-shaped test that exercises `hop kill` followed immediately by bare `hop` on the same workspace would also be valuable; the existing `IdempotentKittyAdapter` already has the right shape (`close_window` simulates the kill, `is_alive`-style check can be added). Out of scope if the unit-level coverage in `test_app.py` is sufficient.

## Task Type

implement

## Principles

- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)

## Blocked By

(none)

## Definition of Done

- Bare `hop` from `p:<session>` after `hop kill` runs the full first-entry bootstrap: backend resolved through `resolve_for_entry` (not the host fallback), `prepare` invoked, editor + shell + any configured windows launched, workspace layout applied.
- Bare `hop` from `p:<session>` while the session's kitty is alive continues to spawn the next ad-hoc shell (`shell-2`, `shell-3`, ...) without ensuring the editor or re-running prepare.
- A new test in `tests/test_app.py` covers the "workspace focused, kitty dead" branch; the existing alive-kitty test for the same workspace stays green.
- The comment on `spawn_session_terminal`'s dead-kitty fallback explains the remaining edge (non-hop window kept the workspace alive) accurately.
- `make` is green.
