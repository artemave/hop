# Add end-to-end coverage for window reuse

Add automated coverage for idempotent window reuse and missing-component recreation across the `hop` command set.

Build a test harness around fake or shimmed Sway, Kitty, browser, and Neovim adapters in `tests/fixtures/` and `tests/`. Assert repeated commands reuse existing windows, `hop edit` recreates a closed editor, `hop run` provisions missing roles, `hop browser` stays session-scoped, and session switching never creates duplicate OS windows for the same session and role.

This task verifies the guarantees summarized in [hop target dispatch and behavior guarantees](../facts/hop-target-dispatch-and-behavior-guarantees.md) across the command surface defined in [hop session model and command contract](../facts/hop-session-model-and-command-contract.md), both derived from [hop_spec.md](../../hop_spec.md).

## Principles

- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [One shared editor per session](../principles/one-shared-editor-per-session.md)
- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)

## Task Type

implement

## Blocked By

- [Implement the session browser command](implement-the-session-browser-command.md)
- [Implement visible-output target selection](implement-visible-output-target-selection.md)

## Definition of Done

- Automated tests cover repeated command calls without duplicate windows
- Tests verify missing components are recreated automatically after manual teardown
- Session and role routing behavior is asserted for terminals, editor, and browser flows
- The test harness is easy to extend as new CLI commands or window roles are added
- Coverage reflects the current behavior described in [hop_spec.md](../../hop_spec.md) and helps catch future spec drift
