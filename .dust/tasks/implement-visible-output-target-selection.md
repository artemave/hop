# Implement visible-output target selection

Implement visible-output target dispatch so users can open file and URL references directly from terminal output in the correct session-owned tool.

Add or finish the Kitty hints integration in `kittens/open_selection/main.py` and the related routing paths so visible selections from managed terminal windows can be resolved and dispatched to `hop edit` or `hop browser` behavior. Cover plain file paths, `path:line` references, git diff paths with `a/` or `b/` prefixes, URLs, and Rails-style references. Resolution should prefer absolute paths, then the terminal working directory, then the project root, while ignoring selections that still cannot be resolved after normalization.

This task implements the visible-output contract captured in [hop target dispatch and behavior guarantees](../facts/hop-target-dispatch-and-behavior-guarantees.md), derived from [hop_spec.md](../../hop_spec.md).

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

- Visible terminal selections can dispatch file targets into the shared session editor
- URL selections dispatch into the session-owned browser flow
- Git diff prefixes and `path:line` references are normalized before resolution
- Unresolvable selections are ignored without routing to the wrong target
- Tests cover the supported target formats and fallback resolution order
