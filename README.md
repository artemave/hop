# hop

`hop` is a session-oriented workspace CLI for Sway, Kitty, and Neovim.

Think of it as tmux, but:

- with GUI apps being part of session
- no separate window switching shortcuts
- no terminal multiplexing layer

It treats each project as a session and uses:

- Sway workspaces for session switching
- Kitty for role-based terminals
- one shared Neovim instance per session

## Requirements

- Python 3.12+
- [Sway](https://swaywm.org/)
- [Kitty](https://sw.kovidgoyal.net/kitty/)
- [Neovim](https://neovim.io/)

`hop` is designed for Linux desktop workflows that already use Sway.

## Prerequisites

Kitty must have remote control enabled. Add this to your `kitty.conf`:

```conf
allow_remote_control yes
```

## Installation

### Install for development with `uv`

```bash
uv sync
```

Run the CLI from the repo with:

```bash
uv run hop --help
```

### Install as an editable package

```bash
python3 -m pip install -e .
```

That exposes the `hop` command in your active Python environment.

## Usage

A session is always the resolved current working directory. Every session-scoped command (`hop`, `hop edit`, `hop term`, `hop run`, `hop browser`) resolves the session this way.

A few commands also distinguish between being **inside** vs **outside** a session. "Inside" means the focused Sway workspace is the cwd-derived session's workspace (`p:<dirname>`) — whether you're focused on a hop terminal, the editor, or some other window in that workspace. Anything else — a different workspace, a launcher prompt, an external script — is "outside". Where the distinction matters, it's noted below.

### Enter or create session

```bash
hop
```

Run from a terminal outside the session, with your shell `cd`-ed into the project directory. Creates the session — a dedicated Sway workspace named `p:<dirname>` with a `shell` terminal — or attaches to it if you've already created it from this directory.

### Add another shell to the session

```bash
hop # run from inside one of the session's terminals
```

Spawns an additional shell terminal in the same session, named `shell-2`, `shell-3`, etc.

For one-keystroke access, bind a sway shortcut to `sway/hop-term-or-kitty`:

```conf
bindsym $mod+Return exec /path/to/hop/sway/hop-term-or-kitty
```

The script asks sway for the focused workspace; if it's a hop session (`p:*`), it `cd`s into the session's project root and runs `hop term` (giving you a fresh `shell-N`). Otherwise it falls back to plain `kitty`. Override the fallback by passing it as the first arg, e.g. `… exec /path/to/sway/hop-term-or-kitty alacritty`.

### Switch to a named session

```bash
hop switch demo
```

This focuses the Sway workspace `p:demo`.

### List live sessions

```bash
hop list
```

This prints active Sway workspaces whose names start with `p:`.

### Open the shared editor

```bash
hop edit
hop edit app/models/user.rb
hop edit app/models/user.rb:42
```

This focuses the session Neovim instance and optionally opens a file or `path:line` target.

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

Notes:

- the default role is `shell`
- the command must be passed as a single CLI argument
- `hop` routes the command to the target terminal and prints an opaque run id on stdout, then returns immediately
- `hop` does not wait for the command to finish or proxy its exit status

### Stream the output of a previous `hop run`

```bash
id=$(hop run --role test "python3 -m pytest -q")
hop tail "$id"
```

`hop tail` blocks until the dispatched command returns to its shell prompt, then writes that command's combined output to stdout and exits 0. It is the second half of the two-step protocol used by [vigun](https://github.com/artemave/vigun): `hop run` dispatches and hands back an id, `hop tail` waits and delivers the output.

Detection relies on Kitty's shell integration (OSC 133 prompt boundaries), which is on by default for `bash`, `zsh`, and `fish`. If you've disabled it in your role terminal, `hop tail` cannot tell when a command has finished.

Run state is persisted to `$XDG_RUNTIME_DIR/hop/runs/<id>.json` (or `/tmp/hop/runs/<id>.json` if `XDG_RUNTIME_DIR` is unset). Override the location with the `HOP_RUNS_DIR` environment variable.

### Browser command

```bash
hop browser
hop browser https://example.com
```

This reuses or creates a session-owned window in your default browser. If that window was moved to another workspace, `hop browser` moves it back to the session workspace before focusing it.

### Kill the current session

```bash
hop kill # run from the project root
```

Closes every Sway/Kitty window owned by the session, removes its workspace, and runs the backend's `teardown` (e.g. `compose down`).

For one-keystroke access from the focused session workspace, bind a sway shortcut to `sway/hop-kill-session`:

```conf
bindsym $mod+Shift+k exec /path/to/hop/sway/hop-kill-session
```

The script reads the focused workspace from sway, looks up its host `project_root` via `hop list --json`, and runs `hop kill` from there. Handy when the session's shells live inside a container or remote backend without `hop` installed, so you can't run `hop kill` from inside the session terminal.

### Switch sessions from the Vicinae launcher

`vicinae/hop-switch-session` is a [Vicinae](https://www.vicinae.com/) script command that lists live hop sessions in the launcher and switches to the one you pick.

Install it by linking the script into your Vicinae scripts directory:

```bash
mkdir -p ~/.local/share/vicinae/scripts
ln -s "$PWD/vicinae/hop-switch-session" ~/.local/share/vicinae/scripts/
```

Then trigger *Reload Script Directories* from Vicinae's root search (or restart Vicinae) and search for *Switch hop session*. The script shells out to `hop list` for the entries, pipes them through `vicinae dmenu`, and runs `hop switch <name>` on the chosen session.

For one-keystroke access, bind the script directly in your Sway config:

```conf
bindsym $mod+Shift+s exec /path/to/hop/vicinae/hop-switch-session
```

That skips Vicinae's launcher UI and pops the session picker straight away — the `# @vicinae.*` headers are inert when the script runs outside Vicinae's index, so the same file serves both entrypoints.

### Move the focused window into a hop session's workspace

`vicinae/hop-move-window-to-session` captures the currently focused window's sway `con_id`, lists live hop sessions in the launcher, and on pick runs `swaymsg` to move that window into `p:<chosen>` and switch to the destination.

It only works when invoked **directly via a sway keybinding** — invoking it from inside Vicinae's launcher means Vicinae itself is focused at script-start, so the script has nothing useful to move:

```bash
mkdir -p ~/.local/share/vicinae/scripts
ln -s "$PWD/vicinae/hop-move-window-to-session" ~/.local/share/vicinae/scripts/
```

```conf
bindsym $mod+Shift+m exec /path/to/hop/vicinae/hop-move-window-to-session
```

### Kill the focused hop session from the Vicinae launcher

`vicinae/hop-kill-session` is a thin wrapper around the `sway/hop-kill-session` helper that exposes it under Vicinae as *Hop kill current session*. Open Vicinae, type `hk` (or any prefix that fuzzy-matches), hit Enter, and the focused hop session is gone. No-op when the focused workspace isn't a `p:*` workspace.

```bash
mkdir -p ~/.local/share/vicinae/scripts
ln -s "$PWD/vicinae/hop-kill-session" ~/.local/share/vicinae/scripts/
```

Unlike `hop-move-window-to-session`, this one is safe to invoke from inside Vicinae's launcher: it reads the focused *workspace* (which Vicinae doesn't change — it floats over the user's current workspace), not the focused window.

### Open visible-output targets from Kitty

Add a Kitty mapping that runs the `hints` kitten with hop's custom processor:

```conf
map ctrl+shift+o kitten hints --customize-processing /path/to/hop/kittens/open_selection/main.py
```

That picker works over visible terminal output and dispatches supported selections to the session editor or browser:

- `app/models/user.rb`
- `app/models/user.rb:42`
- `b/app/models/user.rb`
- `https://example.com`
- `Processing UsersController#index`

File-shaped tokens that don't resolve to a real file under the source window's cwd are not highlighted. Dispatch attempts (and skip reasons) are written to `$XDG_RUNTIME_DIR/hop/open-selection.log` for debugging.

## Session backends

A session has a **backend** that decides where its shells and editor run. The default is **host** (shells and nvim run on the host). Other backends — devcontainer, ssh, anything else you can describe as a chain of commands — are configured as named entries in either `~/.config/hop/config.toml` or a project's `.hop.toml`. Both files use the same `[backends.<name>]` schema and are merged at session entry.

### Auto-detection

When you enter a session (bare `hop`), hop walks the configured backends in declaration order and runs each backend's `default` probe command in the project root. The first one that exits 0 wins. If none succeed (or no backend has a `default`), the session falls back to **host**.

Once a session is created, the chosen backend is persisted in `${XDG_RUNTIME_DIR}/hop/sessions/<name>.json` (with the resolved commands and the discovered workspace path) and reused for every subsequent `hop term`, `hop run`, `hop edit`, and `hop kill` against that session — auto-detect is not re-run mid-session.

### Global config

Create `${XDG_CONFIG_HOME:-~/.config}/hop/config.toml`:

```toml
[backends.devcontainer]
default   = ["test", "-f", "docker-compose.dev.yml"]
prepare   = ["podman-compose", "-f", "docker-compose.dev.yml", "up", "-d", "devcontainer"]
shell     = ["podman-compose", "-f", "docker-compose.dev.yml", "exec", "devcontainer", "/usr/bin/zsh"]
editor    = ["podman-compose", "-f", "docker-compose.dev.yml", "exec",
             "devcontainer", "nvim", "--listen", "{listen_addr}"]
teardown  = ["podman-compose", "-f", "docker-compose.dev.yml", "down"]
workspace = ["podman-compose", "-f", "docker-compose.dev.yml", "exec", "devcontainer", "pwd"]
```

Fields per backend:

- `shell` (required) — argv hop runs to spawn one shell per role terminal.
- `editor` (required) — argv hop runs to launch the shared nvim. `{listen_addr}` is substituted with the host-visible nvim socket path.
- `default` (optional) — auto-detect probe. Hop runs it in the project root; exit 0 selects this backend. Backends without `default` are not eligible for auto-detect — they can only be picked by name (`hop --backend <name>` or `[backend].name` in `.hop.toml`).
- `prepare` (optional) — argv hop runs once at session creation, before launching kitty. Idempotent (e.g. `compose up -d`).
- `teardown` (optional) — argv hop runs at `hop kill` after closing windows.
- `workspace` (optional) — argv whose stdout is the path inside the backend that maps to the host project root. Used by the open_selection kitten to translate visible-output cwds back to host paths. Captured once at session creation.

Supported placeholders inside command lists: `{listen_addr}` (in `editor`) and `{project_root}` (anywhere).

The name `host` is reserved for the implicit fallback — an explicit `[backends.host]` table is ignored.

### Project config

`<project_root>/.hop.toml` uses **the same `[backends.<name>]` schema** as the global file. Drop in whatever subset of fields you want — partial entries are fine. Hop merges the two files when resolving the session backend:

- Project entries come first in auto-detect order.
- Same-named entries are field-merged with project fields winning.
- Backends without `shell` and `editor` after merge are unusable and dropped silently.

So the same syntax handles every project-level use case in one example:

```toml
# Project's docker-compose.dev.yml uses a different service name.
[backends.devcontainer]
shell = ["docker", "compose", "-f", "compose.dev.yml", "exec", "app", "zsh"]

# Always force devcontainer here even when something else would also match.
# (default = ["true"] always exits 0; default = ["false"] always skips.)
# [backends.devcontainer]
# default = ["true"]

# Define a project-specific backend that doesn't exist in ~/.config/hop/config.toml.
[backends.my-vm]
default   = ["test", "-f", ".my-vm-marker"]
shell     = ["lima", "shell", "default", "--", "/usr/bin/zsh"]
editor    = ["lima", "shell", "default", "--", "nvim", "--listen", "{listen_addr}"]
workspace = ["lima", "shell", "default", "--", "pwd"]
```

To force the host backend in a project, either pass `hop --backend host` once at session creation (persisted afterwards) or override every configured backend's `default` to a failing command.

### Per-invocation override: `--backend <name>`

`hop --backend <name>` from a project root creates the session with the named backend, regardless of auto-detect. Use `hop --backend host` to force the host backend in a project that would otherwise auto-activate something else. The choice is persisted, so subsequent `hop term`, `hop run`, etc. against that session keep using the same backend without you re-passing the flag.

The flag is only valid on the bare `hop` entry — `hop switch --backend …`, `hop run --backend …` (etc.) are rejected. Once a session exists, the flag has no effect; it's a session-creation knob. Passing a backend name that isn't configured (and isn't `host`) errors out.

### Self-contained project compose files

Each project's `docker-compose.dev.yml` should be **self-contained** so hop (and any other tool) can run it standalone without an overlay flag. The recommended pattern is `include:`:

```yaml
include:
  - ${HOME}/projects/dotfiles/devcontainer/docker-compose.yml
services:
  devcontainer:
    # any project-specific overrides
```

Notes:

- `podman-compose` (the Python implementation) accepts the legacy plain-string `include:` list shape; the modern `{path: ...}` dict form will fail. Use plain strings.
- `${HOME}` is expanded by compose's environment substitution. `~` is **not** expanded — use `${HOME}` instead.

For step-by-step setup and troubleshooting, see [`docs/devcontainer.md`](docs/devcontainer.md).

## Development

Run tests with:

```bash
uv run pytest -q
```

If you are not using `uv`, install the package in editable mode and run:

```bash
pytest -q
```
