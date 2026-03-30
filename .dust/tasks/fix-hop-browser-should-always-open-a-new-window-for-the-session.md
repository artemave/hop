# Fix: hop browser should always open a new window for the session

Fix `hop browser` to always open a new window in the session workspace instead of a tab.

## Task Type

implement

## Principles

- [Session-Oriented Workspaces](../principles/session-oriented-workspaces.md)

## Blocked By

(none)

## Definition of Done

- `hop browser` passes `--new-window` for all browsers, not just known families
- New session browser window is detected by tracking all Sway window IDs before launch
- `hop kill` closes the browser before closing Kitty terminals
- All tests pass
