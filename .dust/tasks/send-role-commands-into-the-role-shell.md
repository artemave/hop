# Send role commands into the role shell

Launch every terminal role as a plain shell and send its command in via `send-text`, replacing the `<cmd>; <shell>` launch composition.

## Background

Terminal role windows (server, console, log, test, custom top-level windows) currently bake their command into the launch argv: `_shell_like_command` (`hop/kitty.py:586-620`) composes `<command>; <shell>` so the window runs the command and, when it exits, drops to an interactive shell. The `_command_for_role` inheritance supplies the trailing shell.

hop already has a second, simpler way to put a command in a role terminal: `run_in_terminal` (`hop/kitty.py:236-265`) finds-or-launches the role window and `send-text`s the command with a trailing newline — this is what `hop run` uses. The two mechanisms differ only in *when* the command is delivered: composition bakes it into the launch; `run_in_terminal` types it into a running shell.

Typing it in is closer to what a user does and has two concrete advantages:

- **The command lands in shell history.** A `send-text` keystroke is a real history entry, so up-arrow re-runs it — the natural move after Ctrl-C'ing a server. The `<cmd>; <shell>` form can't do this (the command was a `-c` argument, never typed).
- **Shell syntax works with no wrapping.** `pkill …; bin/dev`, pipes, and redirections are parsed by the interactive shell itself. There is nothing to quote or compose.

This task makes bootstrap dispatch terminal-role commands the way `hop run` already does, and drops the `<cmd>; <shell>` composition.

## Design

### Terminal roles launch a bare shell

`_shell_like_command` / `_launch_args` stop composing `<command>; <shell>`. Every *terminal* role (`shell`, `server`, `console`, `log`, `test`, ad-hoc `shell-N`, custom top-level windows) launches the shell-slot value alone — every terminal role becomes "shell-like" at launch. The role's own command is no longer part of the launch argv.

### The command is sent after launch

Bootstrap (`enter_project_session`, `hop/commands/session.py`) dispatches each terminal role's resolved command by reusing the existing `send-text` path. For each declared terminal role with a non-empty command (server, console, log, a `test` with a command, top-level windows), once the role's shell window exists, `send-text` the command + `\n` into it — exactly what `run_in_terminal` does today. The cleanest wiring routes these through `run_in_terminal(session, role=…, command=…)`: with the composition gone, launching a terminal role *is* launching its shell, so `run_in_terminal` becomes "launch the role shell, then type the command."

The `shell` role has no command → just the shell, nothing sent.

Commands are sent as-configured. Until [Backend owns the integrated login shell](backend-owns-the-integrated-login-shell.md) lands, a hand-wrapped command like `sh -c '$SHELL -lc "bin/dev"'` is sent verbatim and still runs (its own wrap supplies login/PATH); the history entry is just the wrapped string. That task later unwraps the commands and makes the shell itself login, so the typed history entries become clean.

### Optimistic send

Send immediately after launch, as `run_in_terminal` already does — no `at_prompt` gate. The pty buffers input, and a shell sources its rc before entering its read loop, so a command sent right after launch is read once the shell is ready.

### Unchanged

- **Editor** stays the shared-nvim launch (one-shared-editor-per-session); **browser** stays xdg. Neither is a "type a command into a shell" role.
- **Re-entry** still skips the sweep — the command is sent only when the role window is first created.

## Files to change

- `hop/kitty.py` — `_shell_like_command` / `_launch_args`: drop the `<command>; <shell>` composition; every terminal role launches the shell-slot value. Reuse the `run_in_terminal` `send-text` seam for post-launch dispatch (extract a helper if it clarifies the shared path).
- `hop/commands/session.py` — `enter_project_session`: for each terminal role with a resolved command, launch the role shell and send the command (via the `run_in_terminal` path); the shell role launches with nothing sent.
- `hop/app.py` — thread the resolved per-role commands into the bootstrap dispatch if not already available there.
- `hop_spec.md`, `README.md` — document that terminal-role commands are typed into the role shell (and land in history), replacing the drop-into-shell composition.

## Tests

Real subprocesses / no mocks per convention.

- `tests/test_kitty.py`:
  - A terminal role with a command launches a bare shell (the shell-slot value), not a `<cmd>; <shell>` composition.
  - After launch, the role's command is `send-text`'d with a trailing newline.
  - A command with shell syntax (`pkill -f '[f]oreman'; bin/dev`) is sent verbatim (no wrapping/quoting).
  - The `shell` role launches the shell with nothing sent; an ad-hoc `shell-N` role likewise.
- `tests/test_session_commands.py`:
  - `enter_project_session` launches each terminal role's shell and sends its command; editor / browser roles still route to their adapters.
  - Re-entry sends nothing (the command is delivered only on first creation).
- `tests/test_app.py`:
  - end-to-end: a Rails layout session launches server / console shells and sends `bin/dev` / `bin/rails console` into them.

## Out of scope

- Gating the send on `at_prompt` (OSC 133 readiness). Optimistic send matches today's `run_in_terminal`; a readiness gate plus timeout is a follow-up only if a real startup race appears.
- The `backend.shell` field and login-wrapping — [Backend owns the integrated login shell](backend-owns-the-integrated-login-shell.md) handles those. This task keeps whatever shell the current resolution produces; it only changes *how the command reaches it*.
- Editor / browser dispatch. They keep their launch adapters.

## Task Type

implement

## Principles

- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `_shell_like_command` / `_launch_args` no longer compose `<command>; <shell>`; every terminal role launches the shell-slot value alone.
- Bootstrap sends each terminal role's resolved command into its shell window via the existing `send-text` path (the `run_in_terminal` seam), with a trailing newline, on first creation only.
- Commands containing shell syntax are sent verbatim, with no wrapping or quoting.
- The `shell` role and ad-hoc `shell-N` roles launch a shell with nothing sent; editor and browser keep their launch adapters.
- Re-entry re-sends nothing.
- Tests in the Tests section pass under `uv run pytest -q` and follow the no-mock convention.
- `hop_spec.md` and `README.md` document the typed-command dispatch.
- `make` passes (test, typecheck, lint, format-check, 100% coverage).
- `bunx dust lint` passes for the task file.
