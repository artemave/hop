# hop kill

`hop` can enter, reuse, and route work within a session, but it cannot end one. Add `hop kill` so a user can tear down the session resolved from the current working directory, close its managed OS windows, and leave no lingering session workspace behind.

## Why this fits hop

- Sessions are the core unit of the tool: exact cwd resolves to a `ProjectSession` and a dedicated `p:<session>` workspace.
- The current CLI surface covers session entry, switching, listing, editor reuse, role terminals, command routing, and a session browser, but not teardown.
- Hop already owns lifecycle and rediscovery for the session's windows, so teardown belongs in the same command surface.

## Current Context

- `hop/cli.py` and `hop/app.py` expose `switch`, `list`, `edit`, `term`, `run`, and `browser`, but no `kill` command.
- `hop/commands/session.py` resolves the caller's exact cwd into a session, switches to `p:<session>`, and ensures the shared `shell` terminal exists.
- `hop/kitty.py` tags Kitty role terminals with stable hop metadata in environment variables and user variables, then rediscovers them by session and role.
- `hop/editor.py` launches the shared Neovim instance as its own Kitty OS window, marks it with session/editor metadata, and binds it to a deterministic per-session socket path.
- `hop/browser.py` tracks the session browser through a Sway mark named `hop_browser:<session>` and explicitly supports the browser drifting off the session workspace before being reattached.
- `hop/sway.py` can list workspaces and windows and can focus, move, and mark windows, but it does not currently expose any close-window or workspace-teardown primitive.
- Current window discovery uses `session_name` for terminals, the editor, and browser marks rather than the full `project_root`, so any future teardown command that follows the same model will inherit basename-collision behavior.

## Proposed Shape

- Add a `kill` subcommand to the CLI parser and command union.
- Resolve the target session from the caller's exact cwd, matching `hop`, `hop edit`, `hop term`, `hop run`, and `hop browser`.
- Discover every window hop considers part of that session: Kitty role terminals, the shared editor window, and the marked session browser even if it drifted to another workspace.
- Close those windows, then ensure the session workspace is no longer left behind.
- Add command-level tests next to the existing session/edit/term/run/browser tests, plus adapter tests for any new close or teardown primitives.

## Open Questions

### What is the kill boundary?

#### Managed windows only

Close only windows hop can positively identify as part of the session: Kitty role windows, the shared editor, and the marked browser, regardless of which workspace they are on. This is the safest option, but it may leave unrelated windows on `p:<session>`, which could keep the workspace alive.

#### Entire session workspace

Close every window currently on `p:<session>` and also close any drifted browser carrying the session mark. This better matches "dispose of all of its windows and workspace," but it risks killing unrelated windows the user manually moved onto the workspace.

### How should users target a session?

#### Current working directory only

Make `hop kill` mirror `hop`, `hop edit`, `hop term`, `hop run`, and `hop browser` by resolving the session from the caller's exact cwd. This stays consistent with the current session model and avoids introducing a second targeting mode immediately.

#### Named session argument

Support `hop kill <session>` so a user can tear down a session from anywhere, similar to `hop switch <session>`. This is more flexible, but it introduces a new targeting style for destructive behavior and may need additional confirmation or stronger identity rules.

### What should happen to focus when killing the current session?

#### Switch away first

If the active workspace is `p:<session>`, move focus to a non-session workspace before closing windows and let Sway drop the empty session workspace naturally. This makes the post-kill landing place predictable.

#### Let Sway decide

Close the session windows in place and rely on Sway's default focus behavior. This keeps implementation simpler, but the user experience after teardown may be surprising.
