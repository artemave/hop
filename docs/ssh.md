# ssh session backend

This guide walks through running hop sessions whose files, shells, and editor live on a remote machine reached over ssh.

`ssh` is just a backend recipe — hop has no built-in ssh awareness. Like `devcontainer`, the recipe is a chain of `[backends.<name>]` commands in `~/.config/hop/config.toml` or a project's `.hop.toml`. The key idea: ssh's `interactive_prefix` is just `ssh host`, and hop's existing chain — shells, editor, kitten path-resolution — composes through it.

## The model

For an ssh session, here is who runs where:

| Component | Runs on | Notes |
|---|---|---|
| Kitty windows (the GUI) | host | One window per role, talking to a kitty session socket on the host. |
| Shells, scripts, builds | remote | Spawned via `ssh host <cmd>` through the `interactive_prefix`. |
| Neovim | remote | Hop launches nvim through `backend.inline()`, so it runs on the remote inside the local kitty pty. Local kitty IPC writes keystrokes into the ssh-tunneled pty — `:drop <path>` just works. |
| File-existence checks (open-selection kitten) | remote | `hop.focused.paths_exist` runs `ssh host sh -c '<while-read>'` and reads the surviving paths off stdout. |
| Browser | host | The host browser is the GUI surface. `host_translate` rewrites `localhost` URLs so they point at the remote machine's reachable hostname. |

**Implication:** your nvim config, plugins, LSP servers, treesitter parsers — all of these must exist on the *remote* machine, because that's where nvim runs. Sync your dotfiles to the remote (or use an installer like `lazy.nvim`'s lock-file restore on first launch). The same goes for any other role command (`bin/dev`, `bin/rails console`, etc.) — they execute on the remote.

## The stub-directory pattern

Hop's session identity is the working directory the user invokes `hop` from. For an ssh-backed project, the source files don't live on the host at all — they're on the remote. The convention is to create a small local "stub" directory containing only `.hop.toml`, and let it represent the project to sway/hop:

```bash
mkdir -p ~/projects/foo-remote
$EDITOR ~/projects/foo-remote/.hop.toml
```

```toml
# ~/projects/foo-remote/.hop.toml
[backends.foo-remote]
activate = "true"
# … rest of the recipe (see below)
```

`activate = "true"` forces this backend to win auto-detect when you run `hop` from the stub directory. The local dir exists only to give sway a workspace identity (`p:foo-remote`); every shell, editor, and path lookup hops through ssh to the remote.

## Recipe

```toml
[backends.foo-remote]
activate              = "true"
prepare               = "ssh -o ControlMaster=auto -o ControlPath=~/.ssh/cm-%r@%h:%p -o ControlPersist=600 -o ServerAliveInterval=60 myhost true"
interactive_prefix    = "ssh -o ControlPath=~/.ssh/cm-%r@%h:%p myhost"
noninteractive_prefix = "ssh -o ControlPath=~/.ssh/cm-%r@%h:%p myhost"
host_translate        = "echo myhost"
```

The pieces:

- **`prepare` opens a ControlMaster** the first time hop enters the session. `ControlPersist=600` keeps the master alive for ten minutes after the last child closes. `ServerAliveInterval=60` sends keepalives so middleboxes and the server's own `ClientAliveInterval` don't time the connection out. The trailing `true` is a no-op command that just forces the master to establish.
- **`interactive_prefix`** reuses the same socket via `-o ControlPath=...`. Every kitty window's shell, the editor, and any `hop run` invocation flows through this — no per-call ssh handshake.
- **`noninteractive_prefix`** is identical to `interactive_prefix` here — ssh doesn't allocate a TTY for non-interactive commands, so the same prefix works for the kitten's path-existence check and any other piped operation hop drives.
- **`host_translate`** swaps `localhost` in URLs (printed inside a remote service) for the remote's reachable hostname, so the host browser can open dev servers without `ssh -L` port forwarding. If your remote isn't reachable from the local network (firewalled), layer `-L` into `prepare` and adjust `host_translate` / `port_translate` accordingly.

## Verify

From the stub directory:

```bash
cd ~/projects/foo-remote
hop
```

You should see:

- A kitty window opens whose prompt is on the remote (e.g. `user@myhost`).
- `hop open ~/projects/foo/some/file.rb` (remote path!) opens that file in an nvim running on the remote.
- Hint-pick (the open-selection kitten) on output that prints a remote path (e.g. `lib/foo.rb`) highlights the path; clicking it dispatches to the remote nvim.

`cat $XDG_RUNTIME_DIR/hop/sessions/foo-remote.json` shows the persisted record. `backend.interactive_prefix` is the ssh-with-ControlPath line; that's what hop replays for every later command against the session.

## Optional: enable the bridge for editor plugins

The remote-side nvim can't call `hop run`/`hop open` directly — `hop` isn't installed on the remote, and host kitty/Sway state isn't reachable. Hop's bridge solves this: a POSIX-sh client on the remote forwards CLI invocations to a unix socket inside `hopd` on the host.

Both ends are wired up through the existing ssh `ControlMaster`, so there's no second connection or new tunnel:

```toml
[backends.foo-remote]
activate              = "true"
prepare               = """
  ssh -o ControlMaster=auto -o ControlPath=~/.ssh/cm-%r@%h:%p -o ControlPersist=600 \\
      -o ServerAliveInterval=60 \\
      -o StreamLocalBindUnlink=yes \\
      -R /run/hop.sock:$XDG_RUNTIME_DIR/hop/api.sock \\
      myhost true \\
  && hop bridge shim | ssh -o ControlPath=~/.ssh/cm-%r@%h:%p myhost \\
       sudo install -m 755 /dev/stdin /usr/local/bin/hop
"""
interactive_prefix    = "ssh -o ControlPath=~/.ssh/cm-%r@%h:%p myhost"
noninteractive_prefix = "ssh -o ControlPath=~/.ssh/cm-%r@%h:%p myhost"
host_translate        = "echo myhost"
```

What changed in `prepare`:

- **`-R /run/hop.sock:$XDG_RUNTIME_DIR/hop/api.sock`** — reverse-forwards the host's bridge socket to `/run/hop.sock` on the remote. The shim's default `HOP_SOCKET` is `/run/hop.sock`, so this needs no in-shim config.
- **`-o StreamLocalBindUnlink=yes`** — tells the remote sshd to unlink any stale socket at the bind path before creating the new one. Without this, a second `ssh -R` after an unclean session leaves the prior socket and fails. Requires `StreamLocalBindUnlink=yes` to also be enabled in the remote `sshd_config` for SocketAddrPath bindings outside the user's runtime dir; if your sysadmin won't enable that, pick a per-user path like `$HOME/.hop-api.sock` on the remote and set `HOP_SOCKET` to match (e.g. via `~/.ssh/environment` or by exporting it from the shell's profile).
- **`hop bridge shim | ssh … sudo install -m 755 /dev/stdin /usr/local/bin/hop`** — pipes the shim script through the same ssh ControlMaster and writes it to `/usr/local/bin/hop` with the executable bit set. `sudo` is needed because `/usr/local/bin/` is typically root-owned; if your login user can write there, or you'd rather install to `~/.local/bin/hop`, drop the sudo and adjust accordingly. Idempotent; re-runs just rewrite the same content.

The remote needs `curl`, `awk`, `base64`, `tr`, and `mktemp` available in `$PATH`. All coreutils-universal; nothing remote-specific to install.

`hopd` must be running on the host before `hop` starts the session (`exec hopd` in your sway config). If the host socket doesn't exist, `ssh -R` will fail to set up the reverse forward and `prepare` aborts before the shim install.

Verify:

```bash
# From inside the remote shell (a session terminal):
hop term --role editor
```

Errors from the acceptor — `no focused Sway window`, `session 'X' from focused window is not in hop state`, etc. — go to the shim's stderr. The bridge requires you to be focused on the session's **editor** window when the call is made; calls from role terminals (test/server) are rejected pending a future enhancement.

## Tradeoffs

- **Plugins live on the remote.** This is the deepest commitment of the model. Either install your nvim stack on the remote (recommended), or use a different shape (sshfs + local nvim) which hop doesn't support directly.
- **Network round-trips on every keystroke into nvim.** Over a sub-50ms RTT link this is unnoticeable; on bad networks, expect lag. ControlMaster keeps the tunnel warm; mosh / et / similar are outside hop's scope.
- **Kitty hint-pick latency.** Each kitten invocation pays one ssh round-trip (~30-100ms warm) to ask `paths_exist`. Acceptable for a keypress-driven highlight; if you find it laggy, consider a host-side file index (hop doesn't ship one yet).
- **Browser URL translation is on you.** `host_translate` swaps the hostname; the port stays as the remote process bound it. If the remote isn't reachable from the local network, you'll need `ssh -L` and a custom `port_translate`.

## Out of scope for this guide

- **ssh + devcontainer composition** (an ssh backend wrapping a devcontainer on the remote). Doable in principle; not yet covered by a built-in recipe.
- **Auto-detect ssh** (an `activate` probe that walks the filesystem for an ssh marker). The stub-directory pattern + `activate = "true"` is the recommended shape; no probe needed.
- **A first-class `type = "ssh"` backend.** Hop has no code that knows about ssh — it's all config recipe.
