# Global layouts and backend prefix

Decouple per-role launch commands from the backend. The backend defines a command prefix; top-level `[layouts.<name>]` and `[windows.<role>]` declare what to launch.

## Background

A previous attempt put per-role commands on the backend (`[backends.<name>.windows.<role>]`). That turned out wrong: every command string redundantly carried the backend's wrap (`podman-compose -f docker-compose.dev.yml exec devcontainer …`) before the actual role command (`bin/dev`, `nvim`, `bin/rails console`). And there was no clean way to declare a window that should autostart only in projects of a particular shape (Rails) without coupling it to one specific backend.

This task replaces that with two orthogonal concepts:

- **Backend** — lifecycle (`prepare` / `teardown` / `workspace` / `port_translate` / `host_translate`) plus a `prefix` shell snippet that wraps every command launched in the backend's environment. No `windows` field.
- **Layout** — a top-level `[layouts.<name>]` table with a single `autostart` probe and a list of windows. When the probe exits 0 in the project root, all of the layout's windows are queued for autostart. Multiple layouts can match in the same session; their windows compose.
- **Top-level windows** — `[windows.<role>]` outside any layout. Always autostart unless individually opted out.

Built-in roles `shell`, `editor`, and `browser` are baked-in windows hop ships with sensible defaults. They appear in the resolved window list automatically. The user can override them by declaring `[windows.<role>]` (top level), which merges per-field with the built-in default.

The reserved-host-backend rule stays: `[backends.host]` cannot be defined. User-defined windows on the host backend just go in top-level `[windows.<role>]` — they aren't attached to any backend.

## Design

### Schema (same in global and project configs)

```toml
# Backend lifecycle + command prefix
[backends.devcontainer]
default        = "test -f docker-compose.dev.yml"
prepare        = "podman-compose -f docker-compose.dev.yml --in-pod=false up -d devcontainer"
teardown       = "podman-compose -f docker-compose.dev.yml down"
workspace      = "podman-compose -f docker-compose.dev.yml exec devcontainer pwd"
port_translate = "..."
prefix         = "podman-compose -f docker-compose.dev.yml exec devcontainer"

# A layout: one autostart probe, multiple windows
[layouts.rails]
autostart = "test -f bin/rails"

[layouts.rails.windows.server]
command = "bin/dev"

[layouts.rails.windows.console]
command   = "bin/rails console"
autostart = "false"  # declared so `hop term --role console` still launches it; not autostarted

# Top-level windows: always autostart unless opted out
[windows.editor]
autostart = "false"  # opt out of the built-in editor for this config

[windows.worker]
command = "bin/jobs"
```

There is **no difference** between global config (`~/.config/hop/config.toml`) and project config (`<project_root>/.hop.toml`) — both accept the full schema. Project entries layer over global entries with project-wins-per-field merge:

- Backends: by name (existing behavior).
- Layouts: by name; windows within a layout merge by role; the layout's `autostart` is replaced if project sets it.
- Top-level windows: by role, per-field.

### Per-window fields

Each window (top-level, in a layout, or a built-in) has:

- `command` (string) — the role command, with **no backend wrap**. The active backend's `prefix` is prepended at launch. Built-in roles ship with a default; the user can override.
- `autostart` (`"true"` or `"false"`, optional) — opt-in / opt-out only. No probe at the per-window level — the gate is whatever the window's container decides:
  - **Built-in window** with no top-level override: hop's default (true for shell/editor, false for browser).
  - **Top-level window**: defaults true (always autostart) unless the entry sets `autostart = "false"`.
  - **Layout window**: gated by the layout's `autostart` probe. The window can carry `autostart = "false"` to opt out of even the matched layout (declared but not auto-launched).

The user can flip a built-in by declaring a top-level entry: e.g. `[windows.browser] autostart = "true"` makes the browser autostart, `[windows.editor] autostart = "false"` skips the editor.

### Backend prefix

`prefix` is a shell snippet treated like other backend command fields (parsed via `_parse_command`, run via `sh -c` after substitution). At launch time, hop builds the effective command as `<prefix> <window_command>` joined by a space (when prefix is set). Host backend has no prefix; the window command runs unchanged.

Examples:

- Host + `[windows.worker] command = "bin/jobs"` → kitty runs `sh -c "bin/jobs"`.
- Devcontainer + `[layouts.rails.windows.server] command = "bin/dev"` → kitty runs `sh -c "podman-compose -f docker-compose.dev.yml exec devcontainer bin/dev"`.

Shell metacharacters in `command` are interpreted by the host shell (the outer `sh -c` running the wrapped string). To run pipes inside the backend, the user writes `command = "sh -c 'bin/dev | tee log'"` so the inner `sh -c` runs in the backend's environment.

### Window resolution

A new resolver (`hop/layouts.py::resolve_windows`) takes `(HopConfig, ProjectSession, CommandRunner)` and produces an ordered tuple of `WindowSpec` (role, command, autostart_active).

Order of the resolved list (also the autostart sweep order at session entry):
1. `shell` (always present).
2. `editor` (always present, autostart from defaults / overrides).
3. `browser` (always present, autostart from defaults / overrides).
4. Active layouts in declaration order — for each, its windows in declaration order.
5. Top-level windows in declaration order.

Resolution algorithm:

1. Seed the result with built-in role specs:
   - `shell`: command="" (host fallback to kitty default), autostart_active=true.
   - `editor`: command=`nvim`, autostart_active=true.
   - `browser`: command="" (host fallback to xdg detection), autostart_active=false.
2. For each `[layouts.<name>]` in declaration order, run the layout's `autostart` probe via the runner in the project root. If exit 0, for each of its windows in declaration order:
   - If the role already exists in the result: replace `command` with the layout's value (when set) and set autostart_active to (true unless the layout window has `autostart = "false"`).
   - If the role is new: append a fresh entry with autostart_active=(true unless `autostart = "false"`).
3. For each top-level `[windows.<role>]` in declaration order:
   - Merge per-field over the existing entry for that role (or append a new one).
   - autostart_active becomes true unless the entry sets `autostart = "false"` (or, for browser, the entry sets `autostart = "true"` to flip the default).
4. Drop any entry whose merged `command` is the empty string for non-built-in roles. (Built-ins keep their empty command — kitty / browser launch paths interpret it as "use the platform default".)

The result is the full list of declared windows for this session. Bootstrap iterates this list:

- `shell` always launches (regardless of autostart_active — it is the bootstrap precondition).
- For every other window with autostart_active=true, dispatch to the right adapter (editor → nvim, browser → browser, else terminal).
- Re-entry skips the entire sweep except the shell launch.

### Persistence

The persisted backend record drops `windows`, gains `prefix`. Layout matching and window resolution re-run on every session entry — the persisted state covers only the backend (so `prepare` / `teardown` / `workspace` / `prefix` / translate commands stay stable across hop invocations). Layouts and top-level windows live in the active config and are re-resolved every time, so adding a layout or `bin/rails` to a project after the session was first created picks up on the next `hop kill` + `hop` cycle.

### Backend wrapping

`CommandBackend` carries `prefix: str | None`. The existing `shell_args(session)` and `editor_args(session)` methods are replaced by `wrap(command, session)` returning the launchable shell argv: `("sh", "-c", "<prefix> <substituted_command>")` or `("sh", "-c", "<substituted_command>")` if no prefix.

`HostBackend.wrap(command, session)` is identity-substituted (returns `("sh", "-c", substituted)`). For the special "shell with empty command" case (host backend, no override), the launch payload code in `kitty.py` continues to pass empty args so kitty uses its default shell — that's a property of the launch path, not the backend.

The post-editor-exit drop-into-shell composition (`<editor_command>; <shell_command>`) is built by the editor adapter from the resolved editor + shell window commands, then handed to `backend.wrap`.

## Files to change

- `hop/config.py` — drop `BackendConfig.windows`; add `BackendConfig.prefix`. New dataclasses `LayoutConfig` (name, autostart, windows) and the existing `WindowConfig` reused for top-level windows. New `HopConfig.layouts: tuple[LayoutConfig, ...]` and `HopConfig.windows: tuple[WindowConfig, ...]`. Parser handles `[backends.<name>] prefix = …`, `[layouts.<name>]`, top-level `[windows.<role>]`. Per-section merge functions. Reject the just-shipped `[backends.<name>.windows.<role>]` shape and the legacy flat `shell`/`editor` shape with actionable messages. Per-window `autostart` is restricted to `"true"` / `"false"`.
- `hop/backends.py` — `CommandBackend.prefix` (str | None). New `wrap(command, session)`. Drop `WindowSpec`, `windows`, `window_for`, `should_autostart`, `window_args`, `shell_args`, `editor_args` (their roles move to `wrap` + the resolver + the editor adapter). `is_runnable` either disappears or becomes "always" (every BackendConfig is runnable now since it doesn't need shell/editor windows on the backend; the parser already validates required fields).
- `hop/layouts.py` (new) — `WindowSpec` (role, command, autostart_active), `resolve_windows(config, session, runner)`. Built-in defaults table. Per-layout autostart probe runs via `_substitute` + `_sh_c` and the injected runner.
- `hop/state.py` — `CommandBackendRecord` drops `windows`, gains `prefix: str | None`. `to_json` / `_decode_backend_record` updated. Old-shape (with `windows` array) decodes as `HostBackendRecord` (no migration; sessions are runtime state).
- `hop/app.py` — `_backend_from_record` / `_record_for_backend` round-trip `prefix` instead of `windows`. Bootstrap path resolves windows via `resolve_windows` and passes the result to `enter_project_session`.
- `hop/commands/session.py` — `enter_project_session` takes a resolved tuple of `WindowSpec` plus the editor/browser/terminal adapters, iterates it, dispatches per role.
- `hop/kitty.py` — `_launch_payload` uses `backend.wrap(window_command, session)` to build launch args for any role. Bootstrap path (no kitty yet) wraps the shell window's command the same way; if the command is empty (host shell default), pass empty args so kitty uses its default shell.
- `hop/editor.py` — `_ensure_editor` builds `<editor_command>; <shell_command>` from the resolved windows, then calls `backend.wrap(...)` to get the launchable argv.
- `hop/browser.py` — top-level `[windows.browser].command` overrides the xdg-detected default. The override path keeps `BrowserLaunchSpec.from_command_string`.
- `hop_spec.md` — replace the per-backend windows section with the layouts + top-level-windows description and the prefix-on-backend rule.
- `README.md` — same.
- `docs/devcontainer.md` — backend example uses `prefix`. Add a layouts example.
- `~/.config/hop/config.toml` — update to the new shape (devcontainer with prefix; rails layout in global config).

## Tests

Real subprocesses where possible (no mocks per project convention).

- `tests/test_config.py`:
  - parse `[backends.<name>] prefix = "..."`; reject unknown fields; reject empty / non-string `prefix`.
  - parse `[layouts.<name>]` with required `autostart` and `[layouts.<name>.windows.<role>]` sub-tables. Missing `autostart` → error.
  - parse top-level `[windows.<role>]` with `command` + `autostart`.
  - reject the just-shipped `[backends.<name>.windows.<role>]` shape with a pointer to `[layouts.…]` / `[windows.…]`.
  - reject the legacy flat `shell`/`editor` fields (continuing the existing behavior).
  - reject per-window `autostart` values other than `"true"` / `"false"`.
  - merge: project layout overrides global layout per-field per-window-role; project top-level window overrides global top-level window per-field; project backend `prefix` overrides global backend `prefix`.
  - both global and project configs accept identical schema (parametrize a couple of cases over both file paths).
- `tests/test_layouts.py` (new):
  - `resolve_windows` with no config produces just the built-in defaults (shell + editor autostart_active=true, browser autostart_active=false).
  - top-level `[windows.editor].autostart = "false"` flips editor's autostart_active to false.
  - top-level `[windows.browser].autostart = "true"` flips browser to autostart_active=true.
  - top-level `[windows.<custom>].command = "..."` adds a window with autostart_active=true; `autostart = "false"` keeps it declared but inactive.
  - layout windows: probe-passing → all the layout's windows enter the result, autostart_active=true; `autostart = "false"` on a single layout window opts it out.
  - layout windows: probe-failing → none of the layout's windows enter the result.
  - multiple matching layouts: both contribute their windows in declaration order.
  - real-fs probe: `autostart = "test -f bin/rails"` triggers when the file exists in tmp_path.
  - placeholder substitution: `{project_root}` in the layout autostart probe is shell-quoted at probe time.
- `tests/test_backends.py`:
  - `CommandBackend.wrap("bin/dev", session)` with prefix set returns `("sh", "-c", "<prefix> bin/dev")` after substitution.
  - `CommandBackend.wrap` without prefix returns `("sh", "-c", "<substituted>")`.
  - `HostBackend.wrap` identity behavior.
  - `CommandBackend` no longer carries windows or `should_autostart`; the public methods are `prepare`, `teardown`, `wrap`, `discover_workspace`, `translate_*`, `with_workspace_path`, `with_*` accessors.
- `tests/test_state.py`:
  - persist/restore `prefix` field.
  - old-shape records (with `windows` array) decode as `HostBackendRecord`.
- `tests/test_session_commands.py`:
  - `enter_project_session` consumes a resolved windows tuple and dispatches each entry to the right adapter (editor → nvim, browser → browser, else terminal). Re-entry skips the sweep.
- `tests/test_app.py`:
  - end-to-end: a config with a Rails layout and a tmp project containing `bin/rails` → on bare `hop`, the resolved windows include the layout's server/console, and the kitty launch payloads use the backend prefix.
  - `_record_for_backend` / `_backend_from_record` round-trip `prefix`.

## Out of scope

- Per-window autostart probes (each window has at most `"true"`/`"false"`; the gate is the layout's probe or the top-level always-on rule). Adding back per-window probes if a real use case shows up.
- Removing the host-backend reservation. `[backends.host]` is still illegal; user-defined windows on the host backend live in top-level `[windows.<role>]`.
- A `[layouts.<name>] backend = "..."` field that scopes a layout to a specific backend. Layouts are backend-agnostic; the autostart probe is enough of an escape hatch.
- Migration of persisted session JSONs from the just-shipped (windows-array) shape. They decode as host and the session re-bootstraps.

## Task Type

implement

## Principles

- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `BackendConfig` carries `prefix` (string, optional); the just-shipped `windows` field is removed; the parser rejects both the just-shipped per-backend windows shape and the legacy flat `shell`/`editor` shape with actionable error messages.
- `LayoutConfig` and top-level `HopConfig.windows` parse and merge with project-wins-per-field semantics. Layout `autostart` is required; per-window `autostart` accepts only `"true"` / `"false"`.
- Both global (`~/.config/hop/config.toml`) and project (`<project_root>/.hop.toml`) configs accept the identical schema.
- `CommandBackend.wrap(command, session)` returns `("sh", "-c", "<prefix> <substituted>")` when prefix is set, `("sh", "-c", "<substituted>")` otherwise. `HostBackend.wrap` returns identity-substituted argv.
- A new resolver in `hop/layouts.py` produces the ordered windows tuple for a session: built-in defaults, layered with active-layout window declarations and top-level window declarations, with per-window autostart opt-in/opt-out applied. Layout autostart probes run via the injected `CommandRunner`.
- `enter_project_session` ensures the shell window unconditionally on first entry, then dispatches every other autostart-active window to the editor / browser / terminal adapter. Re-entry runs only the shell step.
- `CommandBackendRecord` persists `prefix` instead of `windows`. Old-shape payloads decode as `HostBackendRecord`.
- `KittyRemoteControlAdapter._launch_payload` and the bootstrap path build launch args via `backend.wrap(resolved_command, session)`. Empty resolved-command (host shell default) keeps the existing "let kitty pick its default shell" behavior.
- The editor adapter composes `<editor_command>; <shell_command>` from the resolved built-in / overridden window commands, then wraps via `backend.wrap`.
- Top-level `[windows.browser].command` overrides the xdg-detected default through the existing `BrowserLaunchSpec.from_command_string` seam.
- New unit tests cover the cases in the Tests section, follow the existing no-mock conventions, and pass under `uv run pytest -q`.
- `hop_spec.md`, `README.md`, and `docs/devcontainer.md` document the new schema. `~/.config/hop/config.toml` is updated.
- `bunx dust lint` passes for the task file.
