# Add Python and uv scaffold and session model

Create the Python and `uv` scaffold for `hop` and centralize project-root, session-name, and workspace-name resolution.

Add initial project files such as `pyproject.toml`, `hop/__main__.py`, `hop/cli.py`, `hop/session.py`, and `tests/`. Build shared helpers for deriving the project root, the session name from the project directory basename, and the Sway workspace name as `p:<session>`. Expose a command parser that can host `switch`, `list`, `edit`, `term`, `run`, and `browser` without mixing argument parsing with window-management logic.

This task implements the foundation described in [hop spec is canonical product context](../facts/hop-spec-is-canonical-product-context.md) and [hop session model and command contract](../facts/hop-session-model-and-command-contract.md), both derived from [hop_spec.md](../../hop_spec.md).

## Principles

- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)

## Task Type

implement

## Blocked By

(none)

## Definition of Done

- `uv` manages the project environment and the `hop` package metadata
- A runnable Python CLI entrypoint exists for the `hop` command
- Shared session helpers derive project root, session name, and workspace name consistently
- The CLI parser exposes the command surface described in [hop session model and command contract](../facts/hop-session-model-and-command-contract.md)
- The scaffold includes adapter boundaries for Sway, Kitty, Neovim, and browser integrations instead of embedding tool-specific calls directly in command handlers
- If the scaffold work changes the intended command contract or project structure assumptions in [hop_spec.md](../../hop_spec.md), the spec and derived facts are updated in the same change
- Unit tests cover root, session, and workspace derivation plus basic argument parsing
