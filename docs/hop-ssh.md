# Remote sessions over ssh (`hop ssh`)

Run a project's hop session on a remote machine — the GUI (kitty/Sway) on your
laptop, the shells/editor/container on the remote box — driven by the project's
*own* `.hop.toml`. No second config, no `ssh` in the recipe.

## How it works

- **`hop ssh <host>`** opens an ssh ControlMaster to `<host>`, reverse-forwards
  hopd's bridge socket onto the remote, installs the `hop` shim on the remote's
  PATH, and drops you into a remote login shell. That's all it does — no session
  is created.
- It also **installs the `kitten` matching your kitty version** on the remote, so
  a remote-*host* session gets Kitty shell integration with no manual setup. The
  remote downloads it itself from kitty's GitHub releases — pinned to the tag
  your host kitty reports, built for the remote's own OS/arch — into
  `~/.cache/hop/kitten`, and skips the fetch when that file already reports the
  right version. If it can't (no network, no `curl`, a platform kitty doesn't
  publish), `hop ssh` **fails with the reason** rather than dropping you into a
  shell whose session would open dead windows. It reaches only the remote host —
  a container behind an ssh→container backend still installs `kitten` in its own
  `prepare` step.
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

## What `hop ssh` leaves on the remote

Two files, both replaced in place rather than accumulating:

- **`~/.local/bin/hop`** — the shim. It needs a PATH hop doesn't control, since
  you type `hop` in whatever shell you happen to be in. Every run rewrites it, so
  it never goes stale.
- **`~/.cache/hop/kitten`** (~25M) — one binary, re-fetched only when your host
  kitty's version changes. Never on PATH: the remote-host integration shell execs
  it by absolute path, so it implies no profile change on the remote, and a
  remote with its own `kitten` elsewhere is left entirely alone.

Both are safe to `rm` when you're done with a box; the next `hop ssh` puts them
back. `$XDG_CACHE_HOME` is honoured if set.

Deliberately, neither the shim nor the kitten has a fallback: the integration
shell execs hop's own kitten with no PATH lookup and no degrade-to-plain-shell.
That's what keeps a remote session's terminal behaviour identical to your
laptop's rather than varying with whatever kitty the remote happens to have —
and it's why a failed install stops `hop ssh` with an error instead of being
best-effort.

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
