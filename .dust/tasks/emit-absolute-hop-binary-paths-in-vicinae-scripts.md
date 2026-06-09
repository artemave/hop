# Emit absolute hop binary paths in vicinae scripts

Make `hopd` write absolute `hop`/`hopd` paths into every generated vicinae script instead of bare names. Bare names fail under the minimal PATH Sway/vicinae inherit.

## Background

Sway is launched by GDM with a stripped login PATH (`/usr/local/bin:/usr/bin:/var/lib/snapd/snap/bin`) that does not include `~/.local/bin`, where `hop` and `hopd` are installed. That PATH is inherited by everything Sway spawns: `vicinae-server`, and therefore every vicinae script vicinae runs.

The generated `hop-*` scripts (`hop/vicinae.py`) all invoke `hop` / `hopd` by bare name:

- `_window_script` → `exec setsid -f hop term --role <role>` / `exec setsid -f hop browser`
- `_kill_script` → `exec hop kill`
- `_switch_script` → `exec setsid -f hop switch <name>`
- `_create_script` → `exec setsid -f hop`
- `_move_script` → `candidates=$(hop list)` and `exec setsid -f hop move "$chosen"`
- `write_daemon_down_script` → `exec setsid -f hopd`

Under the inherited PATH these resolve to "command not found", so clicking a vicinae entry silently does nothing. `vicinae`, `setsid`, `bash`, `find`, etc. live in `/usr/bin` and stay reachable, so only the `hop` / `hopd` invocations need absoluting.

The companion environment fix (adding `~/.local/bin` to the session PATH via `environment.d`) was deliberately rejected in favor of absolute paths, so the scripts must not depend on PATH for the hop binaries.

## Design

### Resolving the binary directory

`hopd` knows where it was launched from: `sys.argv[0]`. The `hop` and `hopd` console scripts are installed side by side in the same bin directory, so:

```python
entry_dir = Path(sys.argv[0]).resolve().parent
hop_bin = str(entry_dir / "hop")
hopd_bin = str(entry_dir / "hopd")
```

`resolve()` makes a relative invocation (`./hopd`) absolute and follows a symlinked entry point into the real install dir (e.g. a `uv tool` venv), where the sibling `hop` also lives. This is the "relative to hopd" resolution.

### Threading the path (explicit DI, matching the module's style)

`hop/vicinae.py` already takes its collaborators explicitly (`windows_for`, `sessions_loader`, `sway`). Add the binary paths the same way rather than reading `sys.argv[0]` deep inside a builder — that keeps `compute_target_scripts` deterministic and unit-testable:

- `compute_target_scripts(focused_workspace, sessions, *, windows_for, hop_bin: str)` — threads `hop_bin` to `_window_script`, `_kill_script`, `_switch_script`, `_create_script`, `_move_script`.
- `regenerate(*, sway, sessions_loader, scripts_dir, windows_for, hop_bin: str)` — forwards `hop_bin`.
- `write_daemon_down_script(scripts_dir, *, error, hopd_bin: str)` — uses `hopd_bin` in the restart line.

The builders substitute the absolute path wherever they emit `hop` / `hopd` today. `shlex.quote` the path in case the install dir contains spaces.

### Daemon wiring

`hop/daemon.py` computes `hop_bin` / `hopd_bin` once from `sys.argv[0]` in `main()` and passes them into the `regenerate(...)` calls and into `_signal_daemon_down` (which forwards to `write_daemon_down_script`). The load-config error path (which calls `_signal_daemon_down` before Sway setup) gets the same `hopd_bin`.

### Sway config (user dotfiles, separate from the package)

`~/projects/dotfiles/.config/sway/config` line 45 `exec hopd` becomes `exec ~/.local/bin/hopd` so the daemon itself starts at boot without relying on PATH. This is a dotfiles edit, tracked separately from the hop package change; it is listed here for completeness, not as a package deliverable.

### term-or-kitty

`hop/sway/term-or-kitty` (bound to `$mod+Return`) has the same bug — it calls bare `hop list` / `hop term` under Sway's PATH — but its plain-kitty fallback masks it (you get a kitty, never a session shell). It resolves hop relative to itself by running the source as a module: the script ships inside the package at `<root>/hop/sway/term-or-kitty`, so the import root is two levels up. It invokes `env PYTHONPATH=<root> python3 -m hop` (`root=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)`), which depends on neither a console-script entry point being on PATH nor the gitignored uv `.venv` existing. `python3` and `/usr/bin` are always on Sway's PATH; the `$mod+Return` binding needs no change.

## Task Type

implement

## Principles

- [Prefer native integrations](../principles/prefer-native-integrations.md)

## Blocked By

(none)

## Definition of Done

- `compute_target_scripts` and `regenerate` take a `hop_bin` parameter; every generated window/kill/switch/create/move script invokes the `hop` binary by that absolute, shell-quoted path instead of bare `hop`.
- `write_daemon_down_script` takes a `hopd_bin` parameter and emits the restart line with that absolute path.
- `hop/daemon.py` derives both paths from `sys.argv[0]` (`Path(sys.argv[0]).resolve().parent`) and passes them through `regenerate` and the daemon-down path.
- `vicinae`, `setsid`, and other `/usr/bin` tools remain bare — only `hop` / `hopd` are absoluted.
- `tests/test_vicinae.py` passes an explicit `hop_bin` / `hopd_bin` and asserts the generated scripts contain the absolute path (and no bare `hop`/`hopd` invocation), covering window, kill, switch, create, move, and daemon-down scripts.
- `hop/sway/term-or-kitty` runs hop relative to its own location via `env PYTHONPATH=<root> python3 -m hop` instead of calling bare `hop`, so `$mod+Return` works under Sway's PATH with no binding change and no dependency on the gitignored `.venv`.
- `make` is green.
