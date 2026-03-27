# Implement role-based terminals and run routing

Implement `hop term` and `hop run` so named Kitty windows are reused or created per session role.

Add Kitty integration in `hop/kitty.py` and command handlers in `hop/commands/term.py` and `hop/commands/run.py`. The Kitty adapter should prefer Kitty-native APIs, Python bindings, and kittens rather than driving `kitty @` through subprocesses. Encode session and role metadata in a stable way so `shell`, `test`, `server`, and other roles can be focused, created, and sent commands idempotently. Default `hop run` to the `shell` role when none is supplied.

This task implements the terminal-role behavior described in [hop session model and command contract](../facts/hop-session-model-and-command-contract.md) and the reuse guarantees described in [hop target dispatch and behavior guarantees](../facts/hop-target-dispatch-and-behavior-guarantees.md), both derived from [hop_spec.md](../../hop_spec.md).

## Principles

- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)

## Task Type

implement

## Blocked By

(none)

## Definition of Done

- `hop term --role <name>` focuses an existing terminal for the session role or creates it when missing
- `hop run --role <name> "<command>"` sends commands to the correct Kitty window and creates it when needed
- Kitty window lookup, focus, and command injection live behind a code-level adapter rather than direct CLI shell-outs
- Session and role metadata are stable enough to prevent duplicate terminal windows during repeated calls
- `hop run "<command>"` uses the `shell` role by default
- Any user-visible change to role behavior or `hop term` / `hop run` semantics updates [hop_spec.md](../../hop_spec.md) and the derived facts in the same change
