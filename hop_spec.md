# hop — Specification

## Goal

Build a CLI tool `hop` that replaces tmux as the development workflow manager by using:

- Sway workspaces as sessions
- Kitty windows as terminals
- a single shared Neovim instance per session

The system must preserve and improve the current tmux-based workflow, including:

- project-based sessions
- fast switching
- opening file references from terminal output into Neovim
- sending commands (e.g. tests) from Neovim to specific terminal windows
- multiple dedicated terminals per project

---

## Core concepts

### Session

A session corresponds to a project.

A session consists of:

- a project root directory
- a dedicated Sway workspace
- one Neovim instance
- multiple terminal windows (each with a role)
- optionally, a browser window

Session name is derived from the project root directory name.

Workspace naming:

`p:<session_name>`

---

### Terminal roles

Each session contains multiple terminal windows.

Each terminal has a **role name**, for example:

- shell
- test
- server
- console

Roles are used to:

- identify windows
- focus existing windows
- create new windows when needed
- route commands to the correct terminal
- set window titles

---

### Neovim (session editor)

Each session has exactly one Neovim instance.

- it is shared across the session
- all file opening actions target this instance
- it is not tied to any specific terminal window

---

### Browser (session-scoped)

Each session may have a browser window.

- browser usage is scoped to the current session
- opening URLs should reuse or create a browser window within the session workspace

---

## CLI behavior

### Enter session

```bash
hop
```

From inside a project directory:

- determine project root
- derive session name
- switch to that session (workspace)
- create it if it does not exist
- ensure at least one terminal window exists (role `shell`)
- reuse the existing `shell` terminal when it already exists

---

### Switch session

```bash
hop switch <session>
```

Behavior:

- focus workspace `p:<session>`
- create that workspace if it does not exist yet

---

### List sessions

```bash
hop list
```

Behavior:

- discover live Sway workspaces whose names start with `p:`
- print session names without the `p:` prefix
- print one session per line in alphabetical order

---

### Open editor

```bash
hop edit
```

- ensure session Neovim is running
- focus it

---

### Open target in editor

```bash
hop edit <target>
```

Examples:

```bash
hop edit app/models/user.rb
hop edit app/models/user.rb:42
```

Behavior:

- ensure Neovim exists
- open the target in that instance

---

### Open or focus terminal

```bash
hop term --role <name>
```

Examples:

```bash
hop term --role shell
hop term --role test
```

Behavior:

- if a terminal with that role exists → focus it
- otherwise → create it

---

### Send command to terminal

```bash
hop run --role <name> "<command>"
```

Examples:

```bash
hop run --role test "bundle exec rails test"
hop run "ls"
```

Behavior:

- find terminal with given role
- if missing → create it
- send command to that terminal

Default role: `shell`

---

### Open browser

```bash
hop browser [url]
```

Behavior:

- reuse or create a browser window associated with the session
- open URL if provided

---

## Opening links from terminal output

A key requirement is:

> from any terminal window, select a file reference or URL from visible output and open it in the appropriate place

This replaces the current tmux + `tmux_super_fingers` workflow.

---

### Interaction model

- user triggers interactive selection over visible terminal output
- user selects a match (file path, file:line, URL, etc.)
- system resolves the match in the context of the session
- system dispatches the result:
  - file → open in Neovim
  - file:line → open in Neovim at line
  - URL → open in browser

---

### Resolution rules (file targets)

When a file-like target is selected:

1. if absolute path → use directly
2. else try resolving relative to the terminal window’s current working directory
3. if not found, try resolving relative to the project root
4. if still not found → ignore

---

### Supported patterns

At minimum, support:

- file paths
- file paths with line numbers
- git diff paths (`a/...`, `b/...`)
- URLs
- Rails-style references:
  - `Processing UsersController#index`

---

## Kitty integration

Kitty is used as the terminal backend.

### Selection (hints)

- interactive selection must work over **visible terminal output**
- selection should allow choosing file paths, URLs, and other matches
- this replaces `tmux_super_fingers`

### Window control

Kitty must be used to:

- identify terminal windows
- distinguish them by session and role
- focus specific windows
- send commands to specific windows

All this means using kittens api (custom kittens).

---

## Command routing (Neovim → terminal)

A key workflow requirement:

> from Neovim, send commands to a specific terminal window (e.g. run tests)

This replaces current `vigun` + tmux behavior.

---

### Vigun integration

The existing Neovim plugin **vigun** must be extended to work with `hop`.

It must be able to:

- send commands (e.g. test runs) to a specific terminal role via `hop`
- typically target role `test`

Example flow:

- user triggers test run inside Neovim
- vigun calls:

```bash
hop run --role test "<test command>"
```

- command appears and runs in the `test` terminal window

Changing vigun is outside of the scope of hop, but we need to have a contract document that can be then used to upgrade vigun.

---

## Neovim lifecycle

- Neovim is started when needed (e.g. via `hop edit`)
- if Neovim is closed (`:qa`), it can be recreated by:

```bash
hop edit
```

---

## Window identification

Each window must be identifiable by:

- session (project)
- role (for terminals)

This enables:

- focusing existing windows
- avoiding duplicates
- routing commands correctly

---

## Behavior guarantees

- all commands are idempotent:
  - no duplicate windows when re-running commands
- windows are reused whenever possible
- missing components are created automatically
- system works entirely with OS windows (no multiplexing layer)

---

## Final summary

`hop` is a session-oriented CLI tool where each project maps to a Sway workspace containing:

- one shared Neovim instance
- multiple named terminal windows
- a session-scoped browser

and provides:

- fast session switching
- opening file references from terminal output into Neovim via interactive selection
- routing commands (e.g. tests) from Neovim to specific terminal windows (via vigun)
- a tmux-free workflow built on Sway and Kitty
