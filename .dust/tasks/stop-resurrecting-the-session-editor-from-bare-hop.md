# Stop resurrecting the session editor from bare hop

Stop resurrecting the session editor when bare `hop` is invoked inside an existing session — vicinae's `Hop editor` entry covers explicit re-creation.

## Background

`hop` invoked from inside an already-focused session workspace lands in `spawn_session_terminal` (`hop/commands/session.py:152`). The current implementation has a side branch: if the session's editor has been closed (`editor.ensure()` reports it had to launch a fresh window), `spawn_session_terminal` returns early and does *not* open the otherwise-expected `shell-<N>` terminal. This was introduced as a "one keystroke recovers the editor" affordance — the user closes nvim with `:qa`, hits `hop` from a sibling shell, and the editor comes back instead of a redundant new shell.

Two things have changed since:

1. `hop/vicinae.py::_window_script` emits a per-window `Hop editor` entry for the focused session whenever the editor role is declared (`hop/vicinae.py:174-175`: `body = "exec setsid -f hop edit\n"`). The vicinae launcher is now the explicit, discoverable way to bring the editor back. Re-creation no longer needs an implicit gesture hidden inside `hop`.
2. `hop_spec.md`'s "Enter session" section already documents the spawn-additional-terminal mode as "create a new Kitty role terminal with that role" (lines 190-196) with no mention of an editor-resurrect branch. The code is out of sync with the spec; removing the branch brings them back into alignment.

After this change, bare `hop` from inside a live session is predictably a `shell-<N>` spawn. Recovering a closed editor is the user's explicit choice: pick `Hop editor` in vicinae (or type `hop edit`).

## Scope of change

### `hop/commands/session.py`

- `spawn_session_terminal`: drop the `if editor.ensure(session): return session` branch (line 173). The function no longer takes the `editor` parameter; remove `SessionEditorAdapter` from its signature and from the `SpawnTerminalAdapter` Protocol's neighborhood (the Protocol itself stays — it's still used by `enter_project_session`).
- The dead-kitty bootstrap branch (line 164-172) keeps its `terminals.ensure_terminal(session, role=SHELL_TERMINAL_ROLE)` call so a workspace that outlived its kitty still gets a fresh shell. It no longer calls `editor.ensure` afterwards — same rule: editor re-creation is the user's explicit choice via vicinae or `hop edit`.
- Update the function's docstring/comments to reflect "always spawns a numbered shell" and to remove the now-stale references to editor resurrection.

### `hop/app.py`

- `execute_command`'s `EnterSessionCommand` arm calls `spawn_session_terminal(current_directory, terminals=services.kitty, editor=services.neovim)` (line 325-329). Drop the `editor=` kwarg. Update the surrounding comment (`# An editor is ensured alongside so a closed editor comes back on the next \`hop\`.`) to state that re-entry / additional-terminal spawns never resurrect the editor.

### `hop/editor.py`

- `SharedNeovimEditorAdapter.ensure(...)` returns a `bool` (`was_launched`) that no caller will read anymore (`enter_project_session` ignores it; `focus()` and `open_target()` use the tuple-returning `_ensure_editor` directly). Drop the return: `ensure(...) -> None`. Simplify the body: call `_ensure_editor` and discard both elements of the tuple. Update the docstring (lines 257-273) to remove the "callers (`spawn_session_terminal`) use this to decide whether ..." passage; keep the `keep_focus` rationale (still load-bearing for `enter_project_session`'s tab-ordering).

### Tests (`tests/test_session_commands.py`)

- Delete `test_spawn_session_terminal_resurrects_a_closed_editor_without_extra_shell` (line 591-606) — the resurrect behavior it asserts is the thing being removed.
- Update `test_spawn_session_terminal_spawns_shell_when_editor_already_open` (line 609-621) so the editor stub is no longer constructed, and the assertion drops `editor.ensured == ["demo"]`. After the change there is no editor adapter to pass in.
- Update `test_spawn_session_terminal_bootstraps_shell_when_kitty_socket_is_dead` (line 624-643): rename to reflect the new behavior (e.g. `..._spawns_canonical_shell_when_kitty_socket_is_dead`); drop the editor adapter; assert `terminals.ensured_terminals == [("demo", "shell", project_root)]` (no second editor.ensure call, no extra ad-hoc shell — the dead-kitty branch bootstraps `shell` and stops).
- Update `test_spawn_session_terminal_picks_first_unused_shell_role`, `test_spawn_session_terminal_skips_used_numbered_shells`, and `test_spawn_session_terminal_does_not_switch_workspace` (lines 549-588): drop the unused `editor = StubEditorAdapter()` construction and the `editor=editor` kwarg.

### Tests (`tests/test_editor.py`)

- `test_ensure_returns_false_when_editor_already_running` (line 164-179) and `test_ensure_returns_true_when_editor_was_just_launched` (line 182-204) exist solely to lock in the `was_launched` return value. After the signature change to `ensure(...) -> None`, refactor them into "ensure() is a no-op when an editor already exists" (no kitty IPC, no focus shift) and "ensure() launches an editor when none exists" (kitty launch payload, sway window adopted) — same coverage, no `was_launched` assertions.

### Tests (`tests/test_app.py`)

- The `EnterSessionCommand` arm tests that exercise the spawn-additional-terminal branch (the ones that fire `spawn_session_terminal` because `services.sway.get_focused_workspace()` already matches `session.workspace_name`) currently pass `services.neovim` through. With `editor=` dropped from the call site, those tests' fakes no longer record an editor.ensure for the re-entry path. Audit the test cases around `test_app.py:365` ("spawn_session_terminal does not fire ...") and the EnterSessionCommand re-entry tests to ensure their assertions still hold — drop any `services.neovim.ensured == [...]` expectations for the spawn-additional-terminal branch.

### `hop_spec.md`

- The "Enter session" section (lines 190-196) already describes the spawn-additional-terminal mode without the editor-resurrect branch. No spec edits required — this task aligns code with the existing spec.
- If the "Open editor" section (line 263+) references the implicit resurrect from bare `hop`, drop that mention. Otherwise leave it alone.

### `README.md`

- Skim for any line that says "bare `hop` brings back the editor" or similar; remove if found.

## Out of scope

- Removing `SharedNeovimEditorAdapter.ensure()` itself. `enter_project_session`'s first-entry sweep still uses it (`hop/commands/session.py:102` and `:110`), as does the test surface for first entry.
- Changing `hop edit` (`SharedNeovimEditorAdapter.focus()`). It still ensures-then-focuses, per the "One shared editor per session" principle.
- Changing how vicinae emits the `Hop editor` entry. It already runs `exec setsid -f hop edit`, which is the canonical user-facing path after this change.
- Changing the dead-kitty branch's choice to bootstrap the canonical `shell` role (rather than a `shell-<N>` ad-hoc). That's the existing behavior and stays as-is.

## Task Type

implement

## Principles

- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)
- [One shared editor per session](../principles/one-shared-editor-per-session.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `spawn_session_terminal` no longer takes an `editor` parameter and never calls `editor.ensure`. Invoked from inside a live session, it always produces a `shell-<N>` terminal (or the canonical `shell` if kitty itself was dead).
- `hop/app.py`'s `EnterSessionCommand` arm calls `spawn_session_terminal(current_directory, terminals=services.kitty)` — no `editor=` kwarg, no implicit editor resurrection on the re-entry path.
- `SharedNeovimEditorAdapter.ensure` returns `None` (no `was_launched` bool). All call sites updated.
- The "resurrects a closed editor" test is removed; the "spawns shell when editor already open" and "bootstraps shell when kitty socket is dead" tests are updated to match the new behavior (no editor.ensure recorded for either case).
- `tests/test_editor.py`'s `was_launched`-flavored tests are refactored to assert behavior rather than the return value.
- The vicinae `Hop editor` entry continues to bring the editor back via `hop edit` after this change (manual verification or documented assertion).
- `make` passes (test, typecheck, lint, format-check, 100% coverage).
- `bunx dust lint` passes for this task file.
