# Add --focus flag to hop run

Add a `--focus` flag to `hop run` that focuses the role terminal after dispatching the command, opting out of the default keep-current-focus behavior.

## Background

`hop run --role <role> "<cmd>"` finds (or creates) the session's role terminal, types the command into it, and returns. By default — and as documented in `hop_spec.md` (Send command to terminal, behavior bullet "default behavior keeps the current focus while routing the command into the target role terminal") — it does not move focus, so callers like `vigun` can dispatch into the test runner without yanking the user out of their editor.

A natural counterpart use case is the operator typing `hop run --role server "bin/dev"` from anywhere in the project tree and wanting to immediately watch the server terminal: today they have to chase it with a separate `hop term --role server` (and possibly `hop switch <session>` if they're on a different Sway workspace). `--focus` is the explicit opt-in that combines those steps.

The kitty adapter already has the seam for the kitty-side of focus: `KittyRemoteControlAdapter._launch_window(..., keep_focus: bool)` is plumbed through, and `ensure_terminal` (the path `hop term` uses) demonstrates the focus-existing-window pattern via `kitty @ focus-window`. The Sway-side seam already exists too: `SwayAdapter.switch_to_workspace(workspace_name)` is what `SwitchSessionCommand` uses today. This task wires both seams into the run path.

`--focus` deliberately departs from the existing default "`hop run` does not switch Sway workspaces" rule (`hop_spec.md`, the note shared with `hop term` / `hop edit` / `hop browser`). The default keep-focus behavior — the contract `vigun` and other automated callers depend on — stays unchanged. `--focus` is the explicit opt-in that says "take me to the role terminal," which only makes sense if it works from a different Sway workspace; otherwise it's a no-op for the most common interactive use case.

## Design

### CLI surface

```bash
hop run --focus "<command>"
hop run --role server --focus "bin/dev"
```

- `--focus` is a boolean flag on the `run` subparser (`action="store_true"`, default `False`).
- It composes with `--role`; ordering is irrelevant.
- Default (no flag) keeps the current behavior: command is sent into the role terminal, focus is preserved.

### Command flow

1. `hop/cli.py::build_parser` declares `run_parser.add_argument("--focus", action="store_true", dest="focus")`.
2. `hop/cli.py::parse_command` populates `RunCommand.focus` from `namespace.focus`.
3. `hop/commands/__init__.py::RunCommand` grows `focus: bool = False`.
4. `hop/app.py::execute_command`'s `RunCommand` arm passes `focus=command.focus` into `run_command`. After `run_command` returns, when `command.focus` is true, calls `services.sway.switch_to_workspace(dispatch.session.workspace_name)` so the user lands on `p:<session>` regardless of where they invoked `hop run`. The Sway switch is idempotent when the user is already on the target workspace.
5. `hop/commands/run.py::run_command` accepts `focus: bool = False` and forwards it to `terminals.run_in_terminal(..., focus=focus)`. It does not take the Sway adapter — the workspace switch is the caller's job (so the run-dispatch unit stays focused on its kitty contract).
6. `hop/commands/run.py::RunKittyAdapter` Protocol grows `focus: bool` as a keyword-only parameter on `run_in_terminal`.

### Kitty adapter behavior (`hop/kitty.py::run_in_terminal`)

`run_in_terminal` accepts a new keyword-only `focus: bool = False` parameter:

- **Existing role window + `focus=False`** (current default): no change — `send-text` only.
- **Existing role window + `focus=True`**: send `focus-window` for that window after `send-text` (mirroring `ensure_terminal`'s focus-existing path).
- **Missing role window + `focus=False`** (current default): `_launch_window(..., keep_focus=True)` — kitty creates the window without taking focus — then `send-text`.
- **Missing role window + `focus=True`**: `_launch_window(..., keep_focus=False)` — kitty creates the window and focuses it — then `send-text`. No extra `focus-window` call needed because launch already focused it.

The bootstrap path (`_bootstrap_session_kitty`, used when the session's kitty isn't listening yet) is the same in both modes: a fresh kitty process always opens its first window focused as the new OS window, which matches `--focus` semantics. No separate handling is required for the bootstrap branch.

### What `--focus` does *not* do

- It does not change the no-flag default. Existing `vigun`-style callers keep their focus-preserving / no-workspace-switch contract.
- It does not affect `hop tail`'s ability to find the run id; the persisted run state and run id semantics are unchanged.
- It does not run a `hop switch` against an arbitrary session name — `--focus` operates on the session resolved from the current working directory (same as the rest of `hop run`). If the caller is outside any project tree, `resolve_project_session` will refuse the call before the focus path runs.

### Order of operations

When `focus=True`:

1. `run_command` resolves the session, calls `kitty.run_in_terminal(..., focus=True)` — kitty creates the role window with `keep_focus=False` (or sends `focus-window` to the existing window), then `send-text` types the command. The kitty pane is now the focused pane inside the session's kitty.
2. After `run_command` returns the dispatch, `app.execute_command` calls `sway.switch_to_workspace(dispatch.session.workspace_name)` — Sway lands the user on `p:<session>`.
3. `dispatch.run_id` is printed to stdout last, so the caller's stdout pipeline is unchanged.

Reverse ordering (Sway switch first, then kitty dispatch) would also work; the chosen order keeps `dispatch.session` available for the workspace switch without requiring `run_command` to take the Sway adapter.

## Files to change

- `hop/cli.py` — declare `--focus` on the run subparser; pass it into `RunCommand`.
- `hop/commands/__init__.py` — `RunCommand.focus: bool = False`.
- `hop/commands/run.py` — `run_command` accepts `focus`; `RunKittyAdapter` Protocol grows the keyword-only `focus` parameter on `run_in_terminal`.
- `hop/app.py` — `RunCommand` match arm forwards `focus` to `run_command` and, when `focus=True`, calls `services.sway.switch_to_workspace(dispatch.session.workspace_name)` after dispatch. `KittyAdapter` Protocol's `run_in_terminal` signature gains the keyword-only `focus: bool` parameter.
- `hop/kitty.py` — `KittyRemoteControlAdapter.run_in_terminal` accepts `focus`; routes to `_launch_window(..., keep_focus=not focus)` for missing windows and sends `focus-window` after `send-text` for existing windows when `focus=True`.
- `hop_spec.md` — extend the `hop run` Behavior list with the `--focus` opt-in: "by default keeps the current focus; `--focus` focuses the target role terminal and switches to its session's Sway workspace before returning the run id." Update the shared "do not switch Sway workspaces" note to call out `hop run --focus` as the explicit exception.
- `README.md` — add a `hop run --focus` example to the `hop run` section.

## Tests

Real behavior, no mocks (per project convention).

- `tests/test_cli.py`:
  - `parse_command(["run", "ls"])` → `RunCommand(command_text="ls", role="shell", focus=False)`.
  - `parse_command(["run", "--focus", "ls"])` → `RunCommand(command_text="ls", role="shell", focus=True)`.
  - `parse_command(["run", "--role", "server", "--focus", "bin/dev"])` → `RunCommand(role="server", command_text="bin/dev", focus=True)`.
  - `parse_command(["run", "--focus", "--role", "server", "bin/dev"])` (flag order swapped) → same as above.

- `tests/test_run_commands.py`:
  - Extend `StubKittyAdapter.run_in_terminal` to record the `focus` argument (and accept the keyword-only param). Assert `run_command(..., focus=False)` forwards `focus=False` and `run_command(..., focus=True)` forwards `focus=True`.
  - `focus` defaults to `False` when omitted (so existing call sites keep working).
  - The persisted run-state JSON shape is unchanged by `focus` (focus is dispatch-time-only).

- `tests/test_kitty.py` — add cases mirroring the existing run / ensure tests:
  - **Existing role window + `focus=False`**: factory call sequence is `ls` → `send-text` only (no `focus-window`).
  - **Existing role window + `focus=True`**: factory call sequence is `ls` → `send-text` → `focus-window` with the matching `id:<n>`.
  - **Missing role window + `focus=False`** (current behavior): launch payload still has `keep_focus=True`.
  - **Missing role window + `focus=True`**: launch payload has `keep_focus=False`; the post-launch `send-text` still goes to the just-created window.

- `tests/test_app.py`:
  - Extend `StubKittyAdapter.run_in_terminal` to record `focus`.
  - `execute_command(RunCommand(role="server", command_text="bin/dev"), ...)` records `focus=False` on the kitty stub and leaves `services.sway.switched_workspaces == []` (default keeps the no-workspace-switch behavior).
  - `execute_command(RunCommand(role="server", command_text="bin/dev", focus=True), ...)` records `focus=True` on the kitty stub and produces `services.sway.switched_workspaces == ["p:<session>"]` for the session resolved from the cwd.
  - The Sway workspace switch happens after `run_in_terminal`, so the test asserts the switch is recorded after the kitty call (use call ordering on the stubs — `StubSwayAdapter.switched_workspaces` is appended to in the order calls happen).
  - `RunCommand(focus=True)` invoked from a directory that already lives on `p:<session>` still appends to `switched_workspaces` (idempotent switch — the contract doesn't depend on the caller's current workspace).

## Out of scope

- Adding `--focus` to `hop tail`, `hop browser`, or `hop edit`. Each has its own focus contract today (`hop browser` and `hop edit` already focus by design; `hop tail` is a passive reader). This task is scoped to `hop run`.
- Changing `hop term`'s default contract. `hop term` already focuses-but-doesn't-switch-workspaces today; aligning it with the new `hop run --focus` semantics (workspace switch on focus) is a separate decision worth its own task.
- Changing `vigun`'s integration. `vigun` calls `hop run` without `--focus`, which is the keep-focus / no-workspace-switch default — no caller-side change needed.
- Resolving a session by name (`hop run --focus --session <name>`). `hop run` always uses the cwd-derived session; if a workflow needs cross-session dispatch, that's a separate session-targeting feature, not part of this task.

## Task Type

implement

## Principles

- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `hop run --focus` is accepted by the CLI and produces `RunCommand(focus=True)`; default remains `focus=False`. The flag composes with `--role` in either order.
- `RunCommand.focus` is plumbed end-to-end: `cli.parse_command` → `app.execute_command` → `commands.run.run_command` → `KittyRemoteControlAdapter.run_in_terminal`.
- `KittyRemoteControlAdapter.run_in_terminal(..., focus=True)` focuses the role terminal: existing windows get a `focus-window` after `send-text`; missing windows are launched with `keep_focus=False` so kitty focuses the new OS window before `send-text` runs.
- `KittyRemoteControlAdapter.run_in_terminal(..., focus=False)` is byte-for-byte the current behavior — no extra IPC calls, `keep_focus=True` on launch.
- `app.execute_command` switches Sway to `p:<session>` (via `services.sway.switch_to_workspace`) when `RunCommand.focus=True`, after the kitty dispatch returns. The default path still does not touch Sway.
- `RunKittyAdapter` and `KittyAdapter` Protocols expose `focus: bool` as a keyword-only parameter on `run_in_terminal` with a default of `False`, so existing callers keep compiling without changes.
- `hop_spec.md` documents `--focus` under the `hop run` Behavior list and reaffirms that `--focus` does not switch Sway workspaces.
- `README.md`'s `hop run` section shows a `--focus` example.
- New unit tests cover the cases listed in the Tests section, follow the no-mock convention, and pass under `uv run pytest -q`.
- `bunx dust lint` passes for the task file.
