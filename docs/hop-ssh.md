# Remote sessions over ssh (`hop ssh`)

Run a project's hop session on a remote machine — the GUI (kitty/Sway) on your
laptop, the shells/editor/container on the remote box — driven by the project's
*own* `.hop.toml`. No second config, no `ssh` in the recipe.

## How it works

- **`hop ssh <host>`** opens an ssh ControlMaster to `<host>`, reverse-forwards
  hopd's bridge socket onto the remote, installs the `hop` shim on the remote's
  PATH, and drops you into a remote login shell. That's all it does — no session
  is created.
- In that shell, **`cd <project> && hop`**. The remote `hop` is the shim; it
  reports `(host, cwd)` to hopd, which starts the session: kitty windows open on
  your laptop, each window's shell running on the remote over the same ssh
  connection.

The session is identified by the remote directory you ran `hop` in — there is no
local copy of the project and no local `.hop.toml`. The same recipe runs a
container locally or on the remote; hop wraps each command in an `ssh` transport
keyed off the session's host. `{host}` resolves to `localhost` locally and the
remote hostname over ssh, so host-dependent values stay portable.

## Quick start

```sh
# on the host (laptop): hopd must be running (exec hopd in your sway config)
hop ssh admin@devbox.local
# now in the remote shell:
cd ~/projects/myapp && hop
```

## Configuration

The repo's `.hop.toml` (or your global `~/.config/hop/config.toml`) is the single
source of truth. Prefixes are identical local and remote:

```toml
[backends.devcontainer]
activate              = "test -f docker-compose.dev.yml"
interactive_prefix    = "podman-compose -f docker-compose.dev.yml exec devcontainer"
noninteractive_prefix = "podman-compose -f docker-compose.dev.yml exec -T devcontainer"
# Host-dependent values use {host} (= localhost locally, the remote hostname over ssh):
host_translate        = "echo {host}"   # so localhost URLs open against the remote
```

- **`host_translate = "echo {host}"`** is what makes the open-selection kitten /
  `hop open` translate a `localhost:PORT` URL printed by a remote service into
  `<remote-host>:PORT` for your laptop browser. Pair it with `port_translate` to
  map the published container port.
- **`{host}`** is the bare hostname (the `user@` is stripped), suitable for
  `LOCAL_HOSTNAME={host}` and `host_translate`.

## Requirements on the remote

- **Login-shell PATH.** The transport runs commands under a non-interactive login
  shell (`$SHELL -lc`), which sources `.zshenv`/`.zprofile` (zsh) or
  `.bash_profile`/`.profile` (bash) — **not** `.zshrc`/`.bashrc`. Put PATH setup
  (Homebrew `shellenv`, tool managers, etc.) where the login shell reads it, or
  `podman-compose`/`bin/dev`/… won't resolve.
- **`~/.local/bin` on PATH** — where `hop ssh` installs the shim.
- **Your editor stack on the remote.** nvim, plugins, LSPs, treesitter parsers
  run on the remote (that's where the editor process is). Sync your dotfiles.

## Editor plugins inside a container

For nvim *inside a devcontainer* to call back (`hop open`, vigun's `hop run`), the
recipe's `prepare` installs the in-container shim and the container surfaces the
bridge socket. `hop ssh` reverse-forwards to `${XDG_RUNTIME_DIR}/hop/api.sock` —
the same path hopd uses locally — so a compose file that already bind-mounts
`${XDG_RUNTIME_DIR}` surfaces it into the container with no extra mount, and the
recipe's `hop bridge shim --socket "$XDG_RUNTIME_DIR/hop/api.sock"` works local or
remote unchanged.

## Troubleshooting

### `Session open refused by peer` / `MaxSessions`

`hop ssh` opens **one ControlMaster per host**, and every session window
multiplexes over it as an ssh *session channel*. sshd's `MaxSessions` (default
**10**) caps those — a multi-window session, or a second session to the same
host, exhausts it:

```
mux_client_request_session: session request failed: Session open refused by peer
```

Raise it on the **remote** in `/etc/ssh/sshd_config` (or a drop-in), then reload
sshd:

```
MaxSessions 100
```

(Port-forwards don't count toward `MaxSessions` — only shell/exec sessions, i.e.
how many windows you run concurrently across all of that host's sessions.)

### A config or code change didn't take effect

The per-session kitty caches hop's code, and a backend's `*_translate` / lifecycle
commands are frozen into the session record when the session is created. After
editing config or upgrading hop, **recreate the session** rather than just
restarting hopd:

```sh
hop kill
hop ssh <host>
cd <project> && hop
```

### A wedged master

A refused/fallback cycle can leave the master half-open (`ControlSocket … already
exists, disabling multiplexing`). Reset it on the host, then reconnect:

```sh
ssh -o ControlPath="$XDG_RUNTIME_DIR/hop/cm-%r@%h:%p" -O exit <host>
hop ssh <host>
```

### A translated URL opens but doesn't load

That's networking, not hop: the remote service's port must be reachable from the
laptop — published on the remote's external interface (not just `127.0.0.1`), not
firewalled, and the hostname must resolve in the browser.

## See also

- [docs/ssh.md](ssh.md) — the underlying hand-wired ssh recipe `hop ssh` automates.
- [docs/ssh-devcontainer.md](ssh-devcontainer.md) — the worked rationale for the
  ssh + container case (transport, quoting, bridge, clipboard).
- [docs/devcontainer.md](devcontainer.md) — the local devcontainer recipe.
