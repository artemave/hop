# hop session model and command contract

The current `hop` contract is a session-oriented CLI built around projects, Sway workspaces, Kitty windows, and one shared Neovim instance.

Per [hop_spec.md](../../hop_spec.md), a session corresponds to a project root and consists of a dedicated Sway workspace named `p:<session_name>`, one shared Neovim instance, multiple Kitty terminal windows identified by role, and an optional browser window. The intended command surface is `hop`, `hop switch <session>`, `hop list`, `hop edit [target]`, `hop term --role <name>`, `hop run --role <name> "<command>"`, and `hop browser [url]`.
