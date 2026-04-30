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

### Session backend

Each session has a **backend** that decides *where* its shells and editor run. The default is **host** (shells spawned by kitty as the user's default shell, neovim running on the host). Non-host backends are user-defined in `~/.config/hop/config.toml` and/or a project's `.hop.toml` — both files use the same `[backends.<name>]` schema and are merged at session entry. Each backend is a set of command-list templates hop runs at the appropriate lifecycle points:

- `prepare` (optional) — argv hop runs once before bootstrapping kitty. Idempotent.
- `shell` (required) — argv kitty uses to spawn each role terminal.
- `editor` (required) — argv kitty uses to launch the shared neovim. Hop substitutes `{listen_addr}` with a host-visible socket path the host's `nvim --server <socket> --remote-…` calls can reach.
- `teardown` (optional) — argv hop runs at `hop kill` after closing windows. Closing kitty windows first sends SIGHUP to in-backend shells so they exit cleanly before any teardown command runs.
- `workspace` (optional) — argv whose stripped stdout is the in-backend path that maps to the host project root. Captured once at session creation and used for cwd translation in the kitten dispatch.
- `port_translate` (optional) — argv hop runs lazily when the kitten dispatch resolves a URL whose host is `localhost`, `127.0.0.1`, or `0.0.0.0`. Stripped stdout is the host-reachable port number that should replace the URL's port. Hop substitutes `{port}` with the URL's original port (or empty string when the URL has no port).
- `host_translate` (optional) — argv hop runs lazily for the same set of localhost URLs. Stripped stdout is the hostname that should replace `localhost` / `127.0.0.1` / `0.0.0.0` in the URL. Both `port_translate` and `host_translate` are independently optional; either or both may be configured.

Substitution placeholders supported inside any command list: `{listen_addr}` (only meaningful in `editor`), `{project_root}`. `{port}` is additionally available inside `port_translate` and `host_translate` (the URL's original port, or empty string when absent).

Backend selection at session creation:

1. `hop --backend <name>` on the bare `hop` entry pins the named backend (or `"host"` to opt out). Pinning a name that isn't configured (and isn't `"host"`) raises `UnknownBackendError`.
2. Otherwise auto-detect walks `[backends.<name>]` tables in global-config declaration order, running each backend's `default` command in the project root; the first that exits 0 wins. Backends without a `default` command are skipped during auto-detect.
3. Fall back to **host** when nothing matches.

Project config at `<project_root>/.hop.toml` uses the **same `[backends.<name>]` schema** as the global file. Hop merges both files when resolving the session backend: project entries come first in auto-detect order; same-named entries are field-merged with project fields winning; the merged entry takes the project's slot in the order. A backend whose merged fields lack `shell` or `editor` is unusable and dropped silently. The reserved name `"host"` cannot be defined in either file.

Overriding `default` in a project file changes which backend wins auto-detect: a `["true"]` override forces this backend to match; a `["false"]` override skips it. There is no separate "pin a backend" knob in the project file — `default` overrides cover both selection and exclusion.

The name `"host"` is reserved for the implicit fallback. An explicit `[backends.host]` table in either config file is ignored.

The chosen backend (resolved commands + discovered workspace path) is persisted in `${XDG_RUNTIME_DIR}/hop/sessions/<name>.json` at session bootstrap. Every subsequent command against that session reads the persisted backend — auto-detect is not re-run mid-session, so global-config edits don't change a live session.

For backends with a shared filesystem and a discovered workspace path, hop translates terminal cwds (e.g. `/workspace/...` for a devcontainer) back to host paths whenever the kitten dispatch resolves a file target from visible terminal output. The translation is a backend method, so the rest of the codebase (kitten, target resolver, commands) is backend-agnostic.

For URL targets, hop applies the same backend-method indirection: if the dispatched URL's host is `localhost`, `127.0.0.1`, or `0.0.0.0`, the backend rewrites it via `host_translate` (replacing the host) and/or `port_translate` (replacing the port) before the URL reaches the session browser. This keeps `http://localhost:3000` printed inside a container's network namespace from being handed to the host browser unchanged. URLs whose host is not one of those three sentinels pass through untouched.

The kitty per-session socket is a filesystem socket at `${XDG_RUNTIME_DIR}/hop/kitty-<session>.sock`. Linux abstract-namespace sockets (`unix:@…`) would not be reachable from inside a container's network namespace, so a filesystem socket is used even for host-backend sessions for forward-compatibility with future in-container hop callers.

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

When the focused Sway workspace already matches the cwd-derived session's workspace (`p:<session>`), `hop` switches to the *spawn-additional-terminal* mode instead:

- use the current working directory as the session root (same rule as above)
- do not switch workspaces — by definition we're already on the right one
- pick the next free role of the form `shell-<N>` (starting from `shell-2`) so the new window is distinct from the canonical `shell` and from any other ad-hoc shells already open
- create a new Kitty role terminal with that role

This makes "give me another shell in this session" a single keystroke (`hop`) from any session terminal. The signal is the focused workspace, not env vars — so the same behavior is available to a Sway keybinding that runs `cd <project_root> && hop term`.

---

## Kitty process model

Each hop session corresponds to **one dedicated Kitty process**, listening on a deterministic Unix socket address derived from the session name: `unix:@hop-<session>`. The first hop command that needs a window for a session (typically bare `hop` — entering the session) bootstraps that Kitty process via `kitty --listen-on=… --directory=…` with hop's environment variables set. Every subsequent role-window for the session (`hop term --role server`, `hop run`, the `shell-N` ad-hoc spawns) goes via `kitty @ launch --type=os-window` to that same socket.

Consequences:

- Killing the session's Kitty process tears down all of its windows in one go.
- Hop is reachable from outside any Kitty terminal as long as the session's Kitty is up — its socket address is computable from the session name, no `KITTY_LISTEN_ON` env plumbing required.
- Different sessions are isolated at the Kitty-process level, not just by workspace.

## Window tagging

Hop tags one piece of metadata on each Kitty role window: the **role**, stored as the `hop_role` user var. Kitty OS window names are session-agnostic (`hop:<role>`, e.g. `hop:shell`, `hop:editor`) — they do not include the session name, so external tools that read Sway's `app_id` only see the role. Per-session identification of hop-managed Sway windows (browser, editor) happens through Sway marks of the form `_hop_<role>:<session>` (leading underscore so Sway hides them from window titles). No `HOP_*` environment variables are exported into role terminals — external tools should consume `hop list --json` to recover session-name → project-root mapping rather than reading shell env. Kitty session and project-root identity live entirely in (a) the per-session Kitty socket address, and (b) the per-session state files.

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
hop list --json
```

Behavior:

- discover live Sway workspaces whose names start with `p:`
- without `--json`: print session names without the `p:` prefix, one per line, alphabetical
- with `--json`: print a JSON array of records `{name, workspace, project_root}`. `project_root` comes from per-session state files written at bootstrap (`${XDG_RUNTIME_DIR}/hop/sessions/<name>.json`) and is `null` when no record exists (e.g. for workspaces created outside hop). This is the stable machine-readable API external tools should consume — not the kitty user_vars or shell env vars.

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

`hop term` invoked without `--role` is an alias for bare `hop` — same env-driven branching: spawns a new `shell-<N>` terminal when run from inside a session, otherwise enters the session.

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
- the editor window is rediscovered through its `_hop_editor:<session>` Sway mark (set on first sighting via `app_id == hop:editor` on `p:<session>`) so repeated `hop edit` calls — and kitten dispatches into the editor — focus the same OS window via Sway and switch to its workspace when needed, even if the user dragged the editor onto a different workspace
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
