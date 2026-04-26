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

A session corresponds to the directory where `hop` is invoked.

A session consists of:

- a session root directory
- a dedicated Sway workspace
- one Neovim instance
- multiple terminal windows (each with a role)
- optionally, a browser window

The session root is exactly the caller's current working directory when `hop` starts. `hop` must not walk ancestors looking for `.git`, `.dust`, `pyproject.toml`, or any other marker file.

- rerunning `hop` from the same directory should attach to the same session
- running `hop` from a different directory should target a different session rooted there

Session name is derived from the session root directory name.

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
- `hop` reuses the user's default browser windowing model instead of a dedicated profile
- the session browser is rediscovered through a session-specific Sway mark rather than by visible title alone
- if the session browser drifts to another workspace, `hop browser` reattaches it to the session workspace instead of adopting a different browser window
- opening URLs should reuse or create a browser window within the session workspace

---

## CLI behavior

### Enter session

```bash
hop
```

From inside the directory you want to treat as the session root:

- use the current working directory as the session root
- derive session name
- switch to that session (workspace)
- attach to the existing session for that directory or create it if it does not exist yet
- ensure at least one terminal window exists (role `shell`)
- reuse the existing `shell` terminal when it already exists

When `hop` is invoked from inside a session terminal (detected via the `HOP_SESSION` environment variable that hop exports into every terminal it creates), it switches to the *spawn-additional-terminal* mode instead:

- use the current working directory as the session root (same rule as above)
- do not switch workspaces — the caller is already on `p:<session>` by construction
- pick the next free role of the form `shell-<N>` (starting from `shell-2`) so the new window is distinct from the canonical `shell` and from any other ad-hoc shells already open
- create a new Kitty role terminal with that role

This makes "give me another shell in this session" a single keystroke (`hop`) from any session terminal.

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
- recreate it cleanly if the previous editor was closed with `:qa`
- reuse the existing session editor instead of creating duplicates

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
- focus the shared session editor window
- open the target in that instance
- when the target is `path:line`, jump to that line after opening the file

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
- terminal lookup is keyed by stable Kitty metadata for the session and role, not by ad hoc window IDs
- `hop term`, `hop edit`, `hop run`, and `hop browser` do **not** switch Sway workspaces — they assume the caller is already on `p:<session>` (which is true when the command is invoked from any of that session's terminals). Use bare `hop` or `hop switch` to enter a session's workspace.

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

- use the caller's current working directory as the session root
- find terminal with given role
- if missing → create it
- send the exact `<command>` string followed by a trailing newline to that terminal
- default behavior keeps the current focus while routing the command into the target role terminal
- print a fresh **run id** to stdout and return; `hop run` does not wait for the dispatched command to finish or proxy its exit status
- the run id is opaque to callers and is the input to `hop tail`
- `hop run` does not switch Sway workspaces — the caller is expected to already be in the session's workspace (the canonical entry points for that are bare `hop` and `hop switch`)

Default role: `shell`

External callers that want a stable test runner target should call `hop run --role test "<command>"`.

The `<command>` value is a single CLI argument, so shell callers must quote it.

---

### Tail command output

```bash
hop tail <run-id>
```

Behavior:

- look up the dispatch state persisted by the matching `hop run` invocation
- block until the dispatched command has returned to its shell prompt
- write the captured combined output of that command to stdout and exit
- `hop tail` exits 0 on successful delivery; it does not propagate the inner command's exit status
- detection relies on Kitty's shell integration (OSC 133 prompt boundaries) for the role terminal; `hop tail` requires the shell in the role terminal to support it

The intended consumer is `vigun`, which dispatches with `hop run` and then streams the output via `hop tail <id>`.

---

### Open browser

```bash
hop browser [url]
```

Behavior:

- reuse or create a browser window associated with the session
- launch the system default browser in a new window when the session browser is missing
- keep the browser associated with the session through a stable Sway mark
- reattach the session browser to the session workspace when it has drifted elsewhere
- if a URL is provided, focus the session browser first and then delegate that URL to the default browser

---

### Kill session

```bash
hop kill
```

Behavior:

- resolve the session from the caller's exact current working directory
- discover all windows hop owns for that session: Kitty role terminals, the shared editor window, and the marked session browser (even if it has drifted to another workspace)
- close all discovered managed windows
- remove the session workspace if it still exists after teardown
- do not close windows on the workspace that hop did not create
- focus after teardown is left to Sway

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
3. if not found, try resolving relative to the session root
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
- selection is implemented through Kitty's `hints` UI with custom processing in `kittens/open_selection/main.py`
- selection should allow choosing file paths, URLs, and other matches
- this replaces `tmux_super_fingers`

### Window control

Kitty must be used to:

- identify terminal windows
- distinguish them by session and role
- focus specific windows
- send commands to specific windows

This control path should use Kitty-native remote control surfaces such as the remote control protocol, Python APIs, or kittens rather than shelling out to `kitty @`.

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
- the shared editor is addressed through a deterministic per-session remote server address
- the editor window is rediscovered through stable Kitty metadata so repeated `hop edit` calls focus the same OS window
- if Neovim is closed (`:qa`), it can be recreated by:

```bash
hop edit
```

---

## Window identification

Each window must be identifiable by:

- session (project)
- role (for terminals)

For Kitty terminals this identity must be encoded as stable metadata on the window itself, so repeated `hop term` and `hop run` calls can rediscover the same role window exactly.

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
