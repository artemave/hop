# Implement the session browser command

Implement `hop browser [url]` so each session can reuse or create a browser window inside its workspace.

Add browser launch and focus logic in `hop/browser.py` and `hop/commands/browser.py`. Reuse the decision captured in [Define browser session isolation](../ideas/define-browser-session-isolation.md) to choose whether `hop` relies on app IDs, a dedicated profile, or another discovery mechanism.

This task implements the browser portion of [hop session model and command contract](../facts/hop-session-model-and-command-contract.md) and the reuse guarantees in [hop target dispatch and behavior guarantees](../facts/hop-target-dispatch-and-behavior-guarantees.md), both derived from [hop_spec.md](../../hop_spec.md).

## Principles

- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)

## Task Type

implement

## Blocked By

(none)

## Definition of Done

- `hop browser` focuses an existing session browser window or creates one when missing
- `hop browser <url>` opens the URL in the session browser without stealing unrelated windows
- Browser discovery and creation remain idempotent across repeated commands
- The browser stays associated with the current session workspace according to the chosen design
- Any user-visible change to browser ownership or `hop browser` semantics updates [hop_spec.md](../../hop_spec.md) and the derived facts in the same change
