# Make session attachment directory-aware

Make session reuse and attachment depend on the exact session-root directory so same-basename directories do not alias one another.

Extend the session identity model in `hop/session.py`, `hop/sway.py`, `hop/kitty.py`, `hop/editor.py`, and the related command paths so existing workspaces, Kitty windows, and the shared Neovim instance are matched using stable directory identity in addition to any human-readable session name. Revisit workspace naming and `hop switch <session>` semantics if the current `p:<session_name>` rule cannot uniquely represent directory-rooted sessions, and add coverage for duplicate basenames in `tests/test_session.py`, `tests/test_session_commands.py`, `tests/test_kitty.py`, `tests/test_editor.py`, and `tests/test_app.py`.

This task follows from the directory-rooted session contract in [hop_spec.md](../../hop_spec.md) and preserves the guarantees in [hop session model and command contract](../facts/hop-session-model-and-command-contract.md).

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

- Running `hop` from two different directories with the same basename can create or attach to distinct sessions
- Terminal and editor reuse only occurs when the directory-rooted session identity matches
- `hop switch` behavior remains explicitly specified and covered by tests after the identity change
- The spec, facts, and tests document how directory-rooted identity is represented and matched
