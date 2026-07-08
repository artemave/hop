# Backend owns the integrated login shell

Add a backend `shell` field for the integrated shell and login-wrap interactive container commands, dropping per-command `$SHELL -lc` boilerplate.

## Background

Two facts about non-host (container) backends make configuration noisier than it should be:

1. **Integration is a per-role command that must be repeated.** To get Kitty's OSC 133 markers inside a `podman exec`-backed shell, the user sets `[windows.shell] command = "kitten run-shell"` and relies on the empty-command inheritance rule (`hop/kitty.py::_command_for_role`) to spray that wrap onto every other role's shell slot. Integration is really a property of the *backend* (it needs the `kitten` binary that lives in that container), not a global shell-role override — but there's nowhere on the backend to put it, so it leaks into `[windows.shell]` and applies to every backend including the host, which doesn't need it.

2. **`podman exec` runs commands non-login, so `PATH` is missing.** A devcontainer's interactive `PATH` (Homebrew, version managers, `~/.local/bin`) is set in login profiles (`.zprofile`/`.zlogin`). `podman exec dc <cmd>` runs `<cmd>` non-login, so `claude`, `bin/dev`, `mix`, `iex`, `bin/rails` aren't found. Users compensate by hand-wrapping *every* command: `command = "sh -c '$SHELL -lc \"bin/rails console\"'"`. The live `~/.config/hop/config.toml` currently carries five such hand-written `$SHELL -lc` wraps (llm, rails server, rails console, phoenix server, phoenix console).

hop already solves (2) for ssh: `SshTransport` (`hop/backends.py:379-405`) never prepends a raw `$SHELL -lc` — it base64-encodes the command and decodes it inside a fixed `exec "${SHELL:-/bin/sh}" -lc "$(… base64 -d)"` wrapper (lines 402-403), so no quoting or argv-flattening can corrupt it, and every command over ssh runs under a login shell "so the remote user's normal PATH (e.g. Homebrew) resolves." Containers have no equivalent — `podman exec` provides no implicit login shell the way `sshd` does.

This task gives the container backend the same treatment ssh already gets, and moves `kitten run-shell` from a repeated role command to a single backend field.

### Why not express it inside `interactive_prefix`?

The prefix is prepended verbatim (`<prefix> <command>`), so it can't be the vehicle:

- Embedding `kitten run-shell` there breaks non-shell commands — `podman exec dc kitten run-shell nvim` feeds `nvim` to `kitten run-shell` as its run-before-shell positional.
- Embedding `$SHELL -lc` there can't quote a multi-word command into the single `-lc "…"` argument — `bin/rails console` splits. That is exactly the quoting hell `SshTransport` avoids with base64, which is why the login-wrap belongs in code, not a config string.

So: `kitten run-shell` becomes a backend field (a slot value), and the login-wrap becomes a backend behavior (mirroring `SshTransport`).

## Design

### `backend.shell` — the integrated shell slot value

`BackendConfig` and `CommandBackend` gain `shell: str | None`. It supplies the command that fills the *shell slot* — the value launched for shell-like roles and for the post-exit drop-shell of non-shell roles.

Define the resolved shell-slot value `S` (in `hop/kitty.py`, where the backend is in scope):

```
S = <explicit [windows.shell].command if set>   # _command_for_role(session, SHELL_ROLE)
    or backend.shell                              # backend default
    or ""                                         # host-native / SHELL_FALLBACK sentinel
```

Precedence is deliberate: an explicit `[windows.shell]` override still wins (so a user can force one shell everywhere), `backend.shell` is the per-backend default, and empty falls through to the existing behavior.

Apply `S` at both shell-slot sites in `_shell_like_command`:

- **Shell-like roles** (the `shell` role, or any role whose own `command` is empty — e.g. a `test` role declared `command = ""`, or an ad-hoc `shell-2`): launch `backend.wrap(S, session)`. For the host backend with `S == ""`, `wrap("")` returns `()` (kitty picks the login shell) exactly as today; for a container with `S == "kitten run-shell"`, it launches the wrapped integrated shell.
- **Non-shell roles** (`nvim`, `server`, `console`, …): the post-exit composition becomes `<command>; <S or SHELL_FALLBACK>` — the drop-shell reads `S` instead of `_command_for_role(SHELL_ROLE)`.

This replaces the "integration rides the empty-command inheritance of the shell-role command" mechanism from [multi-step-lifecycle-commands-and-shell-role-inheritance](multi-step-lifecycle-commands-and-shell-role-inheritance.md): empty-command roles still inherit *a* shell, but the shell they inherit is now `S`, which resolves through the backend rather than only through the shell role's config command.

### Login-wrap for interactive container commands

Add a module-level helper in `hop/backends.py`, mirroring the `SshTransport` remote string (lines 402-403):

```python
def _login_wrap(inner: str) -> str:
    encoded = base64.b64encode(inner.encode()).decode("ascii")
    return f'sh -c \'exec "${{SHELL:-/bin/sh}}" -lc "$(printf %s {encoded} | base64 -d)"\''
```

A throwaway `sh -c` inside the container expands `$SHELL`, execs it as a **login** shell (sourcing the container's `.zprofile`/`.zlogin`), which execs the decoded command — the container analogue of what `sshd` does implicitly for ssh. base64 makes it immune to quoting/metacharacters, matching the ssh path.

Apply it in `CommandBackend.inline` (`hop/backends.py:496`), gated on a non-empty `interactive_prefix`:

```python
def inline(self, command, session):
    substituted = substitute(command, session=session, host=self._host)
    if not self.interactive_prefix:
        return substituted                     # host: unchanged, no login-wrap
    substituted_prefix = substitute(self.interactive_prefix, ...)
    return f"{substituted_prefix} {_login_wrap(substituted)}"
```

Because `inline` is the single seam used by both `wrap` (shell-like roles) and the `<cmd>; <shell>` composition in `hop/kitty.py`, every interactive command in a container — the shell slot value `S`, `nvim`, `claude`, `bin/dev` — is uniformly login-wrapped. The user's per-command `$SHELL -lc` wrappers become redundant and are removed from config.

Scope of the login-wrap:

- **Interactive only.** `inline` is not used by the noninteractive path (`paths_exist`, `read_file`, `binary_file`, `port_translate` build their commands from `noninteractive_prefix` directly). Those stay bare, non-login execs — they get `PATH` from the image `ENV` and must not source login profiles.
- **Container only.** Gated on a non-empty `interactive_prefix`. The host backend (empty prefix) is untouched: its shell role stays kitty-native, and its non-shell commands stay as they are.
- **Composes with ssh.** For an ssh→container backend (`interactive_prefix = "podman exec dc"`, transport = `SshTransport`), the outer ssh login-wrap and this inner container login-wrap nest cleanly (a login shell on the remote host, then a login shell in the container). base64 nesting decodes layer by layer.

Double-wrapping is harmless: if a user forgets to strip their manual `sh -c '$SHELL -lc …'`, the login shell simply runs it as its command, so migration can be partial without breakage.

### `~/.config/hop/config.toml` migration

- Delete the global `[windows.shell] command = "kitten run-shell"` block.
- Add `shell = "kitten run-shell"` to `[backends.devcontainer]` and `[backends.starfish]`.
- Strip the now-redundant `sh -c '$SHELL -lc …'` wrappers, leaving the bare commands:
  - `[windows.llm] command = "claude --dangerously-skip-permissions"`
  - `[layouts.rails.windows.server] command = "pkill -f '[f]oreman' 2>/dev/null; bin/dev"`
  - `[layouts.rails.windows.console] command = "bin/rails console"`
  - `[layouts.phoenix.windows.server] command = "pkill -f '[b]eam.*phx.server' 2>/dev/null; mix phx.server"`
  - `[layouts.phoenix.windows.console] command = "iex -S mix"`
- `[layouts.rails.windows.test] command = ""` stays — it now inherits `S` (the backend's integrated shell).

## Files to change

- `hop/config.py` — `BackendConfig.shell: str | None`; parse `[backends.<name>] shell = "…"` (string, optional; reject non-string / empty). It sits alongside `interactive_prefix` in the backend table.
- `hop/backends.py` — `CommandBackend.shell: str | None`; module-level `_login_wrap`; apply it in `inline` gated on non-empty `interactive_prefix`. `base64` is already imported.
- `hop/state.py` — `CommandBackendRecord.shell`; round-trip in `to_json` / `_decode_backend_record` (old records without `shell` decode as `shell=None`).
- `hop/app.py` — `_backend_from_record` (`:328-340`) / `_record_for_backend` (`:346-358`) carry `shell`.
- `hop/kitty.py` — compute `S` and use it in `_shell_like_command` at both the shell-like launch and the drop-shell composition. Thread `backend` where needed.
- `hop_spec.md`, `README.md`, `docs/devcontainer.md`, `docs/ssh-devcontainer.md` — document `backend.shell` and the automatic container login-wrap; retire the "wrap the shell role with `kitten run-shell` + inheritance" framing and the "install kitten so `kitten run-shell` windows work" phrasing (the kitten install stays; the *reason* is now `backend.shell`).
- `~/.config/hop/config.toml` — the migration above.

## Tests

Real subprocesses / real backends where possible (no mocks, per project convention).

- `tests/test_config.py`: parse `[backends.<name>] shell = "kitten run-shell"`; reject non-string / empty `shell`; a backend with no `shell` yields `shell=None`.
- `tests/test_backends.py`:
  - `CommandBackend(interactive_prefix="podman exec dc").inline("bin/rails console", session)` returns `podman exec dc sh -c 'exec "${SHELL:-/bin/sh}" -lc "$(printf %s <b64> | base64 -d)"'` where `<b64>` decodes to `bin/rails console`. Assert by base64-decoding the token, not string-matching the whole line.
  - Host backend (`interactive_prefix=""`) `inline` is unchanged (identity-substituted, no login-wrap).
  - `wrap("")` on a container returns the login-wrapped `${SHELL:-sh}`; `wrap("")` on the host returns `()`.
  - A multi-word / metacharacter command (`pkill -f '[f]oreman'; bin/dev`) round-trips intact through the base64 wrap.
- `tests/test_kitty.py`:
  - With `backend.shell = "kitten run-shell"` and no `[windows.shell]`, a shell-like role launches the backend shell (`S` resolves to `kitten run-shell`, login-wrapped).
  - An empty-command non-shell role (`test`, `command = ""`) inherits `S` (the backend shell), not `SHELL_FALLBACK`.
  - A non-shell role (`server`, `command = "bin/dev"`) composes `bin/dev; <S>` with `S` as the drop-shell.
  - An explicit `[windows.shell] command = "fish"` overrides `backend.shell` (precedence).
  - Host backend with no `backend.shell` still returns the kitty-native `()` shell and the existing `; ${SHELL:-sh}` drop-shell.
- `tests/test_remote_ssh.py`: an ssh→container backend (`interactive_prefix` set, `SshTransport`) nests both login-wraps; the decoded innermost command is the shell slot value.
- `tests/test_state.py`: persist/restore `shell`; an old record without `shell` decodes as `shell=None`.
- `tests/test_app.py`: `_record_for_backend` / `_backend_from_record` round-trip `shell`.

## Out of scope

- A `login_shell = false` (or similar) opt-out for images with a broken/empty `$SHELL` or hostile `/etc/profile`. The login-wrap is automatic for every non-host backend; an escape hatch waits for a real image that needs it.
- hop provisioning the `kitten` binary itself (injecting the `curl … | install` as a default `prepare` step with arch detection). The install stays user-authored in `prepare`; "hop owns the binary" is a separate, heavier follow-up.
- Login-wrapping the **host** backend's non-shell commands. The host keeps kitty's native login shell for the shell role; non-shell host commands stay non-login (a user who needs login there can still hand-wrap, as before).

## Task Type

implement

## Principles

- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [No defensive don'ts](../principles/no-defensive-donts.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)

## Blocked By

(none)

## Definition of Done

- `BackendConfig` and `CommandBackend` carry `shell: str | None`; the parser accepts `[backends.<name>] shell = "…"` and rejects non-string / empty values; a backend without the field resolves to `shell=None`.
- `CommandBackend.inline` login-wraps interactive commands (via a base64 `exec "${SHELL:-/bin/sh}" -lc …` helper mirroring `SshTransport`) when `interactive_prefix` is non-empty, and is unchanged for the host backend. The noninteractive command path (`paths_exist`/`read_file`/`binary_file`/`port_translate`) is untouched.
- The shell slot value `S` resolves as explicit `[windows.shell].command` → `backend.shell` → empty, and is used for shell-like roles (`backend.wrap(S)`) and the non-shell drop-shell (`<cmd>; <S or SHELL_FALLBACK>`). Host-native `()` shell behavior is preserved when `S` is empty and the prefix is empty.
- Empty-command roles inherit `S` (the backend shell), not a bare `SHELL_FALLBACK`.
- `CommandBackendRecord` persists `shell`; old records decode as `shell=None`. `_backend_from_record` / `_record_for_backend` round-trip it.
- ssh→container backends nest the ssh and container login-wraps without corruption; the decoded innermost command is correct.
- Tests in the Tests section pass under `uv run pytest -q` and follow the no-mock convention (base64 tokens asserted by decoding, not literal matching).
- `hop_spec.md`, `README.md`, `docs/devcontainer.md`, and `docs/ssh-devcontainer.md` document `backend.shell` and the automatic container login-wrap, and drop the shell-role-inheritance framing for integration. `~/.config/hop/config.toml` is migrated (backend `shell` fields added, `[windows.shell]` and the five `$SHELL -lc` wrappers removed).
- `make` passes (test, typecheck, lint, format-check, 100% coverage).
- `bunx dust lint` passes for the task file.
