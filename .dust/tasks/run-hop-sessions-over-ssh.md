# Run hop sessions over ssh

Add a `noninteractive_command_prefix` backend field so hop sessions can target a remote filesystem. The open-selection kitten consumes a new `hop.focused.paths_exist` API.

## Background

We're planning support for an ssh-style backend — one whose project root lives on a different host, reached through ssh + ControlMaster. The goal is to make this expressible purely as backend config, with no code that knows the word "ssh".

`hop`'s backend abstraction (`hop/backends.py::CommandBackend`) is defined entirely by shell command strings — `prepare`, `teardown`, `workspace`, `port_translate`, `host_translate`, plus a `command_prefix` that wraps every window's command. With nothing more, `command_prefix = "ssh host"` already runs shells, the editor, and `hop run`/`hop term` on a remote machine: `editor.py` builds nvim's launch via `backend.inline()`, so nvim itself runs on the remote inside a local kitty pty. Kitty's "open file at line X" IPC writes keystrokes into the child process and doesn't care whether the child is local or remote.

The piece that breaks today is the kitten's file-existence filter. `hop/targets.py:153` (inside `_resolve_file_candidate`) calls `resolved_candidate.exists()` on the local filesystem to decide whether a file-shaped token in visible terminal output is a real target. With an ssh backend the candidate paths only exist on the remote, so the filter rejects every match — both in the kitten's `mark` highlight phase and in the dispatch phase. The kitten ends up highlighting nothing for ssh-backed sessions even though clicks would otherwise route correctly.

### Two prefixes, not one

Backend operations split cleanly into two modes:

- **Interactive** — kitty-launched shells, editor, browser. These need a real TTY for line editing, signals, prompts.
- **Non-interactive** — anything hop pipes stdin to or reads stdout from programmatically.

Today's `command_prefix` covers interactive use. For ssh that prefix (`ssh host`) also works non-interactively, because ssh doesn't allocate a TTY for non-interactive commands. But `podman-compose exec devcontainer` does — to pipe stdin to it, you need `podman-compose exec -T devcontainer`, and that `-T` variant is unusable for an interactive shell (no prompt, no line editing).

So backends need **two prefixes**: one for interactive launches, one for non-interactive operations. For ssh they're identical; for podman-compose they differ by one flag.

We introduce `noninteractive_command_prefix` as the second prefix. It defaults to `command_prefix` when unset, which is the right thing for ssh and a hint that any future non-interactive backend operation (file reads, one-shot scripts, anything pipe-driven) should reach for the same field — no new config knobs each time.

### Path-existence check as the first consumer

Today the only non-interactive backend operation hop needs is "do these paths exist in the backend?" — driving the kitten's filter. Rather than make this a config string the user writes ("here's the shell snippet that runs a read-loop"), hop synthesizes the command itself. The user only provides the prefix; hop appends the invariant `sh -c '<read loop>'`. Measured on a local devcontainer, this round-trips ten paths in ~230ms (via `podman-compose exec -T`) or ~120ms (via bare `podman exec -i`); warm-ControlMaster ssh is in the same neighborhood. Comfortably within budget for a keypress-driven highlight.

### `hop.focused.paths_exist` as the kitten API

The kitten itself stays thin: regex → ask hop → yield marks. The "ask hop" call is a single function — `hop.focused.paths_exist(candidates) -> set[str]` — that internally resolves the focused session via sway IPC, reads the focused window's live in-shell cwd from kitty's per-session socket (OSC 7 via `cwd_of_child`), reconstructs the backend from session state, resolves relative candidates against the cwd, and calls `backend.paths_exist`. The kitten has no awareness of sessions, backends, env vars, or kitty IPC.

### `workspace` and the translate methods get deleted

`workspace` / `workspace_path` exist today only to drive two prefix-translation methods on `SessionBackend`: `translate_terminal_cwd` (in-backend cwd → host) and `translate_host_path` (host → in-backend). Both translations exist solely so the kitten could resolve candidates against the host filesystem and `editor.py` could rewrite host paths back into the backend's namespace before reaching nvim. Once the kitten asks the backend directly via `paths_exist`, and `editor.py` passes whatever path it received straight through, the prefix-translation chain has no caller. `workspace`, `workspace_path`, `discover_workspace`, `with_workspace_path`, and both `translate_*` methods are deleted.

### ssh recipe

ControlMaster, keepalives, and dead-master recovery are all expressible as ssh option flags (`ControlMaster=auto`, `ControlPersist=600`, `ServerAliveInterval=60`, `ControlPath=...`) on the user's `command_prefix` and `prepare` strings. Hop does not need a typed `ssh` backend.

The companion ergonomic move is the **stub-directory pattern**: a local directory containing only `.hop.toml`, with the ssh backend's `activate = "true"`. The stub dir gives sway/hop a real local cwd to identify the session by (preserving the "project = windows sharing a working directory" model), while every shell, editor invocation, and path lookup hops through ssh to the remote. No source files live locally.

### Devcontainer migration

Existing devcontainer configs lose the `workspace` line and gain a `noninteractive_command_prefix` line. The `workspace`-derived translations they implicitly relied on go away with this task.

## Design

### `hop/focused.py` (new module)

Single public function:

```python
def paths_exist(candidates: Iterable[str]) -> set[str]: ...
```

Given path strings (absolute or relative, as they appeared in terminal output), return the subset that exist on the focused hop session's backend.

Internally:

1. Resolve the focused hop session:
   - Use `SwayIpcAdapter.get_focused_workspace()` (`hop/sway.py:198`) to read the focused workspace name. If it doesn't match the `p:<session_name>` shape, skip to the fallback.
   - Load session state via `load_sessions()`. If absent for that session name, fall back.
2. Find the focused window's in-shell cwd:
   - Build the session's kitty socket address via `session_socket_address(session_name)` (`hop/kitty.py:82`).
   - Call kitty remote control over that socket to list windows (the `ls` command, matching `focus:1`).
   - Parse the focused window's `foreground_processes[0].cwd` — that's kitty's reported `cwd_of_child` for the focused process, reflecting the in-shell cwd via OSC 7.
   - If the socket isn't live (`KittyRemoteControlAdapter.is_alive()` returns False), fall back.
3. Reconstruct the backend from the session record. Either export `_backend_from_record` from `hop/app.py` or move it into a shared module (`hop/state.py` is a candidate).
4. For each candidate:
   - If absolute: keep as-is.
   - If relative: prepend the focused-window cwd from step 2.
5. Call `backend.paths_exist(session, candidates_as_paths)` once with the full list.
6. Return the subset of *original* input strings whose resolved form was reported existing — the caller compares by string identity, not Path equality.

Fallback (no focused hop session, socket not live, or any error in steps 1–3): treat candidates as if checked against the current process's `Path.cwd()` with local `Path.exists()`. This keeps the kitten useful when invoked from a non-hop kitty window.

### Backend command field: `noninteractive_command_prefix`

Add to `BackendConfig` (`hop/config.py:69`):

- `noninteractive_command_prefix: str | None = None` — shell prefix used to wrap commands hop pipes stdin to or otherwise runs non-interactively in the backend. Defaults to `command_prefix` when unset.

Add the field to `_BACKEND_FIELDS`, `_merge_backend_pair` (project-wins-per-field, same as the others), and `backend_from_config` plumbing.

No corresponding `paths_exist` config field. The shell read-loop is hop's responsibility, not the user's.

### Backend method: `paths_exist`

Add to the `SessionBackend` Protocol (`hop/backends.py:40`):

```python
def paths_exist(self, session: ProjectSession, paths: Sequence[Path]) -> set[Path]: ...
```

Behavior:

- `HostBackend.paths_exist`: returns `{p for p in paths if p.exists()}` — pure local check.
- `CommandBackend.paths_exist`:
  1. Empty input → empty result, no subprocess invoked.
  2. Pick prefix: `self.noninteractive_command_prefix or self.command_prefix`. If both are `None` (shouldn't happen for any real CommandBackend, but guard anyway): fall back to local `Path.exists()` per path.
  3. Compose the command: `f"{prefix} sh -c {shlex.quote(_PATH_EXISTS_LOOP)}"` where `_PATH_EXISTS_LOOP` is the constant `'while IFS= read -r p; do test -e "$p" && printf "%s\\n" "$p"; done'`.
  4. Run via `self.runner` with the newline-joined paths fed on stdin.
  5. Parse stdout: each non-empty line is a path that exists. Return the subset of input `paths` whose `str(path)` appears in stdout.
  6. Non-zero exit → `SessionBackendError` with stderr/stdout, mirroring how `_run_translate_command` reports failures.

The shell loop is a module-level constant (`_PATH_EXISTS_LOOP`) in `hop/backends.py`, not config.

### Extend `CommandRunner` to accept stdin

`CommandRunner` (`hop/backends.py`) is a Protocol over `subprocess.run`-style calls. Add an optional `stdin: bytes | None = None` parameter. `default_runner` passes it through to `subprocess.run` (which accepts `input=...`). Existing call sites pass nothing; only `paths_exist` uses the new parameter.

This is orthogonal to the recent stderr-streaming change (b223245) — `default_runner` already branches on `sys.stderr.isatty()` for stderr handling. Adding a stdin parameter is additive.

### Delete `workspace`, `workspace_path`, and the translate methods

Remove:

- `BackendConfig.workspace` field (`hop/config.py`).
- `CommandBackend.workspace_command`, `CommandBackend.workspace_path`, `CommandBackend.discover_workspace`, `CommandBackend.with_workspace_path` (`hop/backends.py`).
- `CommandBackendRecord.workspace_command`, `CommandBackendRecord.workspace_path` (`hop/state.py`, plus their JSON encode/decode).
- The `discover_workspace` call site in `SessionBackendRegistry.resolve_for_entry()` at `hop/app.py:210` and the subsequent `with_workspace_path(...)` chain.
- `_backend_from_record` / `_record_for_backend` round-trip of `workspace_command` and `workspace_path`.
- `SessionBackend.translate_terminal_cwd` and `SessionBackend.translate_host_path` from the Protocol; their `HostBackend` and `CommandBackend` implementations.
- The call site at `hop/commands/open_selection.py:50` (`translate_terminal_cwd(session, source_cwd)`) — `source_cwd` is no longer passed through any translator.
- The call site at `hop/editor.py:298` (inside `_translate_target`) — `path_text` flows to nvim unchanged. `_translate_target` may collapse into inline logic that just splits the target string into path + line number.

`translate_localhost_url` stays — URL host/port translation is a separate concern from path existence.

### Wire the kitten

`kittens/open_selection/main.py::mark` (currently `lines 48–72`) is rewritten as:

```python
from hop.focused import paths_exist

def mark(text, args, Mark, extra_cli_args, *unused_args):
    matches = list(_collect_matches(text))
    existing = paths_exist(c.selected_text for c in matches)
    for index, match in enumerate(matches):
        if match.selected_text in existing:
            yield Mark(index, match.start, match.end, match.selected_text, {})
```

`_collect_matches` is the existing regex iteration extracted into a helper that yields a small dataclass (`start`, `end`, `selected_text`). No session, backend, env, kitty-IPC, or cwd logic in the kitten — all of that lives in `hop.focused.paths_exist`.

`handle_result` / `dispatch_selected_match` continue to run in boss context and call `open_selection_in_window`. That dispatch path also routes through `hop.focused.paths_exist` (or shares its lower-level pieces) so the in-boss code and the subprocess code agree on what's a valid target.

### Wire `_resolve_file_candidate`

`hop/targets.py::_resolve_file_candidate` and `resolve_visible_output_target` are simplified to drop the local `Path.exists()` check at line 153. Existence filtering is no longer their concern — it's pushed up to the caller via `paths_exist`. They become pure path-shape resolution: given a candidate string and a base cwd, produce the resolved absolute path (or None for unparseable shapes). The caller decides existence.

### Wire the dispatch path

In `hop/commands/open_selection.py::open_selection_in_window`:

- Drop the `translate_terminal_cwd` call at line 50.
- Resolve candidate paths via the simplified `resolve_visible_output_target`.
- Filter via `hop.focused.paths_exist` (or its lower-level building block — dispatch already has `session` + `backend` in hand and can call `backend.paths_exist` directly with relative resolution done locally).
- Pass the resolved file path straight to `neovim.open_target` — no `translate_host_path` call downstream.

### Persist `noninteractive_command_prefix` in session state

`CommandBackendRecord` (`hop/state.py:20`) round-trips command fields. Add `noninteractive_command_prefix: str | None = None` to:

- `CommandBackendRecord` (with matching `to_json` / decode in `_decode_backend_record`),
- `CommandBackend` dataclass + `backend_from_config`,
- `_record_for_backend` / `_backend_from_record` round-trip in `hop/app.py`.

`workspace_command` and `workspace_path` are *removed* from this record at the same time — old session JSONs containing those keys decode without error (keys silently dropped). Sessions are runtime state in `${XDG_RUNTIME_DIR}/hop/sessions/`; live sessions re-bootstrap on next `hop kill` + `hop`.

### Canonical ssh recipe

```toml
[backends.myhost]
activate       = "true"
prepare        = "ssh -o ControlMaster=auto -o ControlPath=~/.ssh/cm-%r@%h:%p -o ControlPersist=600 -o ServerAliveInterval=60 myhost true"
command_prefix = "ssh -o ControlPath=~/.ssh/cm-%r@%h:%p myhost"
host_translate = "echo myhost"
# noninteractive_command_prefix defaults to command_prefix — ssh doesn't allocate TTY for non-interactive commands
```

### Devcontainer migration

The existing `~/.config/hop/config.toml` devcontainer config is updated:

- **Remove** the `workspace = "podman-compose ... pwd"` line.
- **Add** the `noninteractive_command_prefix` line:

```toml
noninteractive_command_prefix = "podman-compose -f docker-compose.dev.yml exec -T devcontainer"
```

Notes:
- `-T` disables podman-compose's TTY allocation. Without it, hop's stdin pipe gets eaten by a pty and the inner read loop sees nothing.
- Measured ~230ms per `paths_exist` call with `podman-compose exec -T`. Bare `podman exec -i <container_name> sh -c '...'` is ~120ms but the recipe needs the running container's name (resolvable via `podman-compose ps -q devcontainer`). The simpler `podman-compose exec -T` form ships in `docs/devcontainer.md`; users who notice the latency can swap in the bare form.

### Documentation

- New file `docs/ssh.md`: canonical ssh recipe, stub-directory pattern, tradeoffs (nvim runs on the remote, plugins/LSP must be installed there; `host_translate` needs the remote port to be reachable from the local network or the user adds `-L` to `prepare`).
- `docs/devcontainer.md`: replace `workspace` with `noninteractive_command_prefix` in the example.
- `hop_spec.md`: drop `workspace` / `translate_terminal_cwd` / `translate_host_path` references; document `noninteractive_command_prefix` and the kitten's `hop.focused.paths_exist` consumption point.
- `README.md`: replace `workspace` with `noninteractive_command_prefix` in the per-backend fields list.

## Files to change

- `hop/config.py` — drop `workspace` field on `BackendConfig`; add `noninteractive_command_prefix`. Update `_BACKEND_FIELDS`, `_merge_backend_pair`, parser. Reject the legacy `workspace` field with an actionable error message that points users at `noninteractive_command_prefix`.
- `hop/backends.py` — Protocol drops `translate_terminal_cwd`, `translate_host_path`; gains `paths_exist`. Drop `discover_workspace`, `with_workspace_path` from `CommandBackend`. Drop `workspace_command`, `workspace_path` fields. Add `noninteractive_command_prefix` field + `paths_exist` method on `HostBackend` / `CommandBackend`. Extend `CommandRunner` protocol to accept optional stdin; update `default_runner` accordingly. Module-level constant `_PATH_EXISTS_LOOP`.
- `hop/state.py` — drop `workspace_command` / `workspace_path`; add `noninteractive_command_prefix`. Update `to_json` / `_decode_backend_record`.
- `hop/app.py` — drop the `discover_workspace` / `with_workspace_path` chain in `SessionBackendRegistry.resolve_for_entry()` (`line 210`); drop `workspace_*` round-trip in `_backend_from_record` / `_record_for_backend`; add `noninteractive_command_prefix` round-trip. Export `_backend_from_record` (or move it — `hop/focused.py` consumes it).
- `hop/targets.py` — `_resolve_file_candidate` and `resolve_visible_output_target` lose the `.exists()` check at `line 153`; they return resolved absolute paths (or None for unparseable shapes), unfiltered.
- `hop/commands/open_selection.py` — drop `translate_terminal_cwd` call at `line 50`; route candidate filtering through `hop.focused.paths_exist` (or call `backend.paths_exist` directly when session+backend are already in hand). Dispatch the resolved path to nvim unchanged.
- `hop/editor.py` — drop the `translate_host_path` call at `line 298`. `_translate_target` collapses to a target-string-split helper that doesn't touch the backend.
- `hop/focused.py` (new) — `paths_exist(candidates)` API. Internally: sway focused-workspace lookup via `SwayIpcAdapter.get_focused_workspace()`, kitty per-session socket query via `session_socket_address(...)` for `cwd_of_child`, backend reconstruction, candidate resolution, `backend.paths_exist` call.
- `kittens/open_selection/main.py::mark` — rewritten as the thin shell described above. `dispatch_selected_match` / `handle_result` continue to run in boss context.
- `~/.config/hop/config.toml` — drop `workspace`; add `noninteractive_command_prefix` to the devcontainer backend.
- `hop_spec.md` — replace `workspace` / translate references with `noninteractive_command_prefix` + `paths_exist`; describe the kitten's `hop.focused.paths_exist` consumption.
- `README.md` — replace `workspace` with `noninteractive_command_prefix` in the backend fields list.
- `docs/devcontainer.md` — show the new devcontainer config.
- `docs/ssh.md` (new) — canonical recipe + stub-directory pattern + tradeoffs.

## Tests

The prevailing pattern in this repo for backend tests is mocking via `RecordingRunner` (`tests/test_backends.py`) — captures calls, returns synthesized `CompletedProcess`. The project rule says avoid mocks where possible, but the existing tests heavily use this pattern, so we continue it where it's already in place and reach for real subprocesses where they're cheap and meaningful.

- `tests/test_backends.py`:
  - `HostBackend.paths_exist` against `tmp_path` with a mix of existing and non-existing paths returns the existing subset. Real Path.exists, no mocks.
  - `CommandBackend.paths_exist` with `noninteractive_command_prefix` set: synthesizes `<prefix> sh -c '<loop>'`, runs against `tmp_path` via a real subprocess (`["sh", "-c", "while IFS= read -r p; do test -e \"$p\" && printf '%s\\n' \"$p\"; done"]` as the prefix is itself runnable), verifies the returned set matches the existing subset of inputs.
  - `CommandBackend.paths_exist` falls back to `command_prefix` when `noninteractive_command_prefix` is unset.
  - `CommandBackend.paths_exist` with both prefixes unset falls back to local `Path.exists()`.
  - Empty input list → empty result, no subprocess invoked.
  - Non-zero exit from configured command raises `SessionBackendError` with stderr in the message.
  - `CommandRunner` accepts and pipes stdin; verified by a subprocess script that echoes its stdin back.
  - Tests for `translate_terminal_cwd`, `translate_host_path`, `discover_workspace`, `with_workspace_path` are deleted.
- `tests/test_config.py`:
  - Round-trip `noninteractive_command_prefix` from TOML.
  - Project-wins-per-field merge with global `noninteractive_command_prefix`.
  - Reject non-string `noninteractive_command_prefix`.
  - Reject the legacy `workspace` field with the actionable error message pointing at `noninteractive_command_prefix`.
- `tests/test_state.py`:
  - `noninteractive_command_prefix` survives `to_json` → `load_sessions`.
  - Session JSONs containing legacy `workspace_command` / `workspace_path` keys decode without error (keys silently dropped).
- `tests/test_targets.py` (existing or new):
  - `_resolve_file_candidate` returns the resolved absolute Path without calling `.exists()`. Verify by giving it a path that does not exist on disk and asserting it's still returned.
- `tests/test_focused.py` (new):
  - `paths_exist` with a fake sway adapter (no focused hop session) falls back to local `Path.exists()` against `Path.cwd()`.
  - `paths_exist` with a fake sway adapter pointing at a fake hop session and an injected cwd: resolves relative candidates against the cwd, calls `backend.paths_exist`, returns the existing subset as the original input strings.
  - When the kitty socket isn't live, `paths_exist` falls back.
  - Backend reconstruction round-trips `noninteractive_command_prefix` correctly through state.
- `tests/test_open_selection_commands.py`:
  - Update existing tests that asserted `translate_terminal_cwd` / `translate_host_path` calls — replace with assertions about `paths_exist` (or its lower-level helper) being the existence filter and the unchanged path being passed to `neovim.open_target`.
- `tests/test_open_selection_kitten.py`:
  - Mark phase with a fake `paths_exist` callable returning a controlled subset filters the yielded marks accordingly.
  - When `paths_exist` falls back to local behavior (no focused hop session), mark still yields marks for paths that exist locally.
- Integration smoke (manual, documented in `docs/ssh.md`, not in CI):
  - Stub-dir + ssh recipe against `localhost` (sshd required) — verify session creation, shell, editor, kitten highlight + dispatch end-to-end.

## Out of scope

- A first-class `type = "ssh"` backend or any code that special-cases ssh. The whole feature is one new generic prefix field + one focused-session helper.
- ControlMaster lifecycle managed by hop (master check, dead-master detect-and-reopen). `ControlPersist` + `ServerAliveInterval` cover the common cases; users with stricter networks layer extra ssh options into `prepare` / `command_prefix` themselves.
- Browser port forwarding via `ssh -L`. Users whose remote ports aren't reachable from the local network add `-L` to their `prepare` and adjust `host_translate` / `port_translate` accordingly.
- ssh+devcontainer composition. Deferred until plain ssh has shaken out.
- A daemon-level remote file index. Bulk `paths_exist` is fast enough; index can be added later if profiling says otherwise.
- A general `hop inspect` CLI surface. `hop.focused.paths_exist` is enough for the kitten today; if more "what's the focused session doing" queries land later, they can promote into a public CLI.
- Migration of in-flight session state. The new state shape silently drops old `workspace_*` keys, and existing live sessions degrade until they're relaunched against the updated config.
- A user-facing `paths_exist` config field. Hop owns the shell read-loop; users only ever express the non-interactive prefix.

## Task Type

implement

## Principles

- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `BackendConfig` carries `noninteractive_command_prefix` and no longer carries `workspace`. The parser merges `noninteractive_command_prefix` project-wins-per-field with the global value and rejects the legacy `workspace` field with an actionable error.
- `SessionBackend` Protocol exposes `paths_exist(session, paths) -> set[Path]` and no longer exposes `translate_terminal_cwd` / `translate_host_path`. `HostBackend.paths_exist` returns the locally-existing subset; `CommandBackend.paths_exist` synthesizes `<prefix> sh -c '<loop>'` (preferring `noninteractive_command_prefix`, falling back to `command_prefix`), runs it with newline-joined paths on stdin, parses newline-separated stdout, and falls back to local `Path.exists()` when no prefix is available.
- `CommandRunner` protocol accepts an optional stdin payload; the default runner passes it through to `subprocess.run`.
- `CommandBackendRecord` persists `noninteractive_command_prefix` and no longer persists `workspace_command` / `workspace_path`. Old session JSONs containing those keys decode without error (keys silently dropped).
- `discover_workspace`, `with_workspace_path`, and the session-bootstrap discovery chain in `SessionBackendRegistry.resolve_for_entry()` (`hop/app.py:210`) are removed.
- `hop/focused.py` exposes `paths_exist(candidates)` that resolves the focused hop session via sway, queries the session's kitty socket for the focused window's `cwd_of_child`, reconstructs the backend, resolves relative candidates against the cwd, calls `backend.paths_exist`, and returns the existing subset as the original input strings. Falls back to local `Path.exists()` against `Path.cwd()` when no hop session is focused or the kitty socket is not live.
- `hop/targets.py::_resolve_file_candidate` and `resolve_visible_output_target` no longer call `.exists()`; they return resolved absolute paths (or None for unparseable shapes) and leave existence filtering to callers.
- `hop/commands/open_selection.py::open_selection_in_window` no longer calls `translate_terminal_cwd`. Candidate filtering routes through `hop.focused.paths_exist` (or `backend.paths_exist` directly). The resolved path is dispatched to nvim unchanged.
- `hop/editor.py:298` no longer calls `translate_host_path`. `path_text` is passed to nvim unchanged.
- `kittens/open_selection/main.py::mark` is rewritten as a thin shell: regex extraction, `paths_exist` consultation, mark emission. No session, backend, env, kitty-IPC, or cwd logic in the kitten.
- `translate_localhost_url` continues to apply to URL targets.
- `docs/ssh.md` documents the canonical ssh recipe (ControlMaster + keepalives + `host_translate`), the stub-directory pattern, and tradeoffs (nvim runs remote; LSP/plugins must exist there).
- `docs/devcontainer.md` replaces the `workspace` line with `noninteractive_command_prefix` in the devcontainer example.
- `~/.config/hop/config.toml` drops `workspace` and gains `noninteractive_command_prefix` for the devcontainer backend.
- `hop_spec.md` replaces `workspace` / translate references with `noninteractive_command_prefix`; the kitten section notes `hop.focused.paths_exist` as the consumption point.
- `README.md` replaces `workspace` with `noninteractive_command_prefix` in the per-backend fields list.
- New unit tests cover the cases listed in the Tests section, follow the existing test conventions, and pass under `uv run pytest -q`.
- `bunx dust lint` passes for the task file.
