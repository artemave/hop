# hop

hop is a project session manager.

Conceptually similar to tmux sessions, except session/window management is delegated to an actual system window manager and app launcher. That means:

- **Single window manager** - sway's normal shortcuts apply directly, no second layered keymap, no prefix key.
- **GUI apps are part of the session** - browser, etc., not just terminals.
- **No multiplexer in the way** - native terminal features (true color, kitty graphics, ligatures, mouse, OSC 52/8/133) work without lossy passthrough; system clipboard and scrollback are the real ones, not a copy-mode buffer.

hop is built on top of [Sway](https://swaywm.org/) window manager, [Kitty](https://sw.kovidgoyal.net/kitty/) terminal emulator and [Neovim](https://neovim.io/) as an editor. Those might become swappable building blocks in the future (potentially opening up OSX support).

## Features

- **Terminals start in the project directory** - spawn a shell anywhere in a session and it's already `cd`-ed into the project root.
- **Open from terminal output** - bundled Kitty kitten picks file paths and URLs from visible output and dispatches them to the session's editor or browser.
- **Shared Neovim per session** - all file links open there.
- **Shared browser per session** - all browser links open there.
- **Pluggable backends** - shells and editor can run on the host, inside a devcontainer, over ssh, or anywhere describable as a chain of commands - without changing how you drive the session.
- **Sway and Vicinae integration** - helper scripts for one-key session switch, kill, focusing, etc.
- **Scriptable** - all features are accessible from the CLI.

## Requirements

- Linux
- Python 3.12+
- [Sway](https://swaywm.org/)
- [Kitty](https://sw.kovidgoyal.net/kitty/) with remote control enabled (`allow_remote_control yes` in `kitty.conf`)
- [Neovim](https://neovim.io/)

Optionally:

- [Vicinae](https://www.vicinae.com/) launcher

## Installation

```bash
git clone https://github.com/artemave/hop
cd hop
uv tool install .
```

Or, as an editable pip package:

```bash
python3 -m pip install -e .
```

## Usage

Run `hop` from a terminal `cd`-ed into your project. This creates the session - a Sway workspace named `p:<dirname>` - or attaches to it if one already exists for that directory.

Day-to-day use is driven by three surfaces, each with its own section below:

- [Sway shortcuts](#sway-shortcuts) - a key for "new shell in this session".
- [Vicinae launcher integration](#vicinae-launcher-integration) - the focused session's declared windows, plus session-switch / kill, surface as direct entries in the launcher's main search.
- [Open visible-output targets from Kitty](#open-visible-output-targets-from-kitty) - a Kitty kitten that picks file paths and URLs from terminal output and routes them to the session's editor or browser.

To kill a session, run `hop kill` from the project root on the host or pick `Hop kill` from the Vicinae launcher (below).

## Sway shortcuts

Bind this helper script in your Sway config to spawn a new shell in the focused hop session (or a plain kitty when not on a hop workspace):

```conf
bindsym $mod+Return exec /path/to/hop/sway/hop-term-or-kitty
```

## Vicinae launcher integration

[Vicinae](https://www.vicinae.com/) integration is driven by `hopd`, a small daemon shipped alongside `hop` that subscribes to Sway's workspace-focus events and rewrites `~/.local/share/vicinae/scripts/hop-*` to reflect the focused session. Wire it in your Sway config:

```conf
exec hopd
```

On every workspace focus change, `hopd` regenerates the Vicinae script set.

What you get in the launcher's main search depends on what's focused:

- **On a hop session's workspace** (`p:<session>`): one entry per declared window — `Hop editor`, `Hop browser`, `Hop shell`, plus any custom roles like `Hop console`, `Hop server`, `Hop test`, etc. (the same set `hop windows` lists). Plus `Hop kill` for the focused session and `Hop switch to <other-session>` for every other live session.
- **Off any hop workspace**: only `Hop switch to <session>` per live session — no `Hop kill`, no per-window entries to clutter unrelated workspaces.

Fuzzy queries hit a single search box: `hop con` → `Hop console`, `hop sw rails` → `Hop switch to rails`, `hop ki` → `Hop kill`. No sub-menus.

Hop owns the `hop-*` filename namespace in `~/.local/share/vicinae/scripts/`; any other files in that directory are left alone.

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

## Configuration

A hop config has three named sections plus one scalar setting, all optional:

- `[backends.<name>]` - backend lifecycle (`prepare` / `teardown` / `workspace` / translate helpers) plus a `command_prefix` shell snippet that wraps every command launched in that backend's environment.
- `[layouts.<name>]` - a named layout with one required `autostart` shell-snippet probe and a list of windows that come up together when the probe matches.
- `[windows.<role>]` - top-level windows (always autostart unless `autostart = "false"`).
- `workspace_layout = "<mode>"` - sway workspace layout applied at first session entry. One of `splith`, `splitv`, `stacking`, `tabbed`.

Configs live in `~/.config/hop/config.toml` or a project's `.hop.toml`.

## Session backends

A session has a **backend** that decides where its windows run. The default is **host**. Other backends - devcontainer, ssh, anything else describable as a chain of commands - are configured as named entries in the config file.

### Auto-detection

When you enter a session (bare `hop`), hop walks the configured backends in declaration order and runs each backend's `default` probe in the project root. The first one that exits 0 wins. If none succeed, the session falls back to **host**. The chosen backend is persisted and reused for all subsequent commands against that session.

### Backend example

```toml
[backends.devcontainer]
default        = "test -f docker-compose.dev.yml"
prepare        = "podman-compose -f docker-compose.dev.yml --in-pod=false up -d devcontainer"
teardown       = "podman-compose -f docker-compose.dev.yml down"
workspace      = "podman-compose -f docker-compose.dev.yml exec devcontainer pwd"
port_translate = """
  podman ps -q \\
    --filter label=io.podman.compose.project=$(basename {project_root}) \\
    --filter label=io.podman.compose.service=devcontainer \\
    | head -1 \\
    | xargs -r -I@ podman port @ {port} \\
    | cut -d: -f2
"""
command_prefix = "podman-compose -f docker-compose.dev.yml exec devcontainer"
```

Each command is a single string. Hop runs it through `sh -c` after substituting placeholders, so pipes, redirects, and `$(...)` all work - write the value the way you'd type it at a terminal. Use TOML triple-quoted strings (`"""…"""`) for multi-line pipelines. Placeholder values are shell-quoted before insertion, so paths with spaces substitute safely.

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
[layouts.rails]
autostart = "test -f bin/rails"

[layouts.rails.windows.server]
command = "bin/dev"

[layouts.rails.windows.console]
command   = "bin/rails console"
autostart = "false"

# Top-level window
[windows.worker]
command = "bin/jobs"
```

The active backend's `command_prefix` wraps each window's `command` at launch, so the same Rails layout works in both a host session (runs `bin/dev` directly) and a devcontainer session (runs `podman-compose exec devcontainer bin/dev`).

Per-window fields:

- `command` (string) - the role command, **without** any backend wrap. The active backend's `command_prefix` is prepended at launch.
- `autostart` (string, optional) - shell probe; the window auto-launches when it exits 0. Defaults to `"true"`.

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

## Automation

The `hop` CLI runs on the host. In a devcontainer session it's not available inside the container - scripts that drive a session run on the host side. The commands below are the integration surface for external tools.

### `hop run` and `hop tail`

```bash
hop run "ls"
hop run --role test "python3 -m pytest -q"
hop run --role server "bin/dev"
hop run --role server --focus "bin/dev"
```

The command must be a single CLI argument. The default role is `shell`. `hop run` dispatches the command, prints an opaque run id, and returns immediately - it does not wait for completion.

By default `hop run` keeps the current focus, which is what automated callers like `vigun` want. Pass `--focus` to focus the role terminal and switch Sway to the session's workspace - useful when you're driving `hop run` interactively and want to immediately watch the role you just dispatched into.

```bash
id=$(hop run --role test "python3 -m pytest -q")
hop tail "$id"
```

`hop tail` blocks until the dispatched command returns to its shell prompt, then writes the combined output to stdout. This two-step protocol is what [vigun](https://github.com/artemave/vigun) uses to send a test run from the editor to a dedicated terminal in the session and collect its output once the run finishes.

Prompt detection uses Kitty's shell integration (OSC 133), which is on by default for `bash`, `zsh`, and `fish`.

### Other commands

- `hop list` - print active Sway workspaces whose names start with `p:`.
- `hop switch <name>` - focus the Sway workspace `p:<name>`.
- `hop edit [<file>[:<line>]]` - focus the session's Neovim and optionally open a file or `path:line` target.
- `hop term --role <name>` - focus or create a Kitty window for the given role.
- `hop browser [<url>]` - reuse or create a session-owned browser window. If the window was moved to another workspace, it's moved back before being focused.
- `hop kill` - close every Sway/Kitty window owned by the session, remove its workspace, and run the backend's `teardown`. Run from the project root.

## Development

```bash
uv sync
uv run pytest -q
```
