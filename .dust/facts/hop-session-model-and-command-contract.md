# hop session model and command contract

The current `hop` contract is a session-oriented CLI built around projects, Sway workspaces, Kitty windows, and one shared Neovim instance.

Per [hop_spec.md](../../hop_spec.md), a session corresponds to a project root and consists of a dedicated Sway workspace named `p:<session_name>`, one shared Neovim instance, multiple Kitty terminal windows identified by role, and an optional browser window. The intended command surface is `hop`, `hop switch <session>`, `hop list`, `hop edit [target]`, `hop term --role <name>`, `hop run [--role <name>] "<command>"`, and `hop browser [url]`.

The current Python scaffold implements that contract as a pure parser plus adapter-driven command runner. `hop.session` centralizes project-root discovery, session-name derivation from the project directory basename, and workspace-name derivation as `p:<session>`. `hop.cli` parses argv into typed commands, while `hop.app` routes those commands through Sway, Kitty, Neovim, and browser adapter boundaries without embedding tool-specific calls in the parser.
