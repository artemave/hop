# hop — Specification

## Goal

Build a CLI tool `hop` that replaces tmux as the development workflow manager by using:

- Sway workspaces as sessions
- Kitty windows as terminals
- a single shared Neovim instance per session

The system must preserve and improve the current tmux-based workflow, including:

- directory-based sessions
- fast switching
- opening file references from terminal output into Neovim
- sending commands (e.g. tests) from Neovim to specific terminal windows
- multiple dedicated terminals per session

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

Each session has a **backend** that decides *where* its windows run. The default is **host** (shells spawned by kitty as the user's default shell, neovim running on the host). Non-host backends are user-defined in `~/.config/hop/config.toml` and/or a project's `.hop.toml` — both files use the same schema and are merged at session entry. Each backend's command fields are shell command strings hop runs through `sh -c` after substituting placeholders:

- `prepare` (optional) — command(s) hop runs once before bootstrapping kitty. Idempotent.
- `teardown` (optional) — command(s) hop runs at `hop kill` after closing windows. Closing kitty windows first sends SIGHUP to in-backend shells so they exit cleanly before any teardown command runs.
- `port_translate` (optional) — command(s) hop runs lazily when the kitten dispatch resolves a URL whose host is `localhost`, `127.0.0.1`, or `0.0.0.0`. Stripped stdout is the host-reachable port number that should replace the URL's port. Hop substitutes `{port}` with the URL's original port (or empty string when the URL has no port).
- `host_translate` (optional) — command(s) hop runs lazily for the same set of localhost URLs. Stripped stdout is the hostname that should replace `localhost` / `127.0.0.1` / `0.0.0.0` in the URL. Both `port_translate` and `host_translate` are independently optional; either or both may be configured.
- `interactive_prefix` (required) — shell snippet hop prepends to every window's command launched in this backend's environment (e.g. `podman-compose -f docker-compose.dev.yml exec devcontainer`). Empty for the built-in `host` backend.
- `noninteractive_prefix` (required) — prefix hop uses for non-interactive backend operations like the file-existence check that drives the open-selection kitten's highlight filter. Backends that allocate a TTY by default (e.g. `podman-compose exec`) must set this to the no-TTY variant (`podman-compose exec -T <service>`); backends that don't (ssh) pass the same string as `interactive_prefix`. Empty for the built-in `host` backend.

Hop ships an implicit `host` backend (`interactive_prefix = ""`, `noninteractive_prefix = ""`, `activate = "true"`) layered below user config. Users can override any field by declaring `[backends.host]` in either config file — `host` is not a reserved name.

The four lifecycle and translate fields (`prepare`, `teardown`, `port_translate`, `host_translate`) accept either a single string or an array of strings. The array form runs each element as its own `sh -c` invocation in declaration order; for `prepare` and `teardown` the sequence aborts on the first non-zero exit (the popup's held shell shows the failing step), and for the translate fields the **last** element's stripped stdout is the translated value (earlier elements run for their side effects). Single-string and one-element-array forms are equivalent. Empty arrays are rejected at parse time so omission stays the only "unset" signal.

`interactive_prefix` and `noninteractive_prefix` remain string-only — they are wraps, not sequences. Use a triple-quoted string for multi-line pipelines.

All commands are run through a **transport** that turns the substituted command string into argv. For a local session the transport is `sh -c <substituted-string>`, so pipes, redirects, and `$(...)` are part of the contract. For a remote session (see *Remote sessions*) the same string is wrapped to run on the remote host over ssh — the recipe is identical either way; the ssh is hop's, never in `.hop.toml`. Substitution placeholders supported inside any command: `{session_root}` and `{host}` (the session's externally-reachable host — the ssh target remotely, `localhost` locally). `{port}` is additionally available inside `port_translate` and `host_translate` (the URL's original port, or empty string when absent). Substituted values are shell-quoted before insertion so paths with spaces or shell metacharacters round-trip safely.

For a **non-host** backend (a non-empty `interactive_prefix`), hop launches the session shell as a **login** shell: the interactive command is base64-encoded behind a fixed `exec "$SHELL" -lc "$(… base64 -d)"` wrapper inside the prefix — the container analogue of the ssh transport's remote login shell — so it sources the user's login profile in the container, parity with the host's native login shell. Role commands are typed into that shell and inherit its environment, so tool-managed `$PATH` (mise/asdf/direnv/…) needs no per-command wrapping. `$SHELL` has no `:-` fallback: the wrapper's `sh` sets it from the backend user's passwd, and an image where it's absent fails loudly. The noninteractive path (`paths_exist` / `read_file` / translate probes) is *not* login-wrapped — it runs bare via `noninteractive_prefix`.

### Remote sessions

A session can run on a remote machine reached over ssh. The same project `.hop.toml` drives it — there is **no second config and no ssh in the recipe**. The prefixes (`podman-compose … exec devcontainer`, etc.) are byte-identical to the local case; hop wraps every composed command (window launches and the runner-mediated `prepare`/`teardown`/`paths_exist`/`read_file`/translate/`activate` calls) in an outer `ssh <host> '<cmd>'` keyed off the session's host. The ssh layer is intrinsic: a kitty window is a host GUI surface, so a remote shell inside it requires an ssh client as the window's child — hop builds that, the user never writes it.

A remote session needs **no local directory and no local `.hop.toml`**. It is a session record carrying `(name, host, remote_cwd)`: the name and `p:<name>` workspace come from the remote directory's basename, the `.hop.toml` is fetched from the remote on demand (not read locally), and kitty windows open in the user's home (their child immediately `ssh`'s out). `{session_root}` and the transport's `cd` use the remote path string, which is never touched as a local filesystem path.

The transport reuses one ssh ControlMaster per host (`ControlMaster=auto` + `ControlPersist`), so a session survives a laptop-sleep / connection drop and redials lazily on the next command. The composed command is base64-encoded behind a fixed decode wrapper so ssh's argv-flattening can't corrupt it and stdin stays free for piped data (the `paths_exist`/`read_file` script-over-stdin path); the decoded command runs under a remote login shell so the remote user's normal PATH resolves with no extra config. See *Remote session setup (`hop ssh`)* for how a session is created.

### Shell integration

Kitty's shell integration (OSC 133 prompt marks) is what `hop tail` and other prompt-aware features depend on. hop enables it implicitly per backend — there is no shell-role config to write:

- **In-place local host** (no `interactive_prefix`, no ssh host): the shell is kitty's direct child, which kitty integrates natively. The implicit shell is empty, so `wrap("")` returns empty argv and kitty spawns the user's login shell.
- **Every other backend** (a container, or a shell over ssh): kitty's integration env doesn't cross the `podman exec` / ssh boundary, so the implicit shell is a snippet that runs `kitten run-shell` when `kitten` is on the backend's PATH, and otherwise opens a plain shell after printing a one-line "integration off" warning to stderr. The check and the degrade live inside the launched shell (no bootstrap probe), and the login-wrap runs the snippet under `$SHELL -lc`, so even the degraded shell has the login environment.

Making `kitten` available is the user's step: an install command in the backend's `prepare` for a container, or — for a remote *host* — `hop ssh`, which best-effort-copies the host's own `kitten` binary onto the remote (a portable kitty release runs on any same-arch glibc target; a musl or cross-arch remote just falls through to the warning). An explicit `[windows.shell].command` overrides the implicit shell on any backend.

### Workspace layout

Top-level `workspace_layout` (string, optional) — sway workspace layout mode hop applies to a session's workspace at first entry. Accepts only `splith`, `splitv`, `stacking`, `tabbed`. Omitted ⇒ sway's default layout. Re-entry from another workspace does not re-apply it (the user may have changed the layout deliberately during the session).

```toml
workspace_layout = "tabbed"
```

### Layouts and top-level windows

Per-role launch commands live outside the backend, in two top-level config sections:

- `[layouts.<name>]` — a named layout with one required `activate` shell-snippet probe and a list of `[layouts.<name>.windows.<role>]` declarations. When the probe exits 0 in the session root, all of the layout's windows are queued for activation. Multiple layouts can match in the same session; their windows compose.
- `[windows.<role>]` — top-level windows, outside any layout. Always active unless individually opted out via `activate = "false"`.

Each window declaration carries:

- `command` (string) — the role command. **No backend wrap inside this string** — the active backend's `interactive_prefix` is prepended at launch time. For built-in roles, hop ships a default; the user can override. Every terminal role's window is launched as the session shell, resolved as: an explicit `[windows.shell].command` override → the backend's implicit integration shell → empty. The implicit integration shell is empty for the in-place local host (kitty spawns and integrates its native login shell, so `wrap("")` returns empty argv), and a `kitten run-shell`-or-degrade snippet for any non-host backend (see *Shell integration*). The role's own command, if non-empty, is then typed into that shell via kitty `send-text` — so it lands in shell history, runs with the interactive shell's environment (no per-command wrapping, shell syntax works as typed), and leaves a usable shell when it exits. A non-shell role with `command = ""` (e.g. `[layouts.rails.windows.test]`) is just that bare shell with nothing typed in.
- `activate` (`"true"` or `"false"`, optional) — opt-in / opt-out only. No probe at the per-window level; the gate is whatever the window's container decides:
  - **Built-in window** (shell / editor / browser) with no top-level override: hop's default (active for shell/editor, inactive for browser).
  - **Top-level window**: defaults active unless the entry sets `activate = "false"`.
  - **Layout window**: gated by the layout's `activate` probe; the window can carry `activate = "false"` to opt out of even the matched layout (declared but not auto-launched).

Built-in defaults:

| role     | command default                      | activate default | runtime adapter         |
|----------|--------------------------------------|------------------|-------------------------|
| shell    | `""` (kitty's platform default shell on host; `${SHELL:-sh}` falls back inside a `interactive_prefix`) | active | kitty terminal |
| editor   | `nvim`                               | active           | shared nvim adapter     |
| browser  | xdg-detected default browser         | inactive         | session browser adapter |

For user-defined roles (anything other than shell / editor / browser), top-level windows default to active (the always-on rule above). To declare a user role for `hop term --role <name>` without auto-launching it on entry, either set `activate = "false"` on the top-level entry, or move it into a layout whose probe is the gate.

Window resolution at session entry, layered with later sources overriding earlier ones for the same role:

1. Built-in defaults (shell, editor, browser).
2. Each layout in declaration order whose `activate` probe exits 0, contributing its windows in declaration order.
3. Top-level `[windows.<role>]` entries in declaration order.

On first session entry, hop ensures the shell window unconditionally (regardless of activate), then dispatches every remaining active window to its runtime adapter (editor → nvim, browser → session browser, everything else → kitty terminal). Re-entry from another workspace ensures only the shell window — the activation sweep never re-fires for a still-live session.

Backend selection (below) is independent of layouts: any session's backend wraps every layout/top-level window's command through `interactive_prefix`.

Backend selection at session creation:

1. `hop --backend <name>` on the bare `hop` entry pins the named backend (or `"host"` to opt out). Pinning a name that isn't configured (and isn't `"host"`) raises `UnknownBackendError`.
2. Otherwise auto-detect walks `[backends.<name>]` tables in global-config declaration order, running each backend's `activate` command in the session root; the first that exits 0 wins. Backends without an `activate` command are skipped during auto-detect.
3. Fall back to **host** when nothing matches.

Project config at `<session_root>/.hop.toml` uses the **same `[backends.<name>]` schema** as the global file. Hop merges both files when resolving the session backend: project entries come first in auto-detect order; same-named entries are field-merged with project fields winning; the merged entry takes the project's slot in the order. A backend whose merged fields lack `shell` or `editor` is unusable and dropped silently. The reserved name `"host"` cannot be defined in either file.

Overriding `activate` in a project file changes which backend wins auto-detect: a `"true"` override forces this backend to match; a `"false"` override skips it. There is no separate "pin a backend" knob in the project file — `activate` overrides cover both selection and exclusion.

The name `"host"` is reserved for the implicit fallback. An explicit `[backends.host]` table in either config file is ignored.

The chosen backend (resolved commands + both prefixes) is persisted in `${XDG_RUNTIME_DIR}/hop/sessions/<name>.json` at session bootstrap. Every subsequent command against that session reads the persisted backend — auto-detect is not re-run mid-session, so global-config edits don't change a live session.

For file targets resolved by the open-selection kitten, hop consults the focused session's backend through `hop.focused.paths_exist`: relative candidates are resolved against the focused window's in-shell cwd (from kitty's per-session socket, via OSC 7), then `backend.paths_exist` runs a single shell loop inside the backend (wrapping `<noninteractive_prefix> sh -c '<while-read>'`) and reports which paths exist. The kitten itself owns no session, backend, or IPC awareness — it just calls `paths_exist` and yields marks for the survivors.

For URL targets, hop applies a backend-method indirection: if the dispatched URL's host is `localhost`, `127.0.0.1`, or `0.0.0.0`, the backend rewrites it via `host_translate` (replacing the host) and/or `port_translate` (replacing the port) before the URL reaches the session browser. This keeps `http://localhost:3000` printed inside a container's network namespace from being handed to the host browser unchanged. URLs whose host is not one of those three sentinels pass through untouched.

The kitty per-session socket is a filesystem socket at `${XDG_RUNTIME_DIR}/hop/kitty-<session>.sock`. Linux abstract-namespace sockets (`unix:@…`) would not be reachable from inside a container's network namespace, so a filesystem socket is used even for host-backend sessions for forward-compatibility with future in-container hop callers.

### Browser (session-scoped)

Each session may have a browser window.

- browser usage is scoped to the current session
- `hop` reuses the user's default browser windowing model instead of a dedicated profile
- the session browser is rediscovered through a session-specific Sway mark rather than by visible title alone
- when no marked window exists, an unclaimed browser window already on `p:<session>` is promoted to the session browser (marked and reused) instead of launching a second one. A window counts as a browser window when its `app_id`/`class` matches the browser's window identifiers *or* its pid's executable matches the launch command — neither signal alone covers both wrapper-script launchers and desktop entries without a `StartupWMClass`. Windows carrying any session's browser mark are never promoted. Browser windows on other workspaces are the user's and are left alone
- raw Sway moves of the browser off `p:<session>` clear that mark — `hopd` reconciles marks against current placement on every Sway `window` event. The window stops being the session's browser, and the next `hop browser` launches a fresh one
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

This makes "give me another shell in this session" a single keystroke (`hop`) from any session terminal. The signal is the focused workspace, not env vars — so the same behavior is available to a Sway keybinding that runs `cd <session_root> && hop term`.

When `hop` is invoked without a controlling TTY (e.g. from vicinae's detached `setsid -f hop`, a sway keybinding, or a launcher script), the first-entry path shows a `kitten panel` overlay (`app_id="hop:popup"`) streaming the backend's `prepare` output while the session is being created. Sway is switched to `p:<session>` *before* the popup runs so the user lands on the session-to-be while prepare streams. On prepare failure the panel stays open at a held shell so the user can read the error; on success it closes and the normal kitty / editor bootstrap proceeds. From an interactive terminal, prepare output streams to that terminal as today.

---

## Kitty process model

Each hop session corresponds to **one dedicated Kitty process**, listening on a deterministic Unix socket address derived from the session name: `unix:@hop-<session>`. The first hop command that needs a window for a session (typically bare `hop` — entering the session) bootstraps that Kitty process via `kitty --listen-on=… --directory=…` with hop's environment variables set. Every subsequent role-window for the session (`hop term --role server`, `hop run`, the `shell-N` ad-hoc spawns) goes via `kitty @ launch --type=os-window` to that same socket.

Consequences:

- Killing the session's Kitty process tears down all of its windows in one go.
- Hop is reachable from outside any Kitty terminal as long as the session's Kitty is up — its socket address is computable from the session name, no `KITTY_LISTEN_ON` env plumbing required.
- Different sessions are isolated at the Kitty-process level, not just by workspace.

## Window tagging

Hop tags one piece of metadata on each Kitty role window: the **role**, stored as the `hop_role` user var. Kitty OS window names are session-agnostic (`hop:<role>`, e.g. `hop:shell`, `hop:editor`) — they do not include the session name, so external tools that read Sway's `app_id` only see the role. Per-session identification of hop-managed Sway windows (browser, editor) happens through Sway marks of the form `_hop_<role>:<session>` (leading underscore so Sway hides them from window titles). No `HOP_*` environment variables are exported into role terminals — external tools should consume `hop list --json` to recover session-name → session-root mapping rather than reading shell env. Kitty session and session-root identity live entirely in (a) the per-session Kitty socket address, and (b) the per-session state files.

---

### Switch session

```bash
hop switch <session>
```

Behavior:

- focus workspace `p:<session>`
- create that workspace if it does not exist yet

---

### Move window to session

```bash
hop move <session>
```

Behavior:

- move the currently-focused Sway window onto the named session's `p:<session>` workspace
- switch the user's view to `p:<session>` after the move, so the moved window is visible at its destination
- error if no session named `<session>` is live, or if no window is focused

---

### List sessions

```bash
hop list
hop list --json
```

Behavior:

- discover live Sway workspaces whose names start with `p:`
- without `--json`: print session names without the `p:` prefix, one per line, alphabetical
- with `--json`: print a JSON array of records `{name, workspace, session_root}`. `session_root` comes from per-session state files written at bootstrap (`${XDG_RUNTIME_DIR}/hop/sessions/<name>.json`) and is `null` when no record exists (e.g. for workspaces created outside hop). This is the stable machine-readable API external tools should consume — not the kitty user_vars or shell env vars.

---

### List declared windows

```bash
hop windows
```

Behavior:

- resolve the session from the caller's current working directory
- run the same window resolver that bootstrap uses (built-in defaults + active layouts + top-level windows), evaluating layout `activate` probes against the session root
- print each resolved role on its own line, in resolution order (built-ins, then active-layout windows, then top-level windows)

Intended for launchers (rofi, fuzzel) to enumerate the focusable / launchable windows for the focused session workspace and dispatch to `hop browser` (browser) or `hop term --role <name>` (every role including `editor`). The vicinae integration consumes the same resolver via the `hopd` daemon (see "Vicinae integration daemon" below) — it does not invoke `hop windows` directly because it has the resolver in-process.

---

### Open target

```bash
hop open <target>
```

Examples:

```bash
hop open app/models/user.rb
hop open app/models/user.rb:42
hop open UsersController#index
hop open https://example.com/foo
```

Behavior — the target is parsed by the same resolver the open-selection kitten uses, so URLs, Rails `Controller#action` refs, and `path[:line]` shapes all dispatch the same way from CLI as from a kitten hint:

- URL → routed to the session browser (with the backend's localhost translation applied)
- binary file → opened on the host with `xdg-open`. Classification is by content, not extension: the backend runs `file --mime-encoding` against the file and anything that is not a text encoding is binary. The viewer always runs on the host, so when the file lives in a different filesystem namespace than the host — a local container or a remote ssh host — it is first copied to a host temp file (riding the same base64-over-transport path as `read_file`, so a file inside a container or on a remote comes across too) and `xdg-open` runs against that local copy. A file already on the host (the in-place `host` backend) is opened in place, no copy
- file or Rails ref → ensure Neovim exists, focus the shared session editor window, open the file (jumping to `:line` when present)

Text classifies to the editor — JSON, YAML, TOML, Markdown, SVG (ASCII text), source code, no-extension files. A missing file (so `hop open not-yet-created.rb` lands in an empty buffer) and an empty file also route to the editor. The backend must have the `file` command available for the probe.

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
- `hop term --role editor` focuses or launches the session's shared Neovim. The editor is a plain role terminal like every other role: `ensure_terminal` launches a shell and types `nvim` into it (or focuses the existing editor window). The singleton — one editor per session — comes from the per-role window mechanism, not a bespoke launch path.
- `hop term`, `hop open`, `hop run`, and `hop browser` do **not** switch Sway workspaces by default — they assume the caller is already on `p:<session>` (which is true when the command is invoked from any of that session's terminals). Use bare `hop` or `hop switch` to enter a session's workspace. `hop run --focus` is the one explicit opt-in that crosses workspaces, since asking to focus the role terminal is meaningless if the caller is somewhere else.

`hop term` invoked without `--role` is an alias for bare `hop` — same env-driven branching: spawns a new `shell-<N>` terminal when run from inside a session, otherwise enters the session.

---

### Send command to terminal

```bash
hop run [--focus] --role <name> "<command>"
```

Examples:

```bash
hop run --role test "bundle exec rails test"
hop run "ls"
hop run --role server --focus "bin/dev"
```

Behavior:

- use the caller's current working directory as the session root
- find terminal with given role
- if missing → create it
- send the exact `<command>` string followed by a trailing newline to that terminal
- default behavior keeps the current focus while routing the command into the target role terminal
- `--focus` opts in to focusing the role terminal: the role's Kitty window receives focus and Sway switches to the session's workspace, so the operator can dispatch and watch the role from any workspace in one step
- print a fresh **run id** to stdout and return; `hop run` does not wait for the dispatched command to finish or proxy its exit status
- the run id is opaque to callers and is the input to `hop tail`
- by default `hop run` does not switch Sway workspaces — the caller is expected to already be in the session's workspace (the canonical entry points for that are bare `hop` and `hop switch`); `--focus` is the explicit opt-in that does switch

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

Headless invocations (vicinae's `hop-kill` script, sway keybindings) show the same `kitten panel` overlay (`app_id="hop:popup"`) streaming `teardown` output after session windows have closed. Window-close ordering is unchanged — only the teardown step is wrapped. Teardown failure leaves the popup open at a held shell; the session's persisted state file is not removed (matching today's "teardown failure short-circuits forget" behavior). From an interactive terminal, teardown output streams to that terminal as today.

### Error display

Any `HopError` raised during a headless `hop` invocation also surfaces through a `kitten panel` overlay (same `app_id="hop:popup"`, title `Hop: error`) so the user sees what went wrong (`UnknownBackendError`, `SwayConnectionError`, "No active session named …", etc.). Errors already surfaced by a lifecycle popup (`SessionBackendError` with `surfaced_by_popup=True`) are not re-shown — exactly one panel per failure. From an interactive terminal, errors continue to print to stderr only.

---

## Vicinae integration daemon

```bash
hopd
```

Long-lived process, separate binary from `hop`. Subscribes to Sway IPC `workspace` events on `SWAYSOCK` and rewrites `~/.local/share/vicinae/scripts/hop-*` on every event.

Behavior:

- on startup: regenerate the script set once, then subscribe to `workspace` events.
- on every workspace event: regenerate the script set.
- on focused workspace `p:<session>`: emit `hop-window-<role>` per role from the same window resolver `hop windows` uses (built-ins + active layouts + top-level), `hop-kill` for the focused session, and `hop-switch-<other-session>` for every other live session.
- off any `p:*` workspace: emit only `hop-switch-<session>` per live session.
- always emit `hop-create` regardless of focused workspace. The script falls through to a second `vicinae dmenu` over directories under `$HOME` (with dot-dirs and well-known build noise pruned) and dispatches `cd <picked> && exec hop`, which creates a session if the directory has none or attaches if one is already running.
- own the `hop-*` filename namespace in the scripts directory: any `hop-*` file not in the target set is removed; any non-`hop-*` file is left untouched.
- on Sway IPC failure (refused subscription, dropped connection, malformed reply): print the error and exit non-zero.

Intended to be wired in sway config as `exec hopd` — *not* `exec_always`. The IPC subscription persists across sway config reloads, so a single instance covers the whole sway session; `exec_always` would spawn a duplicate on every reload. If hopd dies between sway sessions the user is responsible for relaunching it (`hopd &` from a terminal, or restart sway). No symlink-based install, no manual reload — vicinae's own `QFileSystemWatcher` picks up the changes within ~100 ms.

Activated entries in vicinae dispatch via `hop browser` (browser role), `hop term --role <name>` (every other role including `editor`), `hop switch <name>`, or `hop kill`. The activated `hop kill` script detaches via `setsid -f` so vicinae's UI-close SIGTERM does not interrupt teardown.

`hopd` also hosts the **bridge acceptor** (see next section) on a unix socket. The acceptor runs in a daemon thread alongside the Sway IPC subscription and shares `hopd`'s lifecycle — no extra `exec` line in sway config.

---

## Bridge acceptor

`hopd` listens on a per-user unix socket at `$XDG_RUNTIME_DIR/hop/api.sock` so editor plugins running inside non-host backends (devcontainer, ssh) can dispatch `hop` CLI calls back to the host. The host is always the authority — backends never carry hop state.

Wire protocol — HTTP/1.0 over `AF_UNIX`:

- Request: `POST /call` with the body as NUL-separated fields framed `host \0 cwd \0 $0 \0 *args`. `host` and `cwd` are the shim's baked ssh host (empty for the in-backend shim) and its `pwd` at call time; `$0` is the shim's own name and is ignored; the rest are forwarded to `hop`.
- Response on successful dispatch: `200 OK` with body equal to the `hop` subprocess's stdout, header `X-Hop-Exit: <integer>` carrying its exit code, and header `X-Hop-Stderr: <base64>` carrying its stderr (base64-encoded because HTTP headers are text-only).
- Response on acceptor-level failure: `400` (caller context — no focused session, malformed argv) or `500` (acceptor fault) with a plain-text `text/plain; charset=utf-8` body explaining the problem.

The acceptor dispatches by request shape:

- **Remote session entry** — a call carrying a non-empty `host` and *no* args (`hop` with no subcommand, from a `hop ssh`-installed shim). No session exists yet, so identity comes from the shim's `(host, cwd)`, not from focus: hop is run with `HOP_REMOTE_HOST` / `HOP_REMOTE_CWD` set and builds the remote `ProjectSession` from them.
- **Everything else** — session identity is resolved on the host from existing Sway state, not from the request. The acceptor queries Sway for the focused window: if its workspace name matches `p:<session>`, the suffix is the session name. This covers every kitty role terminal — shell, editor, test/server/console/… — since they all live on the session workspace, plus any other window inside a session workspace; they carry no per-window session identity in Sway, but the workspace tag is hop's canonical session-to-window mapping.

  The session record is then looked up in `$XDG_RUNTIME_DIR/hop/sessions/<name>.json`; the `hop` subprocess is spawned with `cwd` set to that session's `session_root`. Bridge calls from windows that are not on a `p:<session>` workspace are rejected with `400`.

Dispatch is via subprocess (`python -m hop <argv>`) per request. Output is buffered before the response is written; streaming is out of scope. The protocol is curl-compatible — `curl --unix-socket $XDG_RUNTIME_DIR/hop/api.sock --data-binary @- http://_/call < argv.nul` is sufficient to drive it from the host, which is also how the host-side test suite exercises it.

### Shim

A POSIX-sh client ships with hop and is printed by:

```bash
hop bridge shim [--socket PATH]
```

Backends install it into the backend's filesystem at the path `hop` (typically `/usr/local/bin/hop`); inside the backend it forwards argv to the host acceptor and demultiplexes the response into stdout, stderr, and an exit code. Required backend-side tools: `curl`, `awk`, `base64`, `tr`, `mktemp` — coreutils-universal or near-universal in dev container base images.

The shim's socket path is `${HOP_SOCKET:-<default>}`. `<default>` is `/run/hop.sock` unless overridden at print time via `hop bridge shim --socket <path>` — useful when a recipe already shares the host's `$XDG_RUNTIME_DIR` into the backend and wants the bridge socket to "just work" without changing the backend's environment. The shim's ssh host is likewise `${HOP_SSH_HOST:-<default>}`, baked empty by default and to the real host by `hop ssh` (see *Remote session setup*).

Per-backend recipes (compose volume mount for devcontainer; ssh `-R` for the ssh backend) along with the `prepare`-time shim install live in the recipe guides under `docs/`.

### Remote session setup (`hop ssh`)

```bash
hop ssh <host>
```

Sets up the ssh transport for remote sessions, then drops into a remote shell. It opens an ssh ControlMaster to `<host>`, reverse-forwards `hopd`'s bridge socket onto the remote, installs the `hop` shim on the remote's PATH (rendered with `<host>` and the forwarded socket baked in), and `exec`s an interactive login shell that reuses the master. It does *only* transport setup — no session is created and no container is touched. Requires `hopd` to be running (the reverse-forward's target); aborts with a clear error otherwise.

From that remote shell, `cd <project> && hop` creates the session: the installed shim reports `(host, cwd)` to `hopd`, which starts a remote session for that directory (see *Remote sessions* and the bridge *Remote session entry* path) with windows spawned on the host and driven over the same ssh connection.

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

## Editor lifecycle

The editor is whatever command the user configures on the `editor` role; nvim is the default. The open-file dispatch is editor-agnostic by construction: hop substitutes `{path}` and `{line}` into the `open_keys` / `open_keys_with_line` templates declared on `[windows.editor]` (default templates emit vim's `:drop fnameescape(...)` sequence), then writes the rendered bytes into the editor's kitty pty.

- the editor is started when needed (e.g. via `hop term --role editor`, or when `hop open <target>` lands on a file/Rails ref) — as a plain role terminal, a shell with `nvim` typed into it. When the open comes from the `kitten/hints` dispatch (which runs inside the kitty boss loop and can't launch a window synchronously without deadlocking it), a missing editor is re-spawned out of process via a detached `hop open <target>`, which brings the editor up and opens the file in its own process
- the shared editor is driven by writing keystrokes into kitty's pty via `kitty @ send-text`, matched by the `hop_role=editor` user var on the kitty window — no editor-side remote-control socket is involved, so backends with a private filesystem (devcontainer, ssh) work without any cross-namespace socket coordination
- the editor window is rediscovered like any role window — by its `hop_role=editor` user var (or `hop:editor` app_id) on `p:<session>` — so `hop open` and `hop term --role editor` always find the one editor per session
- if the editor window is closed, the next `hop term --role editor` launches a fresh one. If the editor is quit (`:qa`) but the window stays open at a shell, the next `hop term --role editor` focuses that shell; it can be recreated by:

```bash
hop term --role editor
```

---

## Window identification

Each window must be identifiable by:

- session (the directory `hop` was invoked in)
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

`hop` is a session-oriented CLI tool where each session maps to a Sway workspace containing:

- one shared Neovim instance
- multiple named terminal windows
- a session-scoped browser

and provides:

- fast session switching
- opening file references from terminal output into Neovim via interactive selection
- routing commands (e.g. tests) from Neovim to specific terminal windows (via vigun)
- a tmux-free workflow built on Sway and Kitty
