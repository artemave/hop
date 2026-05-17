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

1. Bind a per-user unix socket on the host under `$XDG_RUNTIME_DIR/hop/api.sock`. The wire protocol is the existing CLI argv plus the originating `cwd`, returning stdout / stderr / exit code.
2. Backends bridge that socket into the backend's filesystem:
   - **devcontainer**: bind-mount the host socket into the container at a known path (e.g. `/run/hop.sock`) via the recipe's compose file or `prepare` step.
   - **ssh**: layer `-R /tmp/hop.sock:$XDG_RUNTIME_DIR/hop/api.sock` into the existing `ControlMaster`. The ssh control socket already exists; reverse-forwarding the hop socket over it is one extra option in `prepare`.
3. Ship a backend shim — a single POSIX `sh` script or static binary — installed at `/usr/local/bin/hop` (or PATH-injected via the recipe) that marshals argv plus `$PWD` to the socket and exits with the matching code. No Python, no hop image dependency.

The surface starts deliberately narrow: only the commands editor integrations actually need (`run`, `edit`, maybe `term`). Anything that requires interactive stdin tunneling stays host-only.

## Trust And Session Model

- The host is the authority. The shim sends a forwarded `cwd` (the backend's view of the project); the host acceptor resolves it to a session.
- The backend's project path often differs from the host's (e.g. container path `/workspaces/foo` vs host path `~/projects/foo`). The acceptor must translate before the project-marker walk. The session record already pins the host-side `project_root`; the acceptor can match by a per-backend path mapping captured at `prepare` time.
- The socket is per-user. No network exposure. ssh `-R` of a unix socket requires `StreamLocalBindUnlink=yes` in remote sshd; that needs to be documented.

## Scope Boundaries

- Not a generic "hop anywhere" mode. The shim is a thin RPC client; it does not duplicate hop logic.
- Long-lived host daemons are out of scope for the first cut. A `socat`-style acceptor that fork-execs `hop` per request is sufficient if the per-call cost is acceptable.
- Mosh, et, sshfs, and other transport variants are out of scope.

## Tests And Docs To Update

- Add integration coverage for "backend-originated `hop run --role test` reaches the host session" for both `devcontainer` and `ssh` backends.
- Document the bridge socket plus shim install steps in `docs/devcontainer.md` and `docs/ssh.md`.
- Extend the vigun contract doc to state that the contract holds inside any backend with the bridge configured.

## Open Questions

### Should the host expose a long-lived daemon or an on-demand acceptor?

#### Option: On-demand acceptor (e.g. `socat` or systemd socket activation)

Each backend call spawns a fresh `hop` process on the host. No new daemon to manage, no shared state to invalidate. Higher per-call latency from process startup, but `hop` is already fast.

#### Option: Long-lived `hop daemon`

Lower per-call latency and a place to cache resolved sessions, but introduces a lifecycle problem (start, restart on upgrade, log location, supervision) the project does not currently have.

### How should the shim discover the socket?

#### Option: Fixed path inside the backend (e.g. `/run/hop.sock`)

Recipe authors mount or forward to a stable path; the shim has no config. Simple but constrains recipe layout.

#### Option: `HOP_SOCKET` env var injected by the recipe's `interactive_prefix`

A recipe can place the socket anywhere at the cost of one more env var in the backend environment.

### How should the host resolve the caller's project when the backend `cwd` differs from the host path?

#### Option: Capture a path mapping at `prepare` time

The session record stores a `{backend_root: host_root}` pair; the acceptor translates incoming `cwd` before the project-marker walk. Explicit and predictable.

#### Option: Suffix-match against active sessions

The acceptor walks active sessions and picks the one whose `project_root` ends with the same trailing path as the backend's `cwd`. No config, but ambiguous when two sessions share a directory name.

### Which subset of the CLI should the bridge expose first?

#### Option: Only `run` and `edit`

The two commands `vigun` and other editor integrations actually invoke. Smallest blast radius, leaves the rest of the CLI untouched by the bridge contract.

#### Option: `run`, `edit`, `term`, `browser`, `tail`

The full editor-callable subset. Larger initial RPC surface, but avoids a second round of plumbing the next time an integration needs one of the others.
