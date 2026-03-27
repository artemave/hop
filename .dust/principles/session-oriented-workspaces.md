# Session-oriented workspaces

Model each project as a session with a dedicated Sway workspace and OS windows as the working surface.

`hop` replaces a tmux-driven workflow with a workspace-driven workflow. The project root is the unit of session identity, `p:<session_name>` is the workspace naming rule, and session components are real OS windows rather than panes in a multiplexing layer. Decisions about command behavior should preserve this session-first model instead of reintroducing hidden multiplexing abstractions.

## Parent Principle

(none)

## Sub-Principles

- [One shared editor per session](one-shared-editor-per-session.md)
- [Role-based terminals are routing primitives](role-based-terminals-are-routing-primitives.md)
