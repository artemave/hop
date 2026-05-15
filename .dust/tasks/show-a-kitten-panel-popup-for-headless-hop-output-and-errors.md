# Show a kitten-panel popup for headless hop output and errors

Surface hop's `prepare`/`teardown` output and unhandled errors through a `kitten panel` overlay when stderr is not a TTY; hold open on failure.

## Background

Every `hop` invocation from vicinae (and other detached launchers) loses stdio because vicinae's UI close tears down the controlling TTY before `setsid -f hop` finishes detaching. `default_runner` (`hop/backends.py:81`) captures subprocess stderr (`stderr=subprocess.PIPE` when `sys.stderr.isatty()` is false), and `cli.main`'s `HopError` catch (`hop/cli.py:125`) prints to a stderr nobody is watching. Three symptom-classes follow:

- **Lifecycle progress is invisible.** Both flows where hop runs a slow host-side subprocess have no UI:
  - *Create.* Picking **Hop create session** dispatches `cd "$HOME/$chosen" && exec setsid -f hop` (`_create_script` in `hop/vicinae.py:202`). The detached `hop` calls `SessionBackendRegistry.resolve_for_entry` (`hop/app.py:163`) → `backend.prepare(session)` — potentially a slow `compose up -d devcontainer`. Only afterwards does `enter_project_session` (`hop/commands/session.py:57`) switch workspace and bootstrap kitty, so the user sees the *previous* workspace frozen for the duration.
  - *Kill.* Picking **Hop kill** dispatches `setsid -f bash -c '... cd <root>; exec hop kill'` (`_render_kill` in `hop/vicinae.py:303`). `kill_session` (`hop/commands/kill.py:81`) closes windows, then calls `backend.teardown(session)` — potentially a slow `compose down`. The session windows are gone but the user has no indication teardown is still running.
- **Lifecycle errors are invisible.** `SessionBackendError` propagates up; the `HopError` branch prints to /dev/null. Prepare fails → user is left on their old workspace with no clue why nothing happened. Teardown fails → containers stay up with no surfaced error.
- **Every *other* `HopError` is invisible too.** It's not only lifecycle commands. A headless `hop --backend devcontainer` against a project with no matching backend raises `UnknownBackendError`; `hop switch nonexistent` raises `HopError("No active session named ...")`; a sway IPC failure raises `SwayConnectionError`; a kitty bootstrap timeout raises `KittyConnectionError`. From vicinae or any other detached caller, all of these die silently the same way the lifecycle errors do.

The fix is one popup surface — a `kitten panel` layer-shell overlay — used for both progress streaming (lifecycle commands) and error display (everything else):

- **Lifecycle popup**: spawned at the `EnterSessionCommand` / `KillCommand` call sites; runs `prepare` / `teardown` inside the panel so output streams live; closes on success; holds open with `exec sh` on failure.
- **Error popup**: spawned by `cli.main`'s `HopError` catch when stderr is non-TTY and the error wasn't already surfaced by a lifecycle popup; prints the error message and holds open with `exec sh` so the user reads it before dismissing.

The trigger for both is the same signal `default_runner` already keys on: `sys.stderr.isatty()`. When stderr is a TTY (interactive `hop` from a kitty window or any shell), lifecycle output streams to that terminal and errors print to it — adding a popup there would be noise. When stderr is *not* a TTY (vicinae, sway keybindings, `nohup hop &`), no terminal is watching, so hop shows the popup. The same auto-detect covers every headless launch path, not just vicinae.

`EnterSessionCommand` additionally switches sway to `p:<session>` *before* the lifecycle popup runs so the user lands on the session-to-be while prepare streams.

## Design

### Trigger

In `hop/app.py::execute_command`:

**`EnterSessionCommand` first-entry path** splits on `services.popup.is_interactive()`:

- **Interactive.** Byte-for-byte today: `resolve_for_entry` runs prepare inline (output streams to the inherited TTY); `enter_project_session` then switches workspace and bootstraps kitty.
- **Headless.** New branch:
  1. `services.session_backends.resolve_for_entry(..., skip_prepare=True)` selects the backend without preparing.
  2. `services.sway.switch_to_workspace(session.workspace_name)` — eager switch so the popup lands on `p:<session>`.
  3. `services.popup.run_prepare(session, backend)` — launches the popup, blocks until it exits. Returns on success; raises `SessionBackendError` on failure.
  4. `set_override` / `enter_project_session` run as today. The workspace switch inside `enter_project_session` is now a no-op.

**`KillCommand`** splits on the same `is_interactive()`:

- **Interactive.** Byte-for-byte today: `kill_session` closes windows, waits, then calls `backend.teardown(session)` inline.
- **Headless.** `kill_session` accepts a new `teardown_runner: Callable[[ProjectSession, SessionBackend], None]` keyword argument (defaulting to `lambda s, b: b.teardown(s)`). The `KillCommand` arm in `execute_command` passes `services.popup.run_teardown` when headless, which runs teardown inside the popup. Window-close ordering is unchanged (still happens before teardown) — only the teardown step is delegated.

**`cli.main`'s `HopError` catch** — catch-all for everything else. When `services.popup.is_interactive()` is false AND the error is not already `surfaced_by_popup`, `cli.main` invokes `services.popup.show_error(error)` before returning 1. Interactive callers still see the existing `print(str(error), file=sys.stderr)` (the stderr print stays unconditional — it's harmless when no terminal watches it, and useful in any captured-stderr context like logs). The popup adds a visible surface; it doesn't replace stderr.

The TTY check is encapsulated in the popup adapter, not inline in `cli.main` / `execute_command`, so tests can inject the answer deterministically.

### HopPopup adapter

A new `HopPopup` Protocol on `HopServices` owns both the "is the caller headless?" decision and the popup mechanics:

```python
class HopPopup(Protocol):
    def is_interactive(self) -> bool: ...
    def run_prepare(self, session: ProjectSession, backend: SessionBackend) -> None: ...
    def run_teardown(self, session: ProjectSession, backend: SessionBackend) -> None: ...
    def show_error(self, error: HopError) -> None: ...
```

`run_prepare` and `run_teardown` either return normally (popup exited 0 → command succeeded) or raise `SessionBackendError` with `surfaced_by_popup=True` (popup exited non-zero → command failed and the user has already seen the failure inside the panel). `show_error` is fire-and-forget — it blocks until the user dismisses the panel, returns nothing, raises nothing. `is_interactive` is read once per call site and gates whether each method is invoked at all.

The production implementation, `KittyHopPopup`, lives in a new `hop/popup.py`:

1. **TTY check.** `is_interactive()` returns `self._stderr_isatty()`. The check is constructor-injectable (`stderr_isatty: Callable[[], bool] = lambda: sys.stderr.isatty()`) so tests can flip it.
2. **Skip when nothing to run.** If `backend.prepare_command` (resp. `backend.teardown_command`) is `None`, the matching method is a no-op and returns immediately — no popup, no flicker. The interactive paths preserve today's "host backend has no prepare/teardown → noop" behavior; the headless paths match that semantics.
3. **Spawn the popup as a layer-shell panel.** Shared internal `_launch(session, *, title, command_str, kind)`:
   ```python
   self._launcher([
       "kitten", "panel",
       "--edge=center", "--columns=100", "--lines=24",
       "--layer=overlay", "--focus-policy=on-demand",
       "--app-id=hop:popup",
       "--title", title,
       "--", "sh", "-c", _popup_script(session, command_str, kind=kind),
   ])
   ```
   `kitten panel` opens a fresh kitty process whose surface is a `wlr-layer-shell` overlay (above all normal toplevels, not part of sway's tiling). The panel is a one-shot UI process; it is not the session's kitty. `--edge=center` + `--columns`/`--lines` size it; `--layer=overlay` puts it above normal windows; `--focus-policy=on-demand` lets the user click in to scroll / dismiss the held shell on failure without stealing focus while the command runs.
4. **Wait for it.** `proc.wait()` blocks hop. Exit code 0 means the inner shell exited cleanly (command succeeded and the wrapper script returned). Anything else means either the user closed the panel (after reading the error, by Ctrl-D'ing the held shell — which triggers panel teardown) or `kitten panel` itself crashed — both treated as "command did not succeed", and `SessionBackendError` is raised with a kind-specific message ("session prepare did not succeed" / "session teardown did not succeed").
5. **No working-directory flag.** `kitten panel` does not accept `--directory`; the lifecycle command's cwd is set inside the wrapper script via `cd <project_root>` before the `flock` invocation. Matches how `CommandBackend.prepare` / `teardown` already run (`runner(argv, cwd=session.project_root)`).
6. **Same `app_id` across kinds.** Lifecycle popups (prepare, teardown) and error popups all carry `app_id="hop:popup"`. Lifecycle popups never overlap with each other (prepare at create, teardown at kill) and never overlap with their own error popup (the error is already surfaced inside the lifecycle panel — see "Avoiding double popups" below). A single app id keeps any user-side compositor rule (if they want one) trivial. The title (`Preparing <name>` / `Tearing down <name>` / `Hop: error`) is the only per-kind UI signal.
7. **Error popup.** `show_error(error)` launches `kitten panel` with the same flags and a one-shot wrapper that prints the formatted error and execs `sh`:
   ```sh
   set -u
   printf 'hop: %s\n\n' "<error_text>"
   printf 'Press Ctrl-D to close.\n'
   exec sh
   ```
   `<error_text>` is `f"{type(error).__name__}: {error}"` so the user sees both the class (e.g. `UnknownBackendError`) and the message. No flock, no cwd, no command execution — the popup is purely a viewer. `show_error` blocks until the user dismisses the panel (so `cli.main` doesn't exit before the user reads); on dismissal it returns regardless of the panel's exit code (the user closing the panel IS the success signal — there's no failure mode for "show a message").

### Popup wrapper script

`_popup_script(session, command_str, *, kind: Literal["prepare", "teardown"])` produces the body the panel's `sh -c` runs. The wrapper preserves the flock serialization that `_flock_sh` (`hop/backends.py:440`) uses — so a popup-run command cannot race the other lifecycle direction (e.g. a popup `compose up` cannot race a still-running detached `compose down` from an earlier `hop kill`):

```sh
set -u
cd "<project_root>"
printf '%s %s\n' "<kind_verb>" "<session_name>"
printf '$ %s\n\n' "<command_str>"
flock -o "<lock_path>" sh -c "<substituted_command>"
status=$?
if [ "$status" -eq 0 ]; then
    exit 0
fi
printf '\n%s failed (exit %d). Press Ctrl-D to close.\n' "<kind_noun>" "$status"
exec sh
```

- `<kind_verb>` is `Preparing` or `Tearing down`; `<kind_noun>` is `prepare` or `teardown`.
- `<project_root>`, `<session_name>`, `<command_str>`, `<lock_path>`, `<substituted_command>` are inserted via `shlex.quote` to keep paths and metacharacters safe.
- `<substituted_command>` is the lifecycle command string with `{project_root}` already substituted — same string `CommandBackend.prepare`/`teardown` would have passed to `flock -o ... sh -c`. The popup reproduces that path; it does NOT call `backend.prepare()` / `backend.teardown()` (which would re-capture stdio).
- `exec sh` on failure replaces the wrapper with an interactive shell so the user can read the error, scroll back, and dismiss on their own. When the user Ctrl-Ds the shell (or closes the panel via the compositor's close binding), the layer-shell surface tears down and the `kitten panel` process exits; `proc.wait()` then surfaces a non-zero exit, which the adapter translates into `SessionBackendError`.

### Avoiding double popups

A lifecycle popup that fails already shows the error inline (the held-open `sh` after `prepare failed`/`teardown failed`). When `run_prepare` / `run_teardown` raises, the resulting `SessionBackendError` propagates to `cli.main`'s `HopError` catch — which would naively pop a second panel with the same message.

The fix is a marker attribute on `HopError`:

```python
class HopError(Exception):
    def __init__(self, *args: object, surfaced_by_popup: bool = False) -> None:
        super().__init__(*args)
        self.surfaced_by_popup = surfaced_by_popup
```

`KittyHopPopup.run_prepare` / `run_teardown` raise `SessionBackendError(message, surfaced_by_popup=True)`. `cli.main` checks the flag:

```python
except HopError as error:
    print(str(error), file=sys.stderr)
    if not error.surfaced_by_popup and not services.popup.is_interactive():
        services.popup.show_error(error)
    return 1
```

So the user sees exactly one popup per failure: the lifecycle one when prepare/teardown fails, the catch-all one when anything else fails. Subclasses that want to suppress the error popup (e.g. `KeyboardInterrupt`-derived flows) can set `surfaced_by_popup=True` at raise time without further plumbing.

### Why `kitten panel` instead of a floating OS window

`kitten panel` uses the `wlr-layer-shell` protocol to render kitty as an overlay surface, distinct from a regular xdg-shell toplevel:

- **No user sway config required.** Layer-shell placement is controlled entirely by the kitten flags (`--edge`, `--columns`, `--lines`, `--layer`). Users don't need to add a `for_window [app_id="hop:popup"] floating enable, ...` rule — the popup is positioned correctly out of the box.
- **Compositor-portable.** Any wlr-layer-shell compositor (sway, hyprland, river, miracle-wm) handles `kitten panel` the same way.
- **Truer popup semantics.** Layer-shell `overlay` layer renders above all normal toplevels, doesn't reflow the workspace tiling, doesn't appear in alt-tab. The popup is a transient overlay, not a window the user might accidentally treat as a regular kitty session.
- **Survives workspace destruction.** Critical for the kill path: layer-shell surfaces are tied to outputs (monitors), not workspaces, so the teardown popup stays visible after `hop kill` closes the session's last window and sway snaps to the previous workspace.
- **No `con_id` polling.** Hop doesn't need to find the popup in sway's window tree to apply floating / sizing — those properties are set declaratively at launch time.

Trade-off: requires kitty ≥ 0.34 (when `kitten panel` got `--edge=center` and `--layer=overlay`). Earlier versions only supported edge-anchored panels. The README's prerequisites bumps the minimum.

### Second silent prepare in bootstrap

`_bootstrap_session_kitty` (`hop/kitty.py:330`) still calls `backend.prepare(session)` on cold-bootstrap. On the headless create path this is the second invocation of the same command — already idempotent by spec (`hop_spec.md:83`), so it's effectively a fast cache-hit (`compose up -d` against running containers is sub-second). Reworking the bootstrap to also skip its prepare call is out of scope; this task is about the lifecycle popup UI, not about eliminating the bootstrap-time idempotent re-execution.

### What the auto-detect does *not* do

- It does not change behavior when stderr is a TTY: every flow from a real terminal still streams lifecycle output and prints errors to that terminal — byte-for-byte unchanged.
- It does not change behavior for re-entry from another workspace (kitty already alive; no prepare runs in either flow).
- It does not change behavior for the spawn-another-shell branch.
- It does not change the existing `print(str(error), file=sys.stderr)` in `cli.main` — the popup is additive, not a replacement. Captured-stderr callers (CI, log pipelines) still see the error in stderr.
- It does not remove the second silent prepare inside `_bootstrap_session_kitty`. Idempotent by spec.
- It does not require any sway-specific window rules. `kitten panel` flags fully describe the popup's appearance via wlr-layer-shell.

## Files to change

- `hop/app.py`:
  - `HopServices` gains `popup: HopPopup`.
  - `execute_command`'s `EnterSessionCommand` arm: on the first-entry branch, consult `services.popup.is_interactive()`. Interactive → existing flow unchanged. Headless → `resolve_for_entry(skip_prepare=True)` → eager `switch_to_workspace` → `services.popup.run_prepare(session, backend)` → `set_override` → `enter_project_session`.
  - `execute_command`'s `KillCommand` arm: pass `services.popup.run_teardown` as the new `teardown_runner` keyword to `kill_session` when `is_interactive()` is false; otherwise let `kill_session` default to inline `backend.teardown`.
  - `build_default_services` / `build_kitten_services` wire `KittyHopPopup` into `HopServices`.
  - `SessionBackendRegistry.resolve_for_entry` accepts `skip_prepare: bool = False` and skips the `backend.prepare(session)` call when set. `probe_workspace_path` still runs (it depends on prepare having completed, which the popup guarantees on the headless path; on the interactive path the inline prepare already ran).
- `hop/commands/kill.py`:
  - `kill_session` accepts `teardown_runner: Callable[[ProjectSession, SessionBackend], None] | None = None`. When `None`, the existing `backend.teardown(session)` call site runs unchanged. When supplied, the call site delegates to `teardown_runner(session, backend)` instead. `forget(session.session_name)` still runs only on successful return from teardown (matches today's flow — `SessionBackendError` short-circuits forget).
- `hop/popup.py` (new):
  - `HopPopup` Protocol (`is_interactive`, `run_prepare`, `run_teardown`, `show_error`).
  - `KittyHopPopup` with `launcher: Callable[[Sequence[str]], subprocess.Popen[bytes]] | None = None` and `stderr_isatty: Callable[[], bool] | None = None` constructor hooks for tests.
  - `_lifecycle_script(session, command_str, *, kind)` helper for prepare/teardown popups. Reuses `_backend_lock_path` and `_substitute` from `hop/backends.py` — export those (lift the leading `_` or expose them through a small public helper) so the popup script can match `_flock_sh`'s shape without duplicating the substitution / locking logic.
  - `_error_script(error)` helper for the error popup. One-shot: `printf 'hop: <error_text>' + Press Ctrl-D + exec sh`.
- `hop/errors.py`:
  - `HopError.__init__` accepts a keyword-only `surfaced_by_popup: bool = False`. The flag is stored on the instance. Existing call sites unchanged.
- `hop/cli.py`:
  - `main`'s `HopError` catch invokes `services.popup.show_error(error)` when `not error.surfaced_by_popup and not services.popup.is_interactive()`. The existing `print(str(error), file=sys.stderr)` is unchanged.
- `hop_spec.md`:
  - Under "CLI behavior" → "Enter session", add: "When invoked without a controlling TTY (e.g. from vicinae's detached `setsid -f hop`, a sway keybinding, or a launcher script), bare `hop` shows a `kitten panel` overlay (`app_id="hop:popup"`) streaming the backend's `prepare` output while the session is being created. On prepare failure the panel stays open at a held shell so the user can read the error; on success it closes and the normal kitty / editor bootstrap proceeds. From an interactive terminal, prepare output streams to that terminal as today."
  - Under "CLI behavior" → "Kill session", add the symmetric paragraph for teardown: "Headless invocations (vicinae's `hop-kill` script) show the same `kitten panel` overlay streaming `teardown` output after session windows have closed. Teardown failure leaves the popup open at a held shell; the session's persisted state file is not removed (matching today's "teardown failure short-circuits forget" behavior)."
  - New top-level "Error display" subsection (or paragraph under "CLI behavior"): "Any `HopError` raised during a headless invocation also surfaces through a `kitten panel` overlay (same `app_id="hop:popup"`) so the user sees what went wrong. Errors already surfaced by a lifecycle popup (`surfaced_by_popup=True`) are not re-shown."
  - Prerequisites section: kitty ≥ 0.34 (for `kitten panel --edge=center --layer=overlay`).
- `README.md` — bump the kitty minimum version in the prerequisites section to ≥ 0.34 and add a one-line explanation of the popup: "If you launch `hop` or any of its subcommands headlessly (vicinae, a sway keybinding, a launcher script), it shows a centered layer-shell overlay for `prepare` / `teardown` output and for any unhandled error; from a terminal the output streams to that terminal." No sway window rule needed.
- `hop/vicinae.py` — no change. The existing `exec setsid -f hop` / `exec hop kill` paths already produce the headless invocations the popup keys on. The comments that explain the `setsid -f` rationale stay accurate (vicinae's SIGTERM-on-close is still the reason for `setsid`; the popup is now the lifecycle UI that `setsid` enables by detaching from vicinae's stdio).

## Tests

Real behavior, no mocks (per project convention).

- `tests/test_popup.py` (new):
  - `KittyHopPopup.is_interactive` returns whatever the injected `stderr_isatty` callable returns. Default factory binds to `sys.stderr.isatty`.
  - `KittyHopPopup.run_prepare` with `backend.prepare_command is None` returns without invoking the launcher (no popup for backends without prepare). Symmetric case for `run_teardown` with `backend.teardown_command is None`.
  - `run_prepare` invokes the launcher with argv whose first two elements are `["kitten", "panel"]`, whose flags include `--edge=center`, `--layer=overlay`, `--focus-policy=on-demand`, `--app-id=hop:popup`, and `--title Preparing <name>`, and whose trailing `sh -c <script>` contains a `cd <project_root>`, `flock -o <lock_path>`, and the substituted prepare command. Launcher fake records argv and returns a `Popen`-like stub whose `wait()` returns 0; the call returns normally.
  - `run_teardown` invokes the launcher with the symmetric argv (`--title Tearing down <name>`, the substituted teardown command).
  - Launcher fake's `wait()` non-zero → `run_prepare` raises `SessionBackendError(..., surfaced_by_popup=True)` referencing the session name and the word "prepare"; `run_teardown` raises with "teardown" in the message and the same flag.
  - `show_error(error)` invokes the launcher with `--app-id=hop:popup`, `--title Hop: error`, and a `sh -c` script that contains `hop: <type>: <message>`, `Press Ctrl-D to close`, and `exec sh`. The call blocks until `wait()` returns and returns regardless of exit code (no raise even on non-zero wait).
  - `_lifecycle_script(kind="prepare")` output: `cd <project_root>`, `Preparing <name>`, `$ <command>`, `flock -o <lock_path> sh -c <substituted>`, `exit 0` on success, `prepare failed (exit <n>)` + `exec sh` on failure.
  - `_lifecycle_script(kind="teardown")` output: same shape, with `Tearing down <name>` / `teardown failed (exit <n>)` in the verbs.
  - `_lifecycle_script` survives commands containing single quotes / backslashes / `$(...)`: assert quoting via `shlex.quote` round-trips correctly.
  - `_error_script` survives error messages containing single quotes / backslashes / newlines: the message is `shlex.quote`d and renders verbatim.

- `tests/test_app.py`:
  - Extend `StubHopServices` with a `StubHopPopup` that exposes a configurable `is_interactive` bool and records `(kind, session_name, command_str)` calls on `run_prepare` / `run_teardown`. Each method is independently configurable to either return or raise `SessionBackendError`.
  - **Create / headless / success:** `execute_command(EnterSessionCommand(), ...)` with `StubHopPopup(is_interactive=False)` against a devcontainer-style backend:
    - `services.sway.switched_workspaces` ends with `p:<session>` *before* `run_prepare` runs (call-order assertion on the stubs).
    - `run_prepare` recorded one call with the session name and the backend's prepare command.
    - The runner stub backing `SessionBackendRegistry` saw zero `flock`/`prepare` invocations (`resolve_for_entry` skipped prepare); the kitty bootstrap-side prepare still ran exactly once.
    - Bootstrap (kitty `ensure_terminal`, editor `ensure`) completed.
  - **Create / headless / failure:** `run_prepare` raises:
    - `execute_command` re-raises (`cli.main` returns 1 via the `HopError` branch).
    - No `ensure_terminal` / `editor.ensure` calls were made.
    - `services.sway.switched_workspaces` still contains `p:<session>` (user is on the new workspace seeing the popup with the error).
  - **Create / interactive:** `StubHopPopup(is_interactive=True)`:
    - `run_prepare` is NOT invoked.
    - `SessionBackendRegistry` runner stub records the prepare command exactly once (inline path).
    - `switched_workspaces` is recorded *after* prepare ran (call-order reversed vs. the headless case).
    - Bootstrap proceeds normally.
  - **Create / no prepare command:** Headless, backend has `prepare_command=None`: `run_prepare` is invoked but is a no-op; bootstrap proceeds.
  - **Create / re-entry & spawn-another-shell:** `run_prepare` records zero calls regardless of `is_interactive`. Existing behavior unchanged.
  - **Kill / headless / success:** `execute_command(KillCommand(), ...)` with `StubHopPopup(is_interactive=False)` against a devcontainer-style session:
    - `kill_session` closed every session window (records on `StubSwayAdapter.closed_windows`) BEFORE `run_teardown` was invoked (call-order assertion).
    - `run_teardown` recorded one call with the session name and the backend's teardown command.
    - The `SessionBackendRegistry` runner stub saw zero `teardown` subprocess invocations (popup ran it).
    - `forget_session` ran (the persisted state file is gone after the call).
  - **Kill / headless / failure:** `run_teardown` raises:
    - `execute_command` re-raises.
    - `forget_session` did NOT run (the persisted state file is still present). Matches today's "teardown failure short-circuits forget" semantics.
    - Session windows were still closed (closing happens before teardown; failure of teardown doesn't un-close them).
  - **Kill / interactive:** `StubHopPopup(is_interactive=True)`:
    - `run_teardown` is NOT invoked.
    - `SessionBackendRegistry` runner stub records the teardown command exactly once (inline path).
    - `forget_session` ran on success; existing test for inline teardown still passes.
  - **Kill / no teardown command:** Headless, backend has `teardown_command=None`: `run_teardown` is invoked but is a no-op; `forget_session` runs as usual.

- `tests/test_cli.py`:
  - `main(["switch", "nonexistent"])` against services with `StubHopPopup(is_interactive=False)` and a sway stub that has no matching workspace: returns 1, `StubHopPopup.shown_errors` records the `HopError` exactly once, the `print(..., file=sys.stderr)` is still emitted (capsys.readouterr().err is non-empty).
  - Same scenario with `StubHopPopup(is_interactive=True)`: returns 1, `shown_errors` is empty (no popup), stderr print still emitted.
  - `main([])` against services where `StubHopPopup` raises `SessionBackendError("prepare failed", surfaced_by_popup=True)` from `run_prepare`: returns 1, `shown_errors` is empty (the popup adapter already surfaced it), stderr print still emitted.
  - `main(["--backend", "nonexistent"])` against services with `StubHopPopup(is_interactive=False)`: `UnknownBackendError` raised inside `resolve_for_entry`, caught by `cli.main`, surfaces in `shown_errors`. (Verifies that lifecycle-adjacent errors NOT raised by the popup adapter itself still get the error popup.)

- `tests/test_app.py` `SessionBackendRegistry` cases:
  - `resolve_for_entry(session, backend_name=..., skip_prepare=True)` returns the backend without invoking the runner's prepare command. Mirrors `test_session_base_registry_runs_activate_then_prepare` with `skip_prepare=True` and asserts only the activate call ran (no `flock`/`prepare`).
  - `resolve_for_entry(session, ..., skip_prepare=True)` still calls `probe_workspace_path` afterwards (it's how `workspace_path` lands on the persisted record).

- `tests/test_kill.py`:
  - `kill_session(..., teardown_runner=fake)` calls `fake(session, backend)` exactly once in place of `backend.teardown(session)`. `forget` runs after `fake` returns normally and does NOT run when `fake` raises.
  - `kill_session(...)` without the kwarg keeps today's behavior (existing tests untouched).

- `tests/test_errors.py` (new, small):
  - `HopError(...)` defaults `surfaced_by_popup` to `False`.
  - `HopError(..., surfaced_by_popup=True)` carries the flag through; subclasses (`SessionBackendError`, `UnknownBackendError`, etc.) inherit the kwarg via `super().__init__(...)`.

- No new `tests/test_vicinae.py` cases — the auto-detect lives behind `HopServices` and the vicinae script bodies don't change. Existing tests still pass.

## Out of scope

- Deduping the second silent `backend.prepare` call inside `_bootstrap_session_kitty`. Idempotent by spec; the popup's job is the cold-prepare UI, not eliminating the bootstrap-time re-execution.
- Surfacing lifecycle output for `hop term --role <role>`, `hop edit`, `hop browser`, `hop run` invocations originating headlessly. Those target already-live sessions; if a headless window-script fires against a session whose kitty has died, the cold-bootstrap inside `_bootstrap_session_kitty` still runs silent prepare — a separate task can extend popup coverage to that edge case.

## Task Type

implement

## Principles

- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `HopServices.popup: HopPopup` exists; production wiring builds a `KittyHopPopup` whose `is_interactive` keys on `sys.stderr.isatty()`.
- `HopError.__init__` accepts `surfaced_by_popup: bool = False`; the attribute is set on the instance and inherited by every subclass.
- `execute_command(EnterSessionCommand(...))` on the first-entry path:
  - Interactive → byte-for-byte today (inline prepare via `resolve_for_entry`, then `enter_project_session`).
  - Headless → `resolve_for_entry(skip_prepare=True)` → sway switches to `p:<session>` *before* prepare runs → `services.popup.run_prepare` runs the popup and blocks → normal bootstrap continues on success. On failure (`SessionBackendError` with `surfaced_by_popup=True`) the bootstrap is aborted: no `set_override`, no `enter_project_session`, no kitty / editor windows.
- `execute_command(KillCommand())`:
  - Interactive → byte-for-byte today (`kill_session` closes windows then runs inline `backend.teardown`).
  - Headless → `kill_session` closes windows then delegates the teardown step to `services.popup.run_teardown`; popup runs the teardown and blocks. On success, `forget_session` runs. On failure, `forget_session` is NOT called.
- `cli.main`'s `HopError` catch invokes `services.popup.show_error(error)` exactly when `not error.surfaced_by_popup and not services.popup.is_interactive()`. The existing `print(str(error), file=sys.stderr)` runs unconditionally as today.
- `EnterSessionCommand` on re-entry / spawn-another-shell and `KillCommand` against a session with no `teardown_command` are no-ops for the lifecycle popup regardless of TTY state.
- `SessionBackendRegistry.resolve_for_entry(session, ..., skip_prepare=True)` selects the backend and probes `workspace_path` but does NOT invoke `backend.prepare`. Default `skip_prepare=False` matches today's behavior.
- `kill_session(..., teardown_runner=...)` delegates to the supplied callable in place of `backend.teardown(session)`; default `None` keeps today's inline behavior.
- `KittyHopPopup.run_prepare` / `run_teardown` are no-ops when the matching command on the backend is `None`. Otherwise each launches `kitten panel` with `--edge=center --layer=overlay --focus-policy=on-demand --app-id=hop:popup` (plus column/line sizing), runs the lifecycle wrapper script (cd + echo + `flock`-wrapped command + held-open-on-failure), and blocks until the kitten process exits. Exit 0 returns normally; non-zero raises `SessionBackendError(..., surfaced_by_popup=True)`.
- `KittyHopPopup.show_error` launches `kitten panel` with the same flags and a one-shot wrapper that prints `hop: <type>: <message>` and execs `sh`; blocks until the panel exits and returns regardless of its exit code.
- The lifecycle wrapper script preserves the flock serialization (`/run/user/<uid>/hop/backend-<session>.lock`) so a popup-run command cannot race the other lifecycle direction running in parallel.
- `hop_spec.md` documents the auto-detect for Enter session, Kill session, and error display; the `app_id="hop:popup"` tag; and the kitty ≥ 0.34 prerequisite.
- `README.md`'s prerequisites section is bumped to kitty ≥ 0.34 and includes a one-line explanation of when the popup appears. No sway `for_window` rule is required.
- No CLI surface changes (`hop --help` output is unchanged); no `hop/vicinae.py` script-body changes.
- New unit tests cover the cases listed in the Tests section, follow the no-mock convention (real subprocess fakes via launcher injection, deterministic TTY via the injected callable), and pass under `make`.
- `bunx dust lint` passes for the task file.
