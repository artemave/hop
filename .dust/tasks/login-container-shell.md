# Login container shell

Login-wrap container-backend shells so they source login profiles, matching host and ssh sessions — via a base64 `$SHELL -lc` wrapper in `inline`.

## Background

Container shells are the lone non-login outlier among backends. A host session's shell is a login shell (kitty spawns `-zsh`); an ssh session's shell is login (`SshTransport` wraps every command in `exec "$SHELL" -lc …`, `hop/backends.py:388,402-403`). Only the container shell is non-login, because `podman exec` provides no implicit login shell the way `sshd` does. So anything a user keeps in a login-only profile (`.zprofile`/`.zlogin`, login-only env, agent/keychain setup) silently doesn't run in containers.

hop already achieves this for ssh via `SshTransport` (`hop/backends.py:379-405`): it base64-encodes the command and decodes it inside a fixed `exec "$SHELL" -lc "$(… base64 -d)"` wrapper, so no quoting or argv-flattening can corrupt it. This task gives the container backend the same treatment.

### Why not `interactive_prefix`?

The prefix is prepended verbatim (`<prefix> <command>`), so it can't be the vehicle: `$SHELL -lc` there can't quote a multi-word command into the single `-lc "…"` argument — `bin/rails console` splits. That is exactly the quoting hell `SshTransport` avoids with base64, which is why the login-wrap belongs in code, not a config string.

## Design

Add a module-level helper in `hop/backends.py`, mirroring the `SshTransport` remote string (`backends.py:402-403`):

```python
def _login_wrap(inner: str) -> str:
    encoded = base64.b64encode(inner.encode()).decode("ascii")
    return f'sh -c \'exec "$SHELL" -lc "$(printf %s {encoded} | base64 -d)"\''
```

A throwaway `sh -c` inside the container expands `$SHELL`, execs it as a **login** shell (sourcing `.zprofile`/`.zlogin`), which execs the decoded command — the container analogue of what `sshd` does implicitly for ssh. base64 makes it immune to quoting/metacharacters.

`$SHELL` is used with no `:-` fallback: it isn't in `podman exec`'s inherited env, but the wrapper's `sh` (bash as `/bin/sh`) sets it from `/etc/passwd` on startup. An image whose `/bin/sh` doesn't provide it (e.g. busybox) fails loudly — the intended behavior, not a case to defend against.

Apply it in `CommandBackend.inline`, gated on a non-empty `interactive_prefix`:

```python
def inline(self, command, session):
    substituted = substitute(command, session=session, host=self._host)
    if not self.interactive_prefix:
        return substituted                     # host: unchanged, no login-wrap
    substituted_prefix = substitute(self.interactive_prefix, ...)
    return f"{substituted_prefix} {_login_wrap(substituted)}"
```

`inline` is the seam `wrap` uses to launch windows. After the send-to-shell and editor-as-role-terminal changes, every role — editor included — launches the shell and receives its command (`nvim`, `bin/dev`, …) typed in via `send-text`. So the only thing flowing through `inline` is the shell-slot value (currently `kitten run-shell` from `[windows.shell]`; a `backend.shell` field after the `shell-setting` task). Login-wrapping it makes the container shell login, and every command typed into it — nvim included — inherits that login environment.

Scope:

- **Interactive only.** `inline` is not used by the noninteractive path (`paths_exist`/`read_file`/`binary_file`/`port_translate` build from `noninteractive_prefix` directly). Those stay bare non-login execs — they get `PATH` from the image `ENV` and must not source login profiles.
- **Container only.** Gated on non-empty `interactive_prefix`. Host (empty prefix) is untouched.
- **Composes with ssh.** For ssh→container (`interactive_prefix = "podman exec dc"`, transport = `SshTransport`), the outer ssh login-wrap and this inner container login-wrap nest cleanly — a login shell on the remote host, then a login shell in the container; base64 nesting decodes layer by layer.

**No config change.** The login-wrap is automatic; the current `[windows.shell] command = "kitten run-shell"` gets login-wrapped in containers with no edit. Double-wrapping is harmless (a login shell running an already-`$SHELL -lc`'d command just runs it), so nothing needs to be stripped first.

## Files to change

- `hop/backends.py` — module-level `_login_wrap`; apply in `inline` gated on non-empty `interactive_prefix`. `base64` is already imported.
- `hop_spec.md`, `README.md`, `docs/devcontainer.md`, `docs/ssh-devcontainer.md` — document that container shells run login (like host/ssh); the kitten install's purpose is unchanged.

## Tests

Real backends, no mocks (per project convention).

- `tests/test_backends.py`:
  - `CommandBackend(interactive_prefix="podman exec dc").inline("bin/rails console", session)` returns `podman exec dc sh -c 'exec "$SHELL" -lc "$(printf %s <b64> | base64 -d)"'` where `<b64>` decodes to `bin/rails console`. Assert by base64-decoding the token, not string-matching the whole line.
  - Host backend (`interactive_prefix=""`) `inline` is unchanged (identity-substituted, no login-wrap).
  - `wrap("")` on a container returns the login-wrapped `${SHELL:-sh}`; `wrap("")` on the host returns `()`.
  - A metacharacter command (`pkill -f '[f]oreman'; bin/dev`) round-trips intact through the base64 wrap.
- `tests/test_remote_ssh.py`: an ssh→container backend (`interactive_prefix` set, `SshTransport`) nests both login-wraps; the decoded innermost command is the original.

## Out of scope

- The `backend.shell` field / moving `kitten run-shell` off `[windows.shell]` — the `shell-setting` task. Until that lands, **host** shells stay non-login (the global `kitten run-shell` overrides kitty's native login shell, and host isn't login-wrapped); this task makes only *container* shells login.
- A `login_shell = false` opt-out for images with a broken/empty `$SHELL` or hostile `/etc/profile`. Automatic for every non-host backend; an escape hatch waits for a real image that needs it.
- Login-wrapping the host backend's commands. Host keeps kitty's native login shell.

## Task Type

implement

## Principles

- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [No defensive don'ts](../principles/no-defensive-donts.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `hop/backends.py` has a module-level base64 `_login_wrap` mirroring `SshTransport`, applied in `inline` when `interactive_prefix` is non-empty and skipped for the host backend.
- The noninteractive path (`paths_exist`/`read_file`/`binary_file`/`port_translate`) is untouched.
- A container `inline`/`wrap` produces a login shell (`$SHELL -lc` via the decoded wrapper); the host `inline`/`wrap` is unchanged (`wrap("")` → `()`).
- ssh→container backends nest the ssh and container login-wraps without corruption; the decoded innermost command is the original.
- Tests pass under `uv run pytest -q`, no mocks, base64 tokens asserted by decoding.
- `hop_spec.md`, `README.md`, `docs/devcontainer.md`, `docs/ssh-devcontainer.md` document the container login shell.
- `make` passes (test, typecheck, lint, format-check, 100% coverage).
- `bunx dust lint` passes for the task file.
