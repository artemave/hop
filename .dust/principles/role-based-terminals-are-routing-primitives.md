# Role-based terminals are routing primitives

Use terminal roles as the stable routing primitive for session command execution.

Each session may have multiple Kitty windows, but each window should be discoverable and reusable through a role such as `shell`, `test`, `server`, or `console`. `hop term` and `hop run` should focus or create terminals by role, and integrations such as `vigun` should target roles rather than individual window IDs or ad hoc titles.

## Parent Principle

- [Session-oriented workspaces](session-oriented-workspaces.md)

## Sub-Principles

- (none)
