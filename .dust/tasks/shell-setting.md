# Shell setting

Give backends a `shell` setting so `kitten run-shell` moves off the global `[windows.shell]` onto the container backends. Host then reverts to kitty's native login shell.

## Background

Integration (`kitten run-shell` for OSC 133 markers) is a property of the *backend* — it needs the `kitten` binary that lives in that container — but there's nowhere on the backend to put it, so it's declared globally as `[windows.shell] command = "kitten run-shell"` and applies to every backend including the host. On host that's wrong twice over: host doesn't need the kitten wrap, and running `kitten run-shell` there replaces kitty's native login shell with a non-login one.

Move it onto the backend: `[backends.<name>] shell = "kitten run-shell"`. Then host (no `shell`, empty `[windows.shell]`) falls back to kitty's native login shell, and only the container backends run `kitten run-shell`.

## Design

### `backend.shell`

`BackendConfig` and `CommandBackend` gain `shell: str | None`. The shell-slot value each terminal role launches resolves as:

```
explicit [windows.shell].command   (a global override, if the user sets one)
  or backend.shell                 (the per-backend default)
  or ""                            (host-native sentinel → wrap("") returns ())
```

`hop/kitty.py::_shell_command(session)` currently returns the shell role's configured command (from `_session_windows_for`, `SHELL_ROLE`). Extend it to take the backend and apply that precedence: shell-role command → `backend.shell` → `""`. Every terminal role already launches `backend.wrap(self._shell_command(...))` (via `_launch_args` and the cold-bootstrap path in `_bootstrap_session_kitty`), so the one change is threading the backend into `_shell_command` at those call sites.

For host with the value `""`, `wrap("")` returns `()` (kitty's native login shell). For a container with `"kitten run-shell"`, it launches that — login-wrapped by the `login-container-shell` change.

### Persistence

`CommandBackendRecord` gains `shell`; round-trip in `hop/state.py` (`to_json` / `_decode_backend_record`, old records without `shell` decode as `shell=None`) and `hop/app.py` (`_backend_from_record` / `_record_for_backend`).

### `~/.config/hop/config.toml` migration

- Delete the global `[windows.shell] command = "kitten run-shell"`.
- Add `shell = "kitten run-shell"` to `[backends.devcontainer]` and `[backends.starfish]`.
- `[layouts.rails.windows.test] command = ""` stays — it inherits the backend shell.

## Files to change

- `hop/config.py` — `BackendConfig.shell: str | None`; parse `[backends.<name>] shell = "…"` (string, optional; reject non-string / empty).
- `hop/backends.py` — `CommandBackend.shell: str | None`.
- `hop/kitty.py` — `_shell_command` takes the backend and applies the shell-role → `backend.shell` → `""` precedence.
- `hop/state.py` — `CommandBackendRecord.shell`; round-trip; old records → `shell=None`.
- `hop/app.py` — `_backend_from_record` / `_record_for_backend` carry `shell`.
- `hop_spec.md`, `README.md`, `docs/devcontainer.md` — document `backend.shell`; retire the "wrap the shell role with `kitten run-shell`" framing (the kitten install stays; the reason is now `backend.shell`).
- `~/.config/hop/config.toml` — the migration above.

## Tests

No mocks.

- `tests/test_config.py`: parse `[backends.<name>] shell = "kitten run-shell"`; reject non-string / empty; no `shell` → `None`.
- `tests/test_kitty.py`:
  - With `backend.shell = "kitten run-shell"` and no `[windows.shell]`, a terminal role launches the backend shell.
  - A role declared `command = ""` launches the backend shell, not a bare `SHELL_FALLBACK`.
  - Explicit `[windows.shell] command = "fish"` overrides `backend.shell` (precedence).
  - Host backend with no `backend.shell` launches the kitty-native `()` shell.
- `tests/test_state.py`: persist/restore `shell`; an old record without `shell` decodes as `shell=None`.
- `tests/test_app.py`: `_backend_from_record` / `_record_for_backend` round-trip `shell`.

## Out of scope

- Login-wrapping container shells — the `login-container-shell` task. This task only relocates the shell declaration; container login-ness comes from that task.

## Task Type

implement

## Principles

- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `BackendConfig` and `CommandBackend` carry `shell: str | None`; the parser accepts `[backends.<name>] shell = "…"`, rejects non-string / empty, and a backend without it resolves to `shell=None`.
- `_shell_command` resolves shell-role command → `backend.shell` → `""`, and every terminal role launches `backend.wrap` of it. Host-native `()` is preserved when both are empty.
- Roles declared `command = ""` launch the backend shell, not a bare `SHELL_FALLBACK`.
- `CommandBackendRecord` persists `shell`; old records decode as `shell=None`; round-trips through `_backend_from_record` / `_record_for_backend`.
- `hop_spec.md`, `README.md`, `docs/devcontainer.md` document `backend.shell`; `~/.config/hop/config.toml` migrated (`[windows.shell]` removed, `shell =` added to the container backends).
- `make` passes (test, typecheck, lint, format-check, 100% coverage).
- `bunx dust lint` passes for the task file.
