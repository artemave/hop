# hop

hop is a project session manager.

Conceptually similar to tmux sessions, except session/window management is delegated to an actual system window manager. That means:

- **Single window manager** - sway's normal shortcuts apply directly, no second layered keymap, no prefix key.
- **GUI apps are part of the session** - browser, etc., not just terminals.
- **No multiplexer in the way** - native terminal features (true color, kitty graphics, ligatures, mouse, OSC 52/8/133) work without lossy passthrough; system clipboard and scrollback are the real ones, not a copy-mode buffer.

hop is built on top of [Sway](https://swaywm.org/) window manager, [Kitty](https://sw.kovidgoyal.net/kitty/) terminal emulator and [Neovim](https://neovim.io/) as an editor. Those might become swappable building blocks in the future (potentially opening up OSX support).

## Features

- **Terminals start in the project directory.** Spawn a shell anywhere in a session and it's already `cd`-ed into the project root.
- **Open from terminal output.** A bundled Kitty kitten picks file paths and URLs from visible output and dispatches them to the session's editor or browser.
- **Shared Neovim per session** - all file links open there.
- **Shared browser per session** - all browser links open there.
- **Pluggable backends.** Shells and editor can run on the host, inside a devcontainer, over ssh, or anywhere describable as a chain of commands - without changing how you drive the session.
- **Sway and Vicinae integration.** Helper scripts for one-key session switch, kill, and moving windows in.

## Requirements

- Linux
- Python 3.12+
- [Sway](https://swaywm.org/)
- [Kitty](https://sw.kovidgoyal.net/kitty/) with remote control enabled (`allow_remote_control yes` in `kitty.conf`)
- [Neovim](https://neovim.io/)

Optionally:

- [Vicinae](https://www.vicinae.com/) launcher

## Installation

With `uv`:

```bash
uv sync
uv run hop --help
```

Or as an editable package:

```bash
python3 -m pip install -e .
```

## Usage

A session is the resolved current working directory. Every session-scoped command (`hop`, `hop edit`, `hop term`, `hop run`, `hop browser`) resolves the session this way.

### Enter or create session

```bash
hop
```

Run from a terminal with your shell `cd`-ed into the project directory. Creates the session - a Sway workspace named `p:<dirname>` with a `shell` terminal - or attaches to it if one already exists for that directory.

### Add another shell to the session

```bash
hop # run from inside one of the session's terminals
```

Spawns an additional shell terminal named `shell-2`, `shell-3`, etc.

### Switch to a named session

```bash
hop switch demo
```

Focuses the Sway workspace `p:demo`.

### List live sessions

```bash
hop list
```

Prints active Sway workspaces whose names start with `p:`.

### Open the shared editor

```bash
hop edit
hop edit app/models/user.rb
hop edit app/models/user.rb:42
```

Focuses the session's Neovim instance and optionally opens a file or `path:line` target.

### Focus or create a terminal by role

```bash
hop term --role shell
hop term --role test
hop term --role server
```

Each role maps to a dedicated Kitty window inside the session.

### Send a command to a role terminal

```bash
hop run "ls"
hop run --role test "python3 -m pytest -q"
hop run --role server "bin/dev"
```

The command must be a single CLI argument. The default role is `shell`. `hop run` dispatches the command, prints an opaque run id, and returns immediately - it does not wait for completion.

### Stream the output of a previous `hop run`

```bash
id=$(hop run --role test "python3 -m pytest -q")
hop tail "$id"
```

`hop tail` blocks until the dispatched command returns to its shell prompt, then writes the combined output to stdout. Together with `hop run` it forms the two-step protocol used by [vigun](https://github.com/artemave/vigun).

Prompt detection uses Kitty's shell integration (OSC 133), which is on by default for `bash`, `zsh`, and `fish`.

### Browser command

```bash
hop browser
hop browser https://example.com
```

Reuses or creates a session-owned window in your default browser. If the window was moved to another workspace, `hop browser` moves it back before focusing it.

### Kill the current session

```bash
hop kill # run from the project root
```

Closes every Sway/Kitty window owned by the session, removes its workspace, and runs the backend's `teardown`.

## Sway shortcuts

Bind these helper scripts in your Sway config for one-keystroke access:

```conf
# New shell in a hop session, or plain kitty otherwise
bindsym $mod+Return exec /path/to/hop/sway/hop-term-or-kitty

# Kill the focused hop session
bindsym $mod+Shift+k exec /path/to/hop/sway/hop-kill-session
```

`hop-term-or-kitty` accepts an optional fallback terminal as its first arg (e.g. `… exec /path/to/sway/hop-term-or-kitty alacritty`).

## Vicinae launcher integration

[Vicinae](https://www.vicinae.com/) script commands live in `vicinae/`. Install them by symlinking into Vicinae's scripts directory:

```bash
mkdir -p ~/.local/share/vicinae/scripts
ln -s "$PWD/vicinae/hop-switch-session"        ~/.local/share/vicinae/scripts/
ln -s "$PWD/vicinae/hop-move-window-to-session" ~/.local/share/vicinae/scripts/
ln -s "$PWD/vicinae/hop-kill-session"           ~/.local/share/vicinae/scripts/
```

Reload Vicinae's script directories or restart Vicinae afterwards.

The same scripts can be bound directly in Sway, skipping Vicinae's launcher UI:

```conf
bindsym $mod+Shift+s exec /path/to/hop/vicinae/hop-switch-session
bindsym $mod+Shift+m exec /path/to/hop/vicinae/hop-move-window-to-session
```

`hop-move-window-to-session` only works when triggered via a Sway keybinding, not from inside the Vicinae launcher (the launcher would be the focused window).

## Open visible-output targets from Kitty

Add a Kitty mapping that runs the `hints` kitten with hop's custom processor:

```conf
map ctrl+shift+o kitten hints --customize-processing /path/to/hop/kittens/open_selection/main.py
```

The picker scans visible terminal output and dispatches supported selections to the session editor or browser:

- `app/models/user.rb`
- `app/models/user.rb:42`
- `b/app/models/user.rb`
- `https://example.com`
- `Processing UsersController#index`

File-shaped tokens that don't resolve to a real file under the source window's cwd are not highlighted.

## Session backends

A session has a **backend** that decides where its windows run. The default is **host**. Other backends - devcontainer, ssh, anything else describable as a chain of commands - are configured as named entries in `~/.config/hop/config.toml` or a project's `.hop.toml`. Both files use the **same schema** and are merged at session entry — there is no difference between global and project configs beyond which file you put a section in.

### Auto-detection

When you enter a session (bare `hop`), hop walks the configured backends in declaration order and runs each backend's `default` probe in the project root. The first one that exits 0 wins. If none succeed, the session falls back to **host**. The chosen backend is persisted and reused for all subsequent commands against that session.

### Top-level shape

A hop config has three named sections plus one scalar setting, all optional:

- `[backends.<name>]` — backend lifecycle (`prepare` / `teardown` / `workspace` / translate helpers) plus a `command_prefix` shell snippet that wraps every command launched in that backend's environment.
- `[layouts.<name>]` — a named layout with one required `autostart` shell-snippet probe and a list of windows that come up together when the probe matches.
- `[windows.<role>]` — top-level windows (always autostart unless `autostart = "false"`).
- `workspace_layout = "<mode>"` — sway workspace layout applied at first session entry. One of `splith`, `splitv`, `stacking`, `tabbed`.

### Backend example

```toml
[backends.devcontainer]
default        = "test -f docker-compose.dev.yml"
prepare        = "podman-compose -f docker-compose.dev.yml up -d devcontainer"
teardown       = "podman-compose -f docker-compose.dev.yml down"
workspace      = "podman-compose -f docker-compose.dev.yml exec devcontainer pwd"
command_prefix = "podman-compose -f docker-compose.dev.yml exec devcontainer"
```

Each command is a single string. Hop runs it through `sh -c` after substituting placeholders, so pipes, redirects, and `$(...)` all work — write the value the way you'd type it at a terminal. Use TOML triple-quoted strings (`"""…"""`) for multi-line pipelines. Placeholder values are shell-quoted before insertion, so paths with spaces substitute safely.

Backend fields:

- `default` (optional) - auto-detect probe. Backends without `default` can only be picked by name.
- `prepare` (optional) - command run once at session creation, before launching kitty. Should be idempotent.
- `teardown` (optional) - command run at `hop kill` after closing windows.
- `workspace` (optional) - command whose stdout maps the backend's path back to the host project root. Used by the open_selection kitten.
- `port_translate` (optional) - command run lazily by the open_selection kitten when it dispatches a `localhost` / `127.0.0.1` / `0.0.0.0` URL. Stdout is the host-reachable port that should replace the URL's port. `{port}` is substituted with the URL's original port.
- `host_translate` (optional) - command run lazily for the same set of localhost URLs. Stdout is the hostname that should replace `localhost` / `127.0.0.1` / `0.0.0.0` in the URL.
- `command_prefix` (optional) - shell snippet prepended to every window command launched in this backend's environment. Empty for the implicit host backend.

Supported placeholders: `{project_root}` (anywhere), and `{port}` (in `port_translate` / `host_translate` only).

The name `host` is reserved for the implicit fallback.

### Layouts and windows

Per-role launch commands live outside the backend, in `[layouts.<name>]` or `[windows.<role>]` tables:

```toml
# A layout: one autostart probe, multiple windows.
[layouts.rails]
autostart = "test -f bin/rails"

[layouts.rails.windows.server]
command = "bin/dev"

[layouts.rails.windows.console]
command   = "bin/rails console"
autostart = "false"  # declared for `hop term --role console`; not auto-launched

# Top-level window: always autostart unless opted out.
[windows.worker]
command = "bin/jobs"
```

The active backend's `command_prefix` wraps each window's `command` at launch, so the same Rails layout works in both a host session (runs `bin/dev` directly) and a devcontainer session (runs `podman-compose exec devcontainer bin/dev`).

Per-window fields:

- `command` (string) - the role command, **without** any backend wrap. The active backend's `command_prefix` is prepended at launch.
- `autostart` (`"true"` or `"false"`, optional) - opt-in / opt-out only. The autostart gate is whatever the window's container decides (built-in default for built-ins, layout's probe for layout windows, always-on for top-level user windows).

Built-in roles `shell`, `editor`, and `browser` ship with hop defaults:

| role    | command default                         | autostart default |
|---------|-----------------------------------------|-------------------|
| shell   | platform default (kitty's login shell on host; `${SHELL:-sh}` falls back inside a `command_prefix`) | autostart |
| editor  | `nvim`                                  | autostart         |
| browser | xdg-detected default browser            | not autostart     |

To change a built-in, declare it as a top-level window: `[windows.editor] autostart = "false"` opts out of the editor for this config; `[windows.browser] autostart = "true"` autostarts the browser; `[windows.shell] command = "/usr/bin/zsh"` overrides the shell.

Multiple matching layouts compose: a Rails project that also has `vite.config.ts` activates both layouts and gets their windows.

### Per-invocation override

```bash
hop --backend <name>
```

Forces a backend at session creation regardless of auto-detect. Use `hop --backend host` to keep the host backend in a project that would otherwise auto-activate something else. The choice is persisted for the session's lifetime.

For step-by-step devcontainer setup and troubleshooting, see [`docs/devcontainer.md`](docs/devcontainer.md).

## Development

```bash
uv run pytest -q
```
