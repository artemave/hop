# devcontainer session backend

This guide walks through the one-time setup for running hop sessions inside containers, and the day-to-day mental model.

`devcontainer` is just a name — hop has no built-in knowledge of compose. Every backend is a chain of commands you describe in `~/.config/hop/config.toml` or in a project's `.hop.toml`; the recipes below happen to use `podman-compose`, but the same shape works for `docker compose`, `podman compose`, `lima`, `kubectl exec`, etc.

## What hop runs

For a session whose backend is `devcontainer`, hop will:

| Hop action | Hop runs |
|---|---|
| First entry into the session (`hop`) | the backend's `prepare` command, then bootstraps kitty |
| Each role terminal | the backend's `shell` command (one per terminal — same container) |
| `hop edit` | the backend's `editor` command (hop drives nvim by writing keystrokes to kitty's pty — no socket setup) |
| `hop kill` | closes kitty windows, then runs the backend's `teardown` command |

All command lists live in `~/.config/hop/config.toml` under `[backends.<name>]`.

## One-time setup

### 1. Project compose file uses `include:`

Hop runs the project's compose file **standalone** — there is no overlay flag in hop. If your dev container is composed of a project-specific layer plus a personal/dotfiles overlay, the recommended pattern is to have the project file `include:` the overlay:

```yaml
# <project_root>/docker-compose.dev.yml
include:
  - ${HOME}/projects/dotfiles/devcontainer/docker-compose.yml
services:
  devcontainer:
    # project-specific overrides go here, e.g.:
    # environment:
    #   FOO: bar
```

Important caveats with `podman-compose` (the Python implementation):

- Use the **plain-string** form of `include:` (a list of strings). The modern `{path: ...}` dict form is not supported and crashes podman-compose with a `TypeError: join() argument must be str ... not 'dict'`.
- Use `${HOME}` (or any other env-var expansion) for absolute paths — `~` is **not** expanded by compose and produces a similar error.

The `devcontainer` service must allocate a TTY:

```yaml
services:
  devcontainer:
    tty: true
    stdin_open: true
    # ...
```

Hop's model is "one container, many terminals via `compose exec`" — that requires the container to stay alive. Without `tty: true`, an interactive PID 1 (typical `bash -lc '...'` entrypoints) reaches EOF immediately under `compose up -d` and the container exits. Hop's `prepare` then succeeds, but by the time kitty `exec`s into it the container is gone and the bootstrap shell window flashes open and closes.

After updating the file, sanity-check it:

```bash
podman-compose -f docker-compose.dev.yml ps
```

If that succeeds with the merged service list (your project's `devcontainer` service plus anything the overlay defines), the layout is good.

### 2. Global hop config

Create `${XDG_CONFIG_HOME:-~/.config}/hop/config.toml`:

```toml
[backends.devcontainer]
activate              = "test -f docker-compose.dev.yml"
prepare               = "podman-compose -f docker-compose.dev.yml up -d devcontainer"
teardown              = "podman-compose -f docker-compose.dev.yml down"
port_translate        = """
  podman ps -q \\
    --filter label=io.podman.compose.project=$(basename {project_root}) \\
    --filter label=io.podman.compose.service=devcontainer \\
    | head -1 \\
    | xargs -r -I@ podman port @ {port} \\
    | cut -d: -f2
"""
interactive_prefix    = "podman-compose -f docker-compose.dev.yml exec devcontainer"
noninteractive_prefix = "podman-compose -f docker-compose.dev.yml exec -T devcontainer"
```

`interactive_prefix` wraps every window command launched in this backend's environment (kitty shells, the editor, browser). The built-in shell and editor (`${SHELL:-sh}` and `nvim`) automatically get the prefix prepended — you don't need to declare per-role commands here unless you want to override them.

`noninteractive_prefix` is the prefix hop uses internally for non-interactive backend operations — currently the file-existence check that drives the open-selection kitten's highlight filter. It's required for every backend; for podman-compose the no-TTY variant is necessary (`exec -T <service>`) because the default `exec` allocates a TTY and eats hop's stdin pipe to the loop, causing the kitten to report nothing as existing.

Each command is a single string. Hop runs it through `sh -c` after substituting placeholders, so pipes, redirects, and `$(...)` work — write the value the way you'd type it at a terminal. Triple-quoted strings (`"""…"""`) let you spread a longer pipeline across lines for readability. Placeholder values (`{project_root}`, `{port}`) are shell-quoted before insertion, so paths with spaces substitute safely.

`port_translate` is invoked lazily when the kitten dispatch encounters a URL like `http://localhost:3000` printed inside the container — the recipe above resolves the running container by compose label (so it works whether the container was brought up by `podman-compose up` or by `podman-compose run …` with their different naming conventions) and asks `podman port` for the host-side port the container's port is published on. Stripped stdout (e.g. `35231`) replaces the URL's port; the host (`localhost`) is left untouched. The companion `host_translate` field exists for backends that swap the hostname instead — not needed for a same-host devcontainer.

If you use a different compose tool, swap the prefix:

```toml
prepare = "docker compose -f docker-compose.dev.yml up -d devcontainer"
# ... etc.
```

`activate` is the auto-detect probe — hop runs it in the project root and picks this backend if it exits 0. Any command works; `test -f <marker>` is the simplest. Backends without `activate` aren't eligible for auto-detect; they can only be picked by name with `hop --backend <name>` or `[backend].name = "<name>"` in `.hop.toml`.

### 3. Verify

From a project root that has `docker-compose.dev.yml`:

```bash
hop
```

You should see:

- `podman ps` (or `docker ps`) shows a new container for the service.
- A kitty window opens whose prompt is inside `/workspace` (or whichever `working_dir` your compose service sets).
- `hop edit` opens an in-container nvim; `:lua print(vim.fn.getcwd())` shows the container path.

You can verify the persisted state:

```bash
cat $XDG_RUNTIME_DIR/hop/sessions/<project_name>.json
```

The `backend.interactive_prefix` and `backend.noninteractive_prefix` fields are what hop wraps around every kitty launch and every non-interactive backend call (like the kitten's path-existence check).

## Project config

`<project_root>/.hop.toml` uses **the same schema** as the global file — backends, layouts, and top-level windows are all parseable in either place. Drop in whatever subset of sections you want. Hop merges the two files when resolving:

- Project entries come first in auto-detect / declaration order.
- Same-named entries are field-merged with project fields winning. The merged entry takes the project's slot.

A single project file can override one field of a global backend, force/skip a backend via its `activate` probe, declare project-specific layouts and windows, and define a brand-new backend — there is no syntactic distinction between these uses:

```toml
# Override one field of a global backend (this project's compose service is named "app").
[backends.devcontainer]
interactive_prefix = "docker compose -f compose.dev.yml exec app"

# Force a backend to win auto-detect in this project.
# [backends.devcontainer]
# activate = "true"

# Or skip a backend by overriding its activate probe to fail.
# [backends.other]
# activate = "false"

# A project-specific layout (e.g. one Rails project with a worker process).
[layouts.this-project]
activate = "true"

[layouts.this-project.windows.worker]
command = "bin/jobs"

# A top-level window that's always active in this project.
[windows.log]
command = "tail -f log/development.log"

# Define a project-specific backend that doesn't exist globally.
[backends.my-vm]
activate                      = "test -f .my-vm-marker"
interactive_prefix                = "lima shell default --"
noninteractive_prefix = "lima shell default --"
```

To force the host backend for a project, pass `hop --backend host` once at session creation (persisted), or override every configured backend's `activate` to a failing command.

## Per-invocation override: `--backend <name>`

`hop --backend <name>` creates the session with the named backend. Use `hop --backend host` to force the host backend in a project that would otherwise auto-activate something else. The choice is persisted in session state:

```bash
cd ~/projects/foo
hop --backend host        # host shell, no prepare runs
hop term --role test      # still a host shell — read from persisted state
hop kill                  # no teardown runs
```

The flag is only valid on bare `hop` (or `hop term` without `--role`, which is the same entry point). Other subcommands reject it. Passing a backend name that isn't configured (and isn't `host`) errors out.

## Tools-managed `$PATH` inside the container (mise / asdf / rbenv / nvm / direnv)

`compose exec devcontainer <cmd>` runs `<cmd>` in a **non-login, non-interactive** shell — `.bashrc`, `.profile`, mise/asdf hooks, and direnv hooks do **not** fire. Anything those normally inject into `$PATH` (`gem`, `rake`, `bundle`, `node`, `npm`, language SDKs, `direnv`-exported env vars, etc.) is missing.

Symptoms: a window declared as `command = "bin/dev"` prints `gem: command not found` / `exec: foreman: not found`, then drops into the post-exit shell where everything works because **that** shell is interactive and triggers the activation.

Wrap any tool-dependent command in a login shell:

```toml
[layouts.rails.windows.server]
command = "bash -lc bin/dev"
```

`bash -l` sources the container user's profile, mise/asdf activate, and `bin/dev` runs with the expected `$PATH`. For multi-word commands, single-quote the inner script so it stays one argument:

```toml
[layouts.rails.windows.console]
command = "bash -lc 'bin/rails console'"
```

If you want to honor the container user's actual login shell instead of hard-coding bash, use `$SHELL` — but it must be expanded **inside** the container, not on the host. Wrap the inner command in single quotes so host sh keeps `$SHELL` literal:

```toml
[layouts.rails.windows.server]
command = "sh -c '$SHELL -lc bin/dev'"

[layouts.rails.windows.console]
command = "sh -c '$SHELL -lc \"bin/rails console\"'"
```

The single-quote dance is mandatory — without it, `$SHELL` expands to the **host's** `$SHELL`, which usually doesn't exist at the same path inside the container. `$SHELL` in the container reflects the container user's `/etc/passwd` entry, so this only buys portability if the image was built with your preferred shell as the user's login shell. Otherwise `bash -lc` is just as accurate and avoids the quoting trap.

## Mental model: one container, many terminals

Each session corresponds to **one** instance of the backend. For a compose-based devcontainer, that's one container, with all shells `compose exec`-ing into it:

```
[host]
  └── kitty (session=foo)
        ├── shell      ──► podman compose exec devcontainer zsh ──┐
        ├── shell-2    ──► podman compose exec devcontainer zsh ──┼─ same container
        ├── test       ──► podman compose exec devcontainer zsh ──┤
        ├── server     ──► podman compose exec devcontainer zsh ──┤
        └── editor     ──► podman compose exec devcontainer nvim ─┘
```

So starting `bin/dev` in `server` and `curl localhost:3000` from `shell` works exactly as if everything ran on the host — same as `tmux` panes attached to a remote host.

## Troubleshooting

### Kitty windows flash open and immediately close

The container's PID 1 exited right after `compose up -d`, so by the time hop tries `compose exec` there's nothing to attach to. Most common cause: the service has an interactive entrypoint (e.g. `bash -lc '...'`) but no TTY allocated, so it reaches EOF immediately. Add to the service:

```yaml
tty: true
stdin_open: true
```

Verify with `podman ps` — if `STATUS` shows `Exited (0)` shortly after `hop`, that's the fingerprint. With `debug_log = true`, the bootstrap log will show `prepare` succeeding but no further `compose exec` output, since the container died after `prepare`.

### `hop` fails silently (especially from Vicinae)

When `hop` is launched from a Vicinae action, its stderr is discarded; even from a terminal, kitty's bootstrap shell child runs detached with stdout/stderr sent to `/dev/null` by default. Turn on the debug log to see what's happening:

```toml
# ~/.config/hop/config.toml
debug_log = true
```

That appends every backend lifecycle command (cmd, exit, stdout, stderr) and the kitty bootstrap launcher's argv + stdio to `$XDG_RUNTIME_DIR/hop/debug.log` (or set `debug_log = "/some/path"` for a custom path). Re-run the failing `hop` invocation, then `tail -n 200 $XDG_RUNTIME_DIR/hop/debug.log`.

### `hop edit` opens nvim but file-open calls do nothing

Hop drives the in-container nvim by writing keystrokes (`<C-\><C-n>:exec 'drop '.fnameescape(...)<CR>`) into the kitty window's pty. If the dispatch doesn't open the file, the most common causes are:

- A startup plugin (dashboard, intro screen, or auto-opened file picker) is intercepting the cmdline before nvim processes hop's keystrokes. Try opening a file via `hop edit <path>` after nvim has fully loaded — if it works then but not at first launch, that's the smoking gun.
- The session's editor window isn't where hop thinks it is. Check `swaymsg -t get_tree | grep -A2 hop:editor` to confirm the window's `app_id`/`class` and Sway mark match the session.

### `prepare` failed but the kitty window opened anyway

`prepare` failures bubble up as a `SessionBackendError` and abort the bootstrap before kitty is launched, so this shouldn't happen. If it does, file an issue with the failure output — `hop` runs `prepare` synchronously and only proceeds on a clean exit.

### `hop kill` left the container running

`hop kill` closes kitty windows first (sending SIGHUP to in-backend shells), then runs `teardown`. If a `teardown` failure aborts halfway, the resource it manages survives. Re-run `hop kill` from the same project root, or run the `teardown` command directly.

### Open-selection kitten can't open files from terminal output

The kitten asks the focused session's backend "do these paths exist?" through `noninteractive_prefix`. If files in the kitten's match list aren't highlighted (or don't dispatch), check:

- `$XDG_RUNTIME_DIR/hop/open-selection.log` for the dispatch line.
- With `debug_log = true`, the bootstrap log shows the synthesized `paths_exist` invocation. For `podman-compose`, you should see `... exec -T devcontainer sh -c 'while IFS= read -r p; ...'`. Missing `-T` is the most common pitfall — without it, podman-compose allocates a TTY and the stdin pipe is eaten.

### Open-selection kitten opens the wrong URL (or hits a dead port)

When the kitten dispatches `http://localhost:3000` from inside a container, hop runs the backend's `port_translate` (and/or `host_translate`) command to rewrite the URL into something the host browser can reach. If the browser opens an unexpected page, an empty error, or hits the host's port instead of the container's, check `$XDG_RUNTIME_DIR/hop/open-selection.log` for the dispatch line — it shows the URL hop actually handed to the browser, after translation. Common causes: the recipe's container filter matches zero containers (so `podman port` is invoked with an empty argument and fails), the requested port isn't published on the container, or the recipe matches the wrong container when more than one is running for the same service.

If `port_translate` and `host_translate` are both omitted, hop leaves localhost URLs unchanged — the host browser will then talk to whatever is bound on the host's side of those ports, which is unrelated to the container's services.

### Auto-detect picked the wrong backend

Auto-detect walks `[backends.<name>]` tables in order (project entries first, then global ones) and picks the first whose `default` probe exits 0. If two backends both match a project, reorder them in `~/.config/hop/config.toml` (declaration order is the priority within a file), pin one with `hop --backend <name>`, or skip one in `.hop.toml` by setting its `default` to `["false"]`.

### `podman-compose` rejects `include: [{path: ...}]`

Use the plain-string form. See the "One-time setup" section above.
