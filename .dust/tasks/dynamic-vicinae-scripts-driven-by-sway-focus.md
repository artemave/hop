# Dynamic vicinae scripts driven by sway focus

Replace the static `vicinae/hop-*` bash scripts with a small Sway-IPC subscriber. The subscriber rewrites `~/.local/share/vicinae/scripts/hop-*` on every focus event so the launcher reflects the focused session.

## Background

Today vicinae integration ships three checked-in bash scripts (`vicinae/hop-window`, `vicinae/hop-switch-session`, `vicinae/hop-kill-session`) that the user manually symlinks into `~/.local/share/vicinae/scripts/`. The pain points:

- `hop-window` shows a single root-search entry; activating it opens a `vicinae dmenu` second search to pick a role. Two searches for one action.
- The static set is the same on every workspace: `Hop kill session` and `Hop window` are listed even on non-`p:*` workspaces where they no-op, polluting search results.
- Built-in roles (`editor`, `browser`) and project-specific terminal roles (`console`, `server`, `test`, etc., as declared by the session's layouts) are invisible at the root — the user always pays the dmenu cost.

Vicinae's launcher cannot register dynamic root entries from inside an extension or script (verified against the daemon source and manifest schema: `commands[]` and `arguments.dropdown.data` are statically baked into `package.json` at install time, and `updateCommandMetadata` only mutates one command's subtitle). The only runtime-mutable surface is the script directory itself: `ScriptCommandService` (`src/server/src/services/script-command/script-command-service.cpp`) registers a `QFileSystemWatcher` on `~/.local/share/vicinae/scripts/` and every XDG equivalent, debounces directory changes 100 ms, and triggers a full rescan that re-emits root items. Adding or removing files in that directory makes them appear/disappear in root search within ~100 ms with no manual reload.

That filesystem-driven seam is what this task uses. A small subscriber listens to Sway's IPC `workspace` events and rewrites the contents of `~/.local/share/vicinae/scripts/hop-*` to reflect the currently focused project's session, including:

- per-role direct entries (`Hop editor`, `Hop browser`, `Hop console`, `Hop server`, ...) so `hop con` from the main launcher dispatches straight into the project's console terminal — no second search;
- a per-other-session `Hop switch to <session>` entry so cross-session switching is also single-search;
- `Hop kill` only when the focused workspace is a `p:*` session — no more polluting non-session workspaces.

This subsumes the existing three scripts and the manual symlink ritual in the README.

## Design

### Process surface

A new `hopd` binary — separate from `hop`, shipped from the same package via a second `[project.scripts]` entry in `pyproject.toml` — runs as a long-lived foreground process that subscribes to Sway IPC `workspace` events. Each event triggers a regeneration pass against `~/.local/share/vicinae/scripts/`. Users wire it once in their Sway config:

```conf
exec_always hopd
```

`exec_always` ensures sway respawns it on reload. The process holds a single Sway IPC socket connection for the duration; if the connection drops, the process exits non-zero and `exec_always` restarts it.

`hopd` is deliberately not a `hop` subcommand: the `hop` CLI is reserved for actions humans and scripts invoke. A long-lived daemon is infrastructure, not automation, and shouldn't pollute `hop --help`. The name is also intentionally non-vicinae-specific: vicinae is today's only consumer of the regenerated scripts, but the user's sway config should not have to mention vicinae explicitly.

Regeneration is idempotent: it always computes the full target set and reconciles the directory. Two concurrent regens (daemon + CLI) write the same content; the worst case is a redundant write, which is fine.

### Script content rules

Resolved against the Sway IPC `get_workspaces` reply for the focused workspace and `list_sessions(...)` for the live session set.

All window-related scripts share the `hop-window-<role>` filename shape so the launcher entries map one-to-one onto the config's window roles. The dispatch (whether it's `hop edit`, `hop browser`, or `hop term --role <role>`) is an internal mapping the script encodes, not something visible at the launcher.

**On a `p:<session>` workspace** (the focused workspace name starts with `p:`, and the session is registered):

- `hop-window-<role>` → `Hop <role>` for every role returned by `session_backends.resolve_windows_for_entry(session)` (built-ins + active layouts + top-level windows, in resolution order). Concrete examples: `hop-window-shell` / `Hop shell`, `hop-window-editor` / `Hop editor`, `hop-window-browser` / `Hop browser`, `hop-window-console` / `Hop console`. The dispatch the script runs is decided by role:
  - `editor` → `hop edit`
  - `browser` → `hop browser`
  - any other role → `hop term --role <role>`
- `hop-switch-<other-session>` → `Hop switch to <other-session>` for every live session whose name is not the focused one. Dispatch: `hop switch <other-session>`.
- `hop-kill` → `Hop kill`, runs `hop kill` from the session's `project_root`. The setsid-detach pattern from today's `hop-kill-session` carries over verbatim.

**Off any `p:*` workspace**:

- `hop-switch-<session>` for every live session. Nothing else — no `hop-window-*`, no `hop-kill`.

**Always**:

- Anything in `~/.local/share/vicinae/scripts/` matching `hop-*` that is not in the target set is deleted. Hop owns the `hop-*` filename namespace in this directory.

### Filename and title sanitization

Roles and session names are user-provided strings. For filenames:

- Replace any character outside `[A-Za-z0-9._-]` with `_`. A role like `test:integration` becomes file `hop-window-test_integration`; a session named `my.project` becomes `hop-switch-my.project` (`.` is preserved).
- The title in the directive header is the unsanitized role / session name (`Hop test:integration` / `Hop switch to my.project`); only the filename is sanitized.
- Collisions (two roles that sanitize to the same filename) are resolved by suffixing `-2`, `-3`, etc. in iteration order. Document this in the regen module's docstring; expect it to be vanishingly rare in practice.

### Generated script template

Each generated script follows the existing directive shape used by `vicinae/hop-window`:

```bash
#!/usr/bin/env bash
# @vicinae.schemaVersion 1
# @vicinae.title <title>
# @vicinae.mode silent

set -euo pipefail
cd <quoted_project_root>
exec hop <subcommand> <quoted_args...>
```

`hop-kill` keeps the `setsid -f` detach + `vicinae close || true` preamble from `vicinae/hop-kill-session` so vicinae's process group SIGTERM doesn't truncate teardown. `hop-switch-<session>` does not need cd (switch resolves by name, not by cwd) and runs `exec hop switch <quoted_session_name>`.

Files are written with mode `0755`. Atomic write via `tempfile.mkstemp` in the target directory + `os.replace` so an in-progress write never produces a half-written script that vicinae might try to parse.

### Sway IPC subscription

`hop/sway.py` grows a subscribe path. Today the transport is one-shot request/reply (`SwayIpcTransport.request`). Subscription needs a long-lived connection that returns one initial `{"success": true}` reply followed by an open-ended stream of event messages whose `message_type` has the high bit set (`0x80000000` for workspace events, per `sway-ipc(7)`).

- `SwayMessageType` gains `SUBSCRIBE = 2`.
- A new `SwayIpcAdapter.subscribe_to_workspace_events()` opens a fresh socket, sends `SUBSCRIBE` with payload `["workspace"]`, validates the success reply, then yields the parsed event payload (a dict with at least `change` and `current.name`) on every subsequent message until the socket closes. It is a generator — caller is the `hopd` loop.
- Subscribe shares `_recv_exact` and the `IPC_HEADER_FORMAT` framing with the existing transport. The reused decoding logic stays in `sway.py`; no separate transport type.

`hopd`'s loop is `for event in adapter.subscribe_to_workspace_events(): regenerate(...)`. It also runs one `regenerate(...)` before entering the loop so the script set is correct on startup before the first event arrives.

### Reconciliation seam

A new `hop/vicinae.py` module owns the pure logic plus the filesystem write:

- `compute_target_scripts(focused_workspace, sessions, *, windows_for) -> tuple[GeneratedScript, ...]` — pure: takes the focused workspace name (or empty string), an ordered tuple of `SessionListing`, and a callable that returns `WindowSpec`s for a given session. Returns the desired filename → content set. Fully unit-testable without filesystem or IPC.
- `reconcile(target, *, scripts_dir) -> None` — applies the target to disk: writes new files, overwrites changed ones, deletes any `hop-*` not in `target`. Atomic per file.
- `regenerate(*, sway, sessions_loader, scripts_dir, windows_for) -> None` — the wrapper `hopd` calls on startup and on every event.

`scripts_dir` is `Path.home() / ".local/share/vicinae/scripts"` by default, overridable for tests. The directory is created on first regen if missing (`mkdir(parents=True, exist_ok=True)`).

### What this removes

- `vicinae/hop-window` — superseded by per-role `hop-window-*`.
- `vicinae/hop-switch-session` — superseded by per-session `hop-switch-*`.
- `vicinae/hop-kill-session` — superseded by `hop-kill` (only present on `p:*` workspaces).
- The whole `vicinae/` directory in the repo, once empty.
- The README's `mkdir -p ~/.local/share/vicinae/scripts && ln -sf "$PWD"/vicinae/hop-* ...` install snippet and the `bindsym $mod+Shift+w exec /path/to/hop/vicinae/hop-window` example. Replaced by a one-line `exec_always hopd`.

## Files to change

- `pyproject.toml` — add `hopd = "hop.daemon:main"` to `[project.scripts]` alongside the existing `hop` entry.
- `hop/sway.py` — `SwayMessageType.SUBSCRIBE = 2`; `SwayIpcAdapter.subscribe_to_workspace_events()` generator; `SwayIpcTransport` Protocol gains a `subscribe(payload) -> Iterator[bytes]` method (or sibling type) so the unit tests can inject a stream-driven fake.
- `hop/vicinae.py` (new) — `GeneratedScript`, `compute_target_scripts`, `reconcile`, `regenerate`.
- `hop/daemon.py` (new) — `main()` entry point for `hopd`: build the same default services `hop` uses, run `regenerate(...)` once, then loop over `subscribe_to_workspace_events()` calling `regenerate(...)` per event. Returns the exit code; non-zero if subscription drops.
- `hop/app.py` — no changes. `hopd` is its own entry point and does not share the `Command` dispatch path. (Hop CLI subcommands that change session state already fire Sway `workspace` events that `hopd` picks up; no in-process regen is needed from the CLI side.)
- Delete `vicinae/hop-window`, `vicinae/hop-switch-session`, `vicinae/hop-kill-session`, and the `vicinae/` directory.
- `README.md` — rewrite the "Vicinae launcher integration" section: replace install + symlink instructions with the one-line `exec_always hopd`; describe the dynamic per-project entries; drop the bindsym example for `hop-window`. Add a sentence noting `hopd` is shipped from the same package as `hop`.
- `hop_spec.md` — add a `hopd` section alongside the `hop` subcommand sections (or as a sibling top-level "Daemon" entry); document its contract (subscribes to Sway workspace events, maintains `~/.local/share/vicinae/scripts/hop-*`, owns the `hop-*` namespace, idempotent on every event). Update the `hop windows` Behavior note that mentions "Intended for launchers (vicinae, rofi)..." to point at `hopd` as the consumer of the resolver instead.

## Tests

Per project convention: real behavior, no mocks. Stubs / fakes are fine.

- `tests/test_vicinae.py` (new) — `compute_target_scripts` against table-driven inputs:
  - On `p:<session>` with built-in-only windows: emits `hop-window-shell`, `hop-window-editor`, `hop-window-browser`, `hop-kill`, plus `hop-switch-*` for every other session.
  - On `p:<session>` with a custom layout adding `console`/`server` roles: emits `hop-window-console`, `hop-window-server` in addition.
  - The dispatched command per role is correct: `editor` → `hop edit`, `browser` → `hop browser`, otherwise → `hop term --role <role>`.
  - Off any `p:*` workspace with three live sessions: emits exactly three `hop-switch-*` and nothing else.
  - With zero live sessions and non-`p:*` focus: empty target.
  - Role and session name sanitization: `test:integration` → file `hop-window-test_integration`, title `Hop test:integration`. Two roles colliding under sanitization → `-2` suffix.
  - Generated script content: starts with the directive header, contains `cd <quoted_project_root>` and `exec hop ...` with shell-quoted args.

- `tests/test_vicinae.py` reconcile cases: write into a `tmp_path`, assert exact filename set on disk, mode 0755, and that pre-existing `hop-stale` files are removed while non-`hop-*` files are left untouched.

- `tests/test_sway.py` — `subscribe_to_workspace_events` against a fake unix-domain server in `tmp_path` (no mocks): the test spins up `socket.socket(AF_UNIX, SOCK_STREAM)` bound to a tempdir path, expects the `SUBSCRIBE` frame, replies `{"success": true}`, then writes one event frame; the test asserts the generator yields the parsed payload. (`SWAYSOCK` is overridden via the existing `socket_path` constructor arg — no need to touch real `/run/user/.../sway-ipc...`.)

- `tests/test_daemon.py` (new) — drive `hop.daemon.main()` against a stub Sway adapter that yields a fake event stream; assert `regenerate` is called once on startup, once per yielded event, and that `main()` returns non-zero when the stream closes (so `exec_always` respawn is triggered).

## Out of scope

- Locking against multiple `hopd` instances. They're idempotent and cheap; the cost of two concurrent subscribers writing the same files is one redundant write per event. Worth a lockfile only if it becomes a real problem.
- Auto-starting `hopd` from `hop` itself. Keep activation explicit (sway config); implicit daemons are surprising.
- Recovering from a dropped Sway IPC connection inside the loop. The process exits and `exec_always` respawns it; reconnect logic adds complexity for no clear win.
- Pinning roles per keybinding (no `bindsym $mod+Shift+w exec hop-window-editor` examples in the README). Users can wire their own bindings against any generated script if they want; this is not a hop-shipped concern.
- Translating the new model to other launchers (rofi, fuzzel, raycast). The `hop_spec.md` `hop windows` enumerator still exists for those; this task is scoped to the vicinae filesystem seam.

## Task Type

implement

## Principles

- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `hopd` is installed as a standalone binary alongside `hop` (declared in `pyproject.toml`'s `[project.scripts]` as `hopd = "hop.daemon:main"`). `hop --help` does not list any vicinae- or watch-related subcommand.
- `hopd` subscribes to Sway IPC `workspace` events on `SWAYSOCK`, runs `regenerate` once on startup, then on every event, and exits non-zero on socket loss so `exec_always hopd` in sway config respawns it.
- `compute_target_scripts` produces the script set described in "Script content rules" for every covered case (focused `p:<session>`, non-`p:*` focus, zero sessions, multi-session). Every generated script is a syntactically valid bash file with the `@vicinae.schemaVersion 1` / `@vicinae.title <...>` / `@vicinae.mode silent` header.
- `reconcile` writes new/changed files atomically (`mkstemp` + `os.replace`), preserves mode `0755`, removes any `hop-*` filename in `~/.local/share/vicinae/scripts/` that is not in the target, and leaves all non-`hop-*` files untouched.
- `~/.local/share/vicinae/scripts/hop-kill` only exists when the focused workspace is `p:*`; vicinae's main search shows no `Hop kill` entry on non-`p:*` workspaces within ~100 ms of focus change.
- After symlinking nothing and only adding `exec_always hopd` to sway config, fuzzy queries from the main vicinae search work in one search box: `hop ed` → `Hop editor`, `hop br` → `Hop browser`, `hop con` (in a project that declares a `console` role) → `Hop console`, `hop sw rails` → `Hop switch to rails-app`, `hop ki` (on a hop workspace) → `Hop kill`. None of these go through `vicinae dmenu`.
- `vicinae/` directory is removed from the repo. README's "Vicinae launcher integration" section reflects the new install (one `exec_always` line, no symlinks) and the dynamic per-project entries. `hop_spec.md` documents `hopd`.
- New tests under `tests/test_vicinae.py`, `tests/test_sway.py`, and `tests/test_daemon.py` follow the no-mock convention (real subprocess / real unix socket / pure logic) and pass under `uv run pytest --cov=hop --cov-branch --cov-fail-under=100`.
- `bunx dust lint` passes for the task file.
