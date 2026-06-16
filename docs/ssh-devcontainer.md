# ssh + container session backend

A worked guide to running a hop session whose dev container lives on a **remote**
machine reached over ssh — the GUI (kitty/Sway) on your laptop, the container on
the remote box. It composes hop's [ssh](ssh.md) and [devcontainer](devcontainer.md)
recipes; read those first.

Everything below is a real, working setup: a Rails app in a `podman-compose`
devcontainer on `admin@devbox.local`, driven from a stub directory `~/projects/tlp`
on the laptop. Every gotcha is one that was hit in practice — this composition has
sharp edges the single-layer recipes don't.

## The shape

There are two wrapping layers. Every command hop runs becomes:

```
ssh admin@devbox.local   podman-compose exec devcontainer   <cmd>
└──────── layer 1 ───────┘└──────────── layer 2 ───────────┘
   host → remote (ssh)        remote → container (podman)
```

| Component | Runs on | Notes |
|---|---|---|
| Kitty windows, Sway, browser | laptop | The GUI surface. |
| Shells, editor, `bin/dev`, builds | container on the remote | `ssh host podman-compose exec devcontainer <cmd>`. |
| `hopd` (bridge host side) | laptop | Reached from the container via an ssh reverse-forward. |
| Source files | container on the remote | The laptop stub dir holds only `.hop.toml`. |

Use the [stub-directory pattern](ssh.md#the-stub-directory-pattern): the laptop
dir exists only to give Sway a workspace identity; `activate = "true"` forces the
backend to win auto-detect.

## The recipe

This is the full `~/projects/tlp/.hop.toml`. Each piece is explained in **Gotchas**.

```toml
[backends.tlp]
activate = "true"

prepare = [
  # 1. ControlMaster + bring the container up.
  "ssh -o ControlMaster=auto -o ControlPath=~/.ssh/cm-%r@%h:%p -o ControlPersist=600 -o ServerAliveInterval=60 admin@devbox.local 'touch $XDG_RUNTIME_DIR/hop-wayland-stub; PATH=/home/linuxbrew/.linuxbrew/bin:$PATH SSH_AUTH_SOCK=$XDG_RUNTIME_DIR/gcr/ssh WAYLAND_DISPLAY=hop-wayland-stub podman-compose -f ~/projects/thonon-les-pains/docker-compose.dev.yml up -d --wait devcontainer'",
  # 2. Reverse-forward the hopd bridge socket onto the master.
  "ssh -O cancel -o ControlPath=~/.ssh/cm-%r@%h:%p -R /run/user/1001/hop.sock:$XDG_RUNTIME_DIR/hop/api.sock admin@devbox.local 2>/dev/null; ssh -o ControlPath=~/.ssh/cm-%r@%h:%p admin@devbox.local 'rm -f /run/user/1001/hop.sock'; ssh -O forward -o ControlPath=~/.ssh/cm-%r@%h:%p -R /run/user/1001/hop.sock:$XDG_RUNTIME_DIR/hop/api.sock admin@devbox.local",
  # 3. Install kitten (for `kitten run-shell` windows).
  "curl -fsSL https://github.com/kovidgoyal/kitty/releases/latest/download/kitten-linux-amd64 | ssh -o ControlPath=~/.ssh/cm-%r@%h:%p admin@devbox.local 'PATH=/home/linuxbrew/.linuxbrew/bin:$PATH SSH_AUTH_SOCK=$XDG_RUNTIME_DIR/gcr/ssh podman-compose -f ~/projects/thonon-les-pains/docker-compose.dev.yml exec -T devcontainer sudo install -m 755 /dev/stdin /usr/local/bin/kitten'",
  # 4. Install the hop bridge shim as in-container `hop`.
  "hop bridge shim --socket /run/user/1001/hop.sock | ssh -o ControlPath=~/.ssh/cm-%r@%h:%p admin@devbox.local 'PATH=/home/linuxbrew/.linuxbrew/bin:$PATH SSH_AUTH_SOCK=$XDG_RUNTIME_DIR/gcr/ssh podman-compose -f ~/projects/thonon-les-pains/docker-compose.dev.yml exec -T devcontainer sudo install -m 755 /dev/stdin /usr/local/bin/hop'",
  # 5. Drop an nvim OSC 52 clipboard plugin into the container's $VIMRUNTIME.
  "echo 'local ok, osc52 = pcall(require, \"vim.ui.clipboard.osc52\"); if ok then vim.g.clipboard = { name = \"OSC 52\", copy = { [\"+\"] = osc52.copy(\"+\"), [\"*\"] = osc52.copy(\"*\") }, paste = { [\"+\"] = osc52.paste(\"+\"), [\"*\"] = osc52.paste(\"*\") } } end' | ssh -o ControlPath=~/.ssh/cm-%r@%h:%p admin@devbox.local 'PATH=/home/linuxbrew/.linuxbrew/bin:$PATH SSH_AUTH_SOCK=$XDG_RUNTIME_DIR/gcr/ssh podman-compose -f ~/projects/thonon-les-pains/docker-compose.dev.yml exec -T devcontainer sudo install -D -m 644 /dev/stdin /usr/share/nvim/runtime/plugin/zz_hop_clipboard.lua'",
]

teardown = "ssh -o ControlPath=~/.ssh/cm-%r@%h:%p admin@devbox.local 'PATH=/home/linuxbrew/.linuxbrew/bin:$PATH SSH_AUTH_SOCK=$XDG_RUNTIME_DIR/gcr/ssh podman-compose -f ~/projects/thonon-les-pains/docker-compose.dev.yml down'"

interactive_prefix    = "ssh -t -o ControlPath=~/.ssh/cm-%r@%h:%p admin@devbox.local 'PATH=/home/linuxbrew/.linuxbrew/bin:$PATH SSH_AUTH_SOCK=$XDG_RUNTIME_DIR/gcr/ssh podman-compose -f ~/projects/thonon-les-pains/docker-compose.dev.yml exec devcontainer'"
noninteractive_prefix = "ssh -o ControlPath=~/.ssh/cm-%r@%h:%p admin@devbox.local 'PATH=/home/linuxbrew/.linuxbrew/bin:$PATH SSH_AUTH_SOCK=$XDG_RUNTIME_DIR/gcr/ssh podman-compose -f ~/projects/thonon-les-pains/docker-compose.dev.yml exec -T devcontainer'"

host_translate = "echo devbox.local"
port_translate = "ssh -o ControlPath=~/.ssh/cm-%r@%h:%p admin@devbox.local 'PATH=/home/linuxbrew/.linuxbrew/bin:$PATH podman ps -q --filter label=io.podman.compose.project=thonon-les-pains --filter label=io.podman.compose.service=devcontainer | head -1 | xargs -r -I@ podman port @ {port} | cut -d: -f2'"

[layouts.rails]
activate = "true"

[layouts.rails.windows.server]
command = "zsh -lc \"'LOCAL_HOSTNAME=devbox.local bin/dev'\""

[layouts.rails.windows.console]
command = "zsh -lc \"'bin/rails console'\""
```

## Gotchas

### 1. The quoting model: host vs remote expansion

Hop runs each prefix as `sh -c "<prefix> <cmd>"` **on the laptop**, so anything
unquoted expands host-side. Single-quote the remote portion so `~`,
`$XDG_RUNTIME_DIR`, and `$PATH` expand on the remote instead — that keeps remote
paths out of the config. `ControlPath=~/.ssh/cm-...` stays *unquoted* on purpose:
that socket lives on the laptop.

### 2. ssh flattens command argv — the central hazard

`ssh host a b c` joins `a b c` with spaces into a single string and the remote
shell re-parses it. One quote level is lost in transit. This breaks any command
that relies on nested quoting.

- **Window commands.** The global devcontainer recipe uses
  `sh -c '$SHELL -lc bin/dev'`. Over ssh that flattens to `sh -c $SHELL -lc bin/dev`,
  so the container runs `sh -c $SHELL` — a bare interactive shell; `bin/dev` never
  runs. The ssh-safe form is `zsh -lc "'<cmd>'"`: the outer `"` strip on the host,
  the inner `'` survive the flatten and are stripped by the remote login shell, so
  the container's login zsh gets `<cmd>` intact (mise/asdf active). Hardcode `zsh`
  rather than `$SHELL` — `$SHELL` would need another in-container shell to expand,
  reintroducing the nesting. (This exact form only works *with* the ssh layer; a
  same-host devcontainer needs the un-nested `sh -c '$SHELL -lc ...'`.)
- **Hop's own path checks.** `paths_exist`/`read_file` used to compose
  `<noninteractive_prefix> sh -c '<script>'`, which hit the same wall — the
  open-selection kitten failed with `zsh:1: parse error near 'do'`. Hop now pipes
  the script over stdin to a bare `sh` (a single token that survives flattening),
  with paths inlined. This is the one place ssh support required a hop code change
  rather than a recipe tweak.

### 3. The non-login ssh PATH

A non-interactive ssh doesn't source your profile, so it misses anything your
interactive shell adds — Homebrew especially. Here it resolved
`/usr/bin/podman-compose` (1.5.0, no `--wait`) instead of the brew 1.6.0 you
actually use. Prepend the tool dir explicitly: `PATH=/home/linuxbrew/.linuxbrew/bin:$PATH`.
Verify with `ssh host 'which -a podman-compose'`.

### 4. Compose env the overlay needs (the `::rw` error)

A typical devcontainer overlay bind-mounts host sockets like `${SSH_AUTH_SOCK}`.
Over a bare non-interactive ssh that variable is empty, so the mount spec becomes
`::rw` and compose aborts with `invalid spec: ::rw: empty section between colons`.
Inject the value the overlay expects — `SSH_AUTH_SOCK=$XDG_RUNTIME_DIR/gcr/ssh`
(the remote's gnome-keyring agent). To use the **laptop's** keys in the container
instead, drop the assignment and add `-A` (agent forwarding) to the ssh flags.

### 5. WAYLAND_DISPLAY: the desktop-session stub

The overlay also mounts `${XDG_RUNTIME_DIR}/${WAYLAND_DISPLAY}` (clipboard
integration for a *local* devcontainer). On a remote box that's a trap:

- The real `wayland-0` socket only exists while someone is logged into the
  remote's graphical session. Headless (or just not logged in) → the mount source
  is missing → `crun: cannot stat .../wayland-0`.
- Leaving `WAYLAND_DISPLAY` empty makes the mount collapse to `/run/user/1001/`,
  which *duplicates* the `${XDG_RUNTIME_DIR}` mount → `duplicate mount destination`.

So `up` `touch`es a hop-owned stub file and points `WAYLAND_DISPLAY` at it:
always present, and distinct from the runtime-dir mount. It's needed **only at
`up`** (container create) — `exec`/`down` don't mount, so they omit it. And it's
fine to lose: a remote session can't use the remote's compositor anyway (your
kitty is on the laptop). See **Clipboard** for how the editor clipboard actually
works.

### 6. The bridge over ssh: `ssh -O forward`, not `-R`

To let in-container editor plugins call back to host `hopd`, reverse-forward
hopd's socket. The non-obvious part: a transient `-R` (`ssh -R ... host cmd`) tears
the forward down when its client exits, **even with a ControlPersist master**.
Attach it to the master instead with `ssh -O forward`, which persists with the
master. Make it idempotent with `-O cancel` + `rm -f` first — `-O cancel` doesn't
unlink the remote socket file, so a bare re-forward fails on the leftover path.

Land the socket under the remote's `$XDG_RUNTIME_DIR` (e.g.
`/run/user/1001/hop.sock`) because the overlay bind-mounts that dir into the
container — that's how the in-container shim reaches it. The uid (`1001`) is
literal: `ssh -R` can't expand a remote env var in the bind path.

### 7. kitten

If your shell/editor windows use `kitten run-shell` (for OSC 133 prompt marks),
`kitten` must exist in the container — install it in `prepare`, same as the
[devcontainer bridge setup](devcontainer.md). Without it every shell window's
command fails.

### 8. Clipboard

nvim runs inside `podman exec`, and the in-container clipboard is its own puzzle:

- No reachable Wayland/X display → `wl-copy`/`xclip` can't talk to a compositor.
- External clipboard tools, **including kitty's own `kitten clipboard`**, write
  OSC 52 to `/dev/tty`; a `podman exec`'d process has no controlling terminal, so
  they fail with `open /dev/tty: no such device or address`.

The one thing that works is **nvim's built-in OSC 52** provider — it writes
through nvim's own channel (`nvim_chan_send`), not `/dev/tty`. Enable it from
*inside the container* (so the user's dotfiles are never touched) by dropping a
plugin into `$VIMRUNTIME/plugin/` in `prepare`. `$VIMRUNTIME` is the one
runtimepath entry `lazy.nvim` keeps after it resets `rtp`, and it's container-local:

```lua
-- $VIMRUNTIME/plugin/zz_hop_clipboard.lua, installed by prepare step 5
local ok, osc52 = pcall(require, "vim.ui.clipboard.osc52")
if ok then
  vim.g.clipboard = {
    name = "OSC 52",
    copy  = { ["+"] = osc52.copy("+"),  ["*"] = osc52.copy("*") },
    paste = { ["+"] = osc52.paste("+"), ["*"] = osc52.paste("*") },
  }
end
```

Copy works out of the box. **Paste** issues an OSC 52 *read*, which kitty gates
behind `clipboard_control`; the default `read-clipboard-ask` prompts on every
paste. Add `read-clipboard` to the host `kitty.conf` to silence it:

```conf
clipboard_control write-clipboard write-primary read-clipboard read-primary
```

Trade-off: any program in any kitty window can then read the clipboard via
OSC 52. Don't reach for `cache_enabled` on the provider to dodge the read — it
makes paste return nvim's last *copy* instead of the real system clipboard.

### 9. Per-window environment

To set an env var for one window only (e.g. the dev server's reachable hostname),
inline the assignment into that window's command rather than the prefix:

```toml
[layouts.rails.windows.server]
command = "zsh -lc \"'LOCAL_HOSTNAME=devbox.local bin/dev'\""
```

Because the assignment prefixes the command, it also **wins over** a value the
project's `.envrc`/direnv exports in the login shell. Putting it on
`interactive_prefix` instead would leak it to every window.

### 10. Layout activation runs host-side

A layout's `activate` probe (e.g. `test -f bin/rails`) runs on the **laptop**, in
the stub dir — which has no project files. So it never fires on its own. Force the
layout on with `activate = "true"` (it field-merges over the global layout, keeping
its windows). The window *commands* still run in the container via the backend.
The backend's own `activate` is `"true"` for the same reason.

### 11. Config changes vs the running session

Hop persists the resolved backend (the prefixes) in the session record, so editing
`interactive_prefix` does **not** affect a running session's new windows — they
replay the persisted prefix until the session is re-bootstrapped (`hop kill && hop`,
or patch `$XDG_RUNTIME_DIR/hop/sessions/<name>.json`). Layout *window commands*, by
contrast, are re-read from config on each window launch, so a changed `server`
command takes effect on the next `hop term --role server`.

### 12. `podman-compose exec` re-resolves `environment:`

`podman-compose exec` re-evaluates the compose file's `environment:` at exec time,
resolving `${VAR}` from the *exec command's* env — not the container's. So a value
you set at `up` can be overridden to empty for `exec`'d processes if the exec
prefix doesn't also set it. (This is why an unset `WAYLAND_DISPLAY` on the prefix
made in-container nvim see `WAYLAND_DISPLAY=""`.)

## Verify

From the stub dir:

```bash
cd ~/projects/tlp
hop
```

- Kitty windows open with prompts inside the container (`/workspace`).
- `server`/`console` run `bin/dev` / `rails console` (not a bare shell).
- Hint-pick on a printed path highlights and opens it in the remote nvim.
- A `localhost:3000` link opens `http://devbox.local:<published-port>` in the host browser.
- In nvim, `"+y` then paste on the host works (OSC 52).
- `cat $XDG_RUNTIME_DIR/hop/sessions/tlp.json` shows the persisted backend.
