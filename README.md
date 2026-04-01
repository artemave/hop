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

### Enter or create session

Run `hop` in the directory you want to use as the session root. This creates a dedicated Sway workspace with a terminal. Running `hop` again from that same directory reuses the same session. Running it from a nested directory creates a different session rooted at that nested directory.

The same directory-rooted rule applies to every session-scoped command: `hop`, `hop edit`, `hop term`, `hop run`, and `hop browser`.

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
- `hop` routes the command to the target terminal and returns immediately
- `hop` does not wait for the command to finish or proxy its exit status

### Browser command

```bash
hop browser
hop browser https://example.com
```

This reuses or creates a session-owned window in your default browser. If that window was moved to another workspace, `hop browser` moves it back to the session workspace before focusing it.

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

## Development

Run tests with:

```bash
uv run pytest -q
```

If you are not using `uv`, install the package in editable mode and run:

```bash
pytest -q
```
