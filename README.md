# hop

`hop` is a session-oriented workspace CLI for Sway, Kitty, and Neovim.

It treats each project as a session and uses:

- Sway workspaces for session switching
- Kitty OS windows for role-based terminals
- one shared Neovim instance per session

## Requirements

- Python 3.12+
- [Sway](https://swaywm.org/)
- [Kitty](https://sw.kovidgoyal.net/kitty/)
- [Neovim](https://neovim.io/)

`hop` is designed for Linux desktop workflows that already use Sway.

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

Run `hop` in a project directory. This creates a dedicated sway workspace with a terminal. Running `hop` from the same directory any other terminal on any other workspace will teleport you to the session workspace.

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

The CLI command exists, but browser integration is not implemented yet.

## Development

Run tests with:

```bash
uv run pytest -q
```

If you are not using `uv`, install the package in editable mode and run:

```bash
pytest -q
```
