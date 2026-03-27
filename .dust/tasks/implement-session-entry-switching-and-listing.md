# Implement session entry, switching, and listing

Implement `hop`, `hop switch`, and `hop list` so sessions map cleanly to `p:<name>` Sway workspaces.

Add Sway integration code in `hop/sway.py` and session orchestration in command handlers such as `hop/commands/session.py`. The Sway adapter should use IPC or another programmatic interface rather than a subprocess wrapper around `swaymsg`. `hop` from inside a project should detect the project root, focus or create the matching workspace, and ensure a `shell` terminal exists before returning control.

This task implements the session entry and switching behavior described in [hop session model and command contract](../facts/hop-session-model-and-command-contract.md), which summarizes the relevant sections of [hop_spec.md](../../hop_spec.md).

## Principles

- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)

## Task Type

implement

## Blocked By

- [Add Python and uv scaffold and session model](add-python-and-uv-scaffold-and-session-model.md)

## Definition of Done

- `hop` from inside a project resolves the session and enters the matching workspace
- `hop switch <session>` focuses an existing session or creates it when appropriate
- `hop list` shows known sessions in a format that can drive fast switching
- Sway workspace discovery and control live behind a programmatic adapter rather than direct CLI shell-outs
- Entering a session guarantees that a `shell` terminal exists without creating duplicates
- Any user-visible change to session naming, workspace behavior, or session entry semantics updates [hop_spec.md](../../hop_spec.md) and the derived facts in the same change
