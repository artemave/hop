# Implement hop kill

Add a `kill` subcommand to `hop` that tears down the session resolved from the caller's current working directory by closing all managed windows and removing the session workspace.

## What to build

- Add `kill` to the CLI parser in `hop/cli.py` and the command union in `hop/app.py`
- Add `hop/commands/kill.py` that:
  - resolves the session from the caller's exact cwd (same as `hop`, `hop edit`, `hop term`, `hop run`, `hop browser`)
  - discovers every window hop owns for that session: Kitty role terminals, the shared editor window, and the marked session browser (even if it has drifted to another workspace)
  - closes all discovered windows
  - removes the session workspace if it still exists after window teardown
- Add close-window and workspace-teardown primitives to `hop/sway.py` and/or `hop/kitty.py` as needed
- Add command-level tests in `tests/test_kill_command.py` and adapter-level tests for any new sway/kitty primitives, co-located with the existing test files

## Constraints

- Target is current working directory only — no `hop kill <session>` argument form
- Kill boundary is managed windows only: close windows hop can positively identify (Kitty role terminals, shared editor, marked browser); do not close unrelated windows on the workspace
- Let Sway decide focus after teardown — do not switch focus away before closing windows
- Follow existing patterns: resolve session via `ProjectSession`, discover windows via the same metadata used by `hop term`, `hop edit`, and `hop browser`

## Principles

- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [One shared editor per session](../principles/one-shared-editor-per-session.md)
- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)

## Task Type

implement

## Blocked By

(none)

## Definition of Done

- `hop kill` run from a session directory closes all managed hop windows for that session
- The session workspace is removed after teardown
- No unrelated windows on the workspace are closed
- A browser that drifted to another workspace is discovered and closed via its Sway mark
- Command-level and adapter-level tests cover the new behavior
- `hop_spec.md` documents the `hop kill` command
