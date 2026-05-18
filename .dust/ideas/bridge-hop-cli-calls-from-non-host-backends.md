# Bridge hop CLI calls from non-host backends

`hop run`, `hop edit`, and friends only work when invoked on the host. Editor plugins like `vigun` that call `hop` from inside the editor process break for `devcontainer` and `ssh` backends because the editor runs on the backend side, where no `hop` binary exists and where the host's kitty / sway / session state is not reachable.

## Problem

The current model puts:

- Kitty windows, the Sway workspace, and `hop` state files on the **host**.
- The editor (Neovim) and shells on the **backend** (devcontainer or ssh remote).

`vigun`'s integration calls `hop run --role test "<cmd>"` from inside nvim. With the `host` backend that resolves to the host's `hop` and works. With a `devcontainer` or `ssh` backend the call originates from the backend side, where:

- `hop` is not installed. Containers ship minimal toolchains by design; the remote in the ssh case is an arbitrary machine unrelated to the local hop install.
- Even if it were installed, the backend cannot reach the host's kitty session socket, Sway IPC, or `$XDG_RUNTIME_DIR/hop/sessions/<name>.json` without a transport.

This breaks the `docs/vigun.md` contract for any non-host backend and undermines `docs/ssh.md`'s "hop composes through ssh" framing — composition works for host-originated calls, not for editor-originated callbacks.

## Codebase Context

- `hop.commands.run.run_command` resolves the session from the caller's `cwd`, then talks to kitty and state files on the host filesystem.
- `hop.commands.edit.edit_in_session` likewise resolves the session from `cwd` and drives the host kitty editor window.
- Session state files live at `$XDG_RUNTIME_DIR/hop/sessions/<name>.json` on the host. They are not visible from inside a non-host backend.
- `CommandBackend.interactive_prefix` already provides a one-way host→backend transport. There is no symmetric backend→host transport today.
- `docs/vigun.md` pins `hop run --role test "<cmd>"` as the stable entrypoint vigun must use; the contract silently assumes the caller can reach host hop state.

## Refined Proposal

Expose a host-side hop RPC surface and ship a tiny in-backend shim that forwards CLI invocations to it.

1. Bind a per-user unix socket on the host under `$XDG_RUNTIME_DIR/hop/api.sock`. An on-demand acceptor (`socat` or systemd socket activation) fork-execs `hop` per request — no long-lived daemon, no shared state to invalidate. The wire protocol is the existing CLI argv, returning framed stdout / stderr / exit code.
2. Ship a backend shim — a small static binary hop installs on the host (e.g. `/usr/share/hop/shim`) — that pipes argv to `/run/hop.sock`, demultiplexes the framed response, and exits with the host's exit code. Static binary, not a `sh` script, because the wire protocol has to keep stdout, stderr, and the exit code separable. No Python, no container-side runtime dependency.
3. Bridge the socket and shim into the backend's filesystem at fixed paths (`/run/hop.sock` and `/usr/local/bin/hop`):
   - **devcontainer**: two bind-mounts in the user's compose file — the socket and the shim binary. Keeping both in compose puts all backend wiring in one place rather than splitting it across compose and `prepare`.
     ```yaml
     volumes:
       - "${XDG_RUNTIME_DIR}/hop/api.sock:/run/hop.sock"
       - "/usr/share/hop/shim:/usr/local/bin/hop:ro"
     ```
   - **ssh**: the recipe's `prepare` does both — `-R /run/hop.sock:$XDG_RUNTIME_DIR/hop/api.sock` layered into the existing `ControlMaster` for the socket, and an inline `install -m 755 /dev/stdin /usr/local/bin/hop < /usr/share/hop/shim` for the binary. Zero user-side config beyond the existing `.hop.toml`.

## Session Identity On The Host Side

The shim does not need to forward `cwd` or any session handle. Once the call lands on the host, the session is identifiable from existing kitty / Sway state, the same way the open-selection kitten already resolves it:

- The editor window carries a `_hop_editor:<session>` Sway mark; kitty role terminals are reachable via per-session sockets. Either is enough to map an incoming call back to a session.
- For relative paths in `hop edit <path>` the existing `cwd_of_child` (OSC 7, kitty-side) plus the backend `workspace_path` fallback in `hop/focused.py` already gives the right answer — that machinery lives on the host today and works regardless of where the shell that emitted OSC 7 is running.
- Trust stays simple: the socket is per-user, no network exposure. ssh `-R` of a unix socket requires `StreamLocalBindUnlink=yes` in the remote sshd config; that needs to be documented.

## Scope Boundaries

- Not a generic "hop anywhere" mode. The shim is a thin RPC client; it does not duplicate hop logic.
- Mosh, et, sshfs, and other transport variants are out of scope.

## Tests And Docs To Update

- Add integration coverage for "backend-originated `hop run --role test` reaches the host session" for both `devcontainer` and `ssh` backends.
- Document the bridge socket plus shim install steps in `docs/devcontainer.md` and `docs/ssh.md`.
- Extend the vigun contract doc to state that the contract holds inside any backend with the bridge configured.
