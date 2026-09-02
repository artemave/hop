# Paste a clipboard image as a file path

Bind a key in every hop session to a bundled kitten that turns a clipboard image into a file path in the focused window. The kitten reads the host clipboard, materializes the image as a file in the focused window's filesystem namespace, and pastes that path into the window. Claude Code and Codex attach the file; a plain shell or editor just receives the path. The keys are set by `[keys].paste` (default `["ctrl+v", "ctrl+shift+v"]`, empty to disable).

## Problem

Claude Code and Codex each implement their own <kbd>Ctrl-V</kbd> handler: read the system clipboard, and if it holds an image, attach it. On a container or remote session that handler has nothing to read — there is no display or clipboard inside the backend:

- **Codex** calls `arboard` (a linked Rust clipboard lib) directly. Inside a container it finds a stale, unreachable `DISPLAY` and hangs until X11 times out, then prints `Failed to paste image: clipboard unavailable`.
- **Claude Code** shells out to `wl-paste`, which has no display to reach.
- <kbd>Ctrl-Shift-V</kbd> (kitty's paste) only pastes clipboard *text*, never an image.

Because Codex reads the clipboard through a linked library rather than a subprocess, a `wl-paste` shim on the backend can't intercept it. The image has to be produced on the host and carried to the backend as a file.

## Approach

hop's controlling process runs in the host's Sway session, so it can read the clipboard directly and hand the bytes to the backend over the command channel hop already owns.

1. **Trigger** — `_bootstrap_session_kitty` binds each of `[keys].paste` (default <kbd>Ctrl-V</kbd> and <kbd>Ctrl-Shift-V</kbd>) to a bundled kitten, once per session kitty. Empty list → no binding, feature off.
2. **Read** — the kitten reads the host clipboard via `wl-paste`. It uses the `wlr-data-control` protocol, so the read does not depend on which window has focus. (hop's host is always a Sway session — Wayland only, no X11 path to handle.)
3. **Materialize** — the image is written to `/tmp/hop-paste-<ns>.png` in the focused window's filesystem namespace through `backend.write_file`: a local write for a host session, a `base64` payload piped through `<noninteractive_prefix> sh` for a container/remote session. One code path; the kitten does not branch on session shape.
4. **Paste** — the kitten sends the path into the focused window with `kitty @ send-text --bracketed-paste=auto`. Codex/Claude Code attach the file it points at; a shell drops it at the prompt; an editor inserts it.
5. **Fallback** — if the clipboard holds text (or anything unexpected, or no hop session resolves), the kitten issues kitty's native `paste_from_clipboard` instead. An empty clipboard is a silent no-op.

The image path issues no OSC 52 clipboard read — nothing here needs a `clipboard_control` change, a `kitten` on the backend, or a Wayland socket in the container.

### Two follow-on cleanups

- **Runtime-dir mount narrows.** Reading the clipboard on the host removes the reason to bind-mount the host Wayland socket (and the `WAYLAND_DISPLAY` stub) into a container. The blanket `${XDG_RUNTIME_DIR}` mount narrows to `${XDG_RUNTIME_DIR}/hop/`, which still carries the bridge socket. Docs-only change (the compose recipes are user-authored examples).
- **The manual `clipboard_control` step goes away.** The README currently tells users to add `clipboard_control … read-clipboard read-primary` to `kitty.conf` so an in-container nvim's OSC 52 paste (`"+p`) doesn't hit the per-paste permission prompt. Now that hop owns session-kitty config, hop injects that line itself at bootstrap, gated by `[clipboard].allow_read` (default `true`). The editor keeps `let g:clipboard = 'osc52'` — no bridge, no extra subcommand, instant. The tradeoff is unchanged from what kitty documents (any program in the session's kitty can then read the system clipboard via OSC 52); `[clipboard].allow_read = false` opts out and restores the prompt.

## Design

### The paste kitten — `hop/kitten/paste/main.py`

Shape mirrors `hop/kitten/hints/main.py`: prepend the `hop`-package parent to `sys.path`, drop stale `hop.*` modules when running inside a kitty boss, `def main(args) -> str: return ""`, and `@result_handler(no_ui=True) def handle_result(args, answer, target_window_id, boss)`.

`handle_result`:

1. `window = boss.window_id_map.get(target_window_id)`; return if `None`.
2. Resolve the focused session + backend via `hop.focused.focused_session_and_backend()`. `None` → go to step 5.
3. `content = hop.clipboard.read()` → `ClipboardImage(bytes)`, `ClipboardText(str)`, or `None`.
4. **Image:** build `hop/paste.py`'s `send_text` from `boss.call_remote_control`, then
   `paste_clipboard_image(session=…, backend=…, write_path=f"/tmp/hop-paste-{time.time_ns()}.png", send_text=…, clipboard_read=lambda: content)`.
5. **Otherwise** (`PASSTHROUGH` return, unresolved session, or any exception): `boss.call_remote_control(window, ("action", f"--match=id:{window.id}", "paste_from_clipboard"))`.
6. On exception, also log one line to a rotating file under `$XDG_RUNTIME_DIR/hop` (like the hints kitten).

`send-text --bracketed-paste=auto` emits ANSI paste-mode escapes (`ESC[200~` … `ESC[201~`), never literal `[` `]`; `auto` sends them only when the receiver has bracketed-paste mode on, so a plain shell shows just the path. Manual verification checks whether an unbracketed path also triggers the Codex/Claude Code attach — if so, drop the flag and send the bare path.

**Boss-loop blocking.** `handle_result` runs in the kitty boss event loop; `backend.write_file` over `ssh … podman exec` is ~100–500 ms and stalls that kitty process for the duration. Acceptable for a v1 on an explicit keypress. If it needs fixing later, spawn the `write_file` + `send-text` in a short detached helper (`subprocess.Popen`, no wait). Note it in the code; don't pre-optimize.

### `hop/clipboard.py` — host clipboard read

Pure host-side; no backend, no kitty. The subprocess calls go through an injectable runner param (default `subprocess.run`). Wayland only — hop runs in a Sway session, so `wl-paste` is the single path.

- `read() -> ClipboardImage | ClipboardText | None`: `wl-paste --list-types`; an `image/*` type present → `wl-paste --type image/png --no-newline` → `ClipboardImage`; else `wl-paste --no-newline` → `ClipboardText`; empty output → `None`.
- `wl-paste` absent (`FileNotFoundError`) → `HopError` with an install hint (`wl-clipboard`). No silent degrade.

Read-only: the editor's copy direction stays on OSC 52 (kitty allows `write-clipboard` by default), so hop never needs to *write* the host clipboard.

### `hop/paste.py` — orchestrator (testable core)

`paste_clipboard_image(*, session, backend, write_path, send_text, clipboard_read=clipboard.read) -> PasteOutcome`

- clipboard holds an image → `backend.write_file(session, Path(write_path), data)`, then `send_text(write_path)`; return `PasteOutcome.IMAGE`.
- anything else → do nothing; return `PasteOutcome.PASSTHROUGH`.

No kitty / `boss` imports. `write_file` errors propagate (the kitten logs and passes through). Empty-clipboard collapses into `PASSTHROUGH` deliberately — `paste_from_clipboard` on an empty clipboard is already a no-op.

### `SessionBackend.write_file`

Add to the Protocol in `hop/backends.py`, the inverse of `read_file` / `materialize_on_host`:

```python
def write_file(self, session: ProjectSession, path: Path, data: bytes) -> None: ...
```

- `HostBackend.write_file`: `path.write_bytes(data)`.
- `CommandBackend.write_file`: base64-encode `data` and deliver it as a heredoc **inside** the script piped to a bare `sh` (same "over stdin to bare `sh`, survives argv-flattening prefixes like `ssh host …`" rule the neighbouring methods follow — the payload can't take a second stdin):
  ```sh
  mkdir -p "$(dirname <path>)" 2>/dev/null
  base64 -d > <path> <<'HOP_EOF'
  <base64…>
  HOP_EOF
  ```
  `composed = f"{substituted_prefix} sh"`, run via `self.noninteractive_transport` + `self.runner(argv, runner_cwd(...), stdin=script)`. Non-zero exit → `SessionBackendError` with stderr. `base64` is already a required backend-side tool (the bridge shim needs it).

### Editor text clipboard

Unchanged mechanism, one less manual step. An in-container nvim keeps:

```vim
if empty($WAYLAND_DISPLAY) && empty($DISPLAY)
  let g:clipboard = 'osc52'
endif
```

Yank (`"+y`) works on kitty's default `write-clipboard`. Paste (`"+p`) issues an OSC 52 read; hop's bootstrap-injected `clipboard_control … read-clipboard read-primary` (when `[clipboard].allow_read`, the default) suppresses the per-paste prompt. No `hop`-side subcommand, no bridge dependency. This task does **not** add a `hop clipboard` provider — OSC 52 plus the injected `clipboard_control` covers the editor.

### `hop.focused.focused_session_and_backend()`

`paths_exist` already resolves workspace → session name → `SessionState` → backend → `session_from_state` inline. Factor that prefix out:

```python
def focused_session_and_backend(
    *, focused_workspace=..., sessions_loader=..., backend_loader=...
) -> tuple[ProjectSession, SessionBackend] | None: ...
```

`None` when no focused hop session / no record / no backend. `paths_exist` is refactored onto it; the paste kitten calls it too. Same injectable loaders for tests.

### Bootstrap wiring

In `hop/kitty.py::_bootstrap_session_kitty`, append to `kitty_args`:

- for each key in `[keys].paste`: `--override`, `map <key> kitten <PASTE_KITTEN_PATH>`. Empty list → nothing. `<PASTE_KITTEN_PATH>` resolves like the hints kitten's asset path (`resolve_asset_path("kitten/paste")`).
- when `[clipboard].allow_read` (default `true`): `--override`, `clipboard_control write-clipboard write-primary read-clipboard read-primary`. When `false`: nothing (kitty's default `read-clipboard-ask` stands).

Keymaps and `clipboard_control` are kitty-process-global, so binding at bootstrap covers every later `kitty @ launch` window.

### Config

Two new top-level tables in `hop/config.py`, siblings of `workspace_layout`. Both parsed in the top-level reader, exposed via `load_global_config`, merged project-over-global, no state-file persistence.

**`[keys]`** — kitty keybindings hop injects at bootstrap, so the user only names a key, never writes a `map` line:

- `paste` — a kitty key spec or a list; default `["ctrl+v", "ctrl+shift+v"]`. Empty string / empty list ⇒ no binding. Accept a bare string as a one-element list. A project `[keys].paste` replaces the global one wholesale (no element merge).

`[keys]` is introduced here with just `paste`; a follow-up task moves the `open_selection` (hints) binding onto it the same way.

**`[clipboard]`** — clipboard-read policy:

- `allow_read` — bool, default `true`. `true` ⇒ hop injects `clipboard_control … read-clipboard read-primary` into the session kitty so OSC 52 paste works without the prompt. `false` ⇒ no override; kitty's default prompt stands. The tradeoff (`true` lets any program in the session's kitty read the system clipboard via OSC 52) is the one kitty documents.

```toml
[keys]
paste = ["ctrl+v", "ctrl+shift+v"]

[clipboard]
allow_read = true
```

### Docs

- `docs/ssh-devcontainer.md` §5 and §8: drop the `WAYLAND_DISPLAY` stub guidance; narrow the mount to `${XDG_RUNTIME_DIR}/hop/`; state that image paste is <kbd>Ctrl-V</kbd> → the paste kitten (host-side read) and editor text clipboard stays on nvim's OSC 52 provider, with hop injecting `clipboard_control` (per `[clipboard].allow_read`) so no `kitty.conf` edit is needed.
- `docs/devcontainer.md` §4a: recommend the `${XDG_RUNTIME_DIR}/hop/` directory mount as the primary pattern; keep the single-file `api.sock` mount as the minimal alternative, with the inode caveat (a hopd restart that recreates the socket breaks a single-file bind mount; the directory mount survives it).
- `README.md` "System clipboard on non-host backends": add the paste-kitten paragraph; add `[keys].paste` and `[clipboard].allow_read` to the config reference; note the host needs `wl-clipboard`. Rework the `clipboard_control … read-clipboard` guidance: it is no longer a manual `kitty.conf` step — hop injects it when `[clipboard].allow_read` (default true); keep the tradeoff sentence; document `allow_read = false` as the opt-out.
- `README.md` near the kitten setup ("Open visible-output targets from Kitty"): a one-line note that hop's session kitties get `allow_remote_control`, a listen socket, and the paste keybinding(s) from bootstrap — no `kitty.conf` changes needed. The paste kitten also uses the in-process `boss.call_remote_control` API regardless.
- All three docs: a stale `DISPLAY` / `WAYLAND_DISPLAY` in the container env is what makes Codex's handler hang and also defeats the `if empty($WAYLAND_DISPLAY) && empty($DISPLAY)` guard in the vimrc snippet — clear it.

### Spec

`hop_spec.md`: add a "Clipboard paste" subsection near "Kitty integration". Cover the bootstrap key binding → bundled kitten, the host-side read, the `write_file` push over `noninteractive_prefix`, the paste into the focused window, the `paste_from_clipboard` fallback, `[keys].paste`, and the bootstrap-injected `clipboard_control` gated by `[clipboard].allow_read`.

## Files to change

- `hop/kitten/paste/main.py`, `hop/kitten/paste/__init__.py` — new; thin kitten mirroring `hop/kitten/hints/`.
- `hop/clipboard.py` — new; `read()` + `ClipboardImage` / `ClipboardText`, injectable runner.
- `hop/paste.py` — new; `paste_clipboard_image(...)` + `PasteOutcome`.
- `hop/focused.py` — extract `focused_session_and_backend()`; refactor `paths_exist` onto it.
- `hop/backends.py` — `write_file` on `SessionBackend` / `HostBackend` / `CommandBackend`.
- `hop/kitty.py` — `_bootstrap_session_kitty` appends one `map` override per `[keys].paste` key, plus the `clipboard_control` override when `[clipboard].allow_read`.
- `hop/config.py` — `[keys]` table (`paste`, string-or-list, normalize + wholesale merge) and `[clipboard]` table (`allow_read` bool, default true); expose via `load_global_config`.
- `hop_spec.md` — "Clipboard paste" subsection.
- `docs/ssh-devcontainer.md`, `docs/devcontainer.md`, `README.md` — as listed under Docs.

## Tests

No mocks of hop's own code. External processes get the doubles the suite already uses (`StubLauncher` in `tests/test_kitty.py`, fake `CommandRunner` / `FakeBackend` in `tests/test_backends.py`).

- **`tests/test_clipboard.py`** (new) — fake runner:
  - `wl-paste --list-types` with `image/png` → `read()` returns `ClipboardImage` from `wl-paste --type image/png`; text-only listing → `ClipboardText`; empty → `None`.
  - `wl-paste` missing (`FileNotFoundError`) → `HopError` with an install hint.
- **`tests/test_paste.py`** (new) — fake `send_text`, fake `clipboard_read`, a `write_path`, `FakeBackend` recording `write_file`:
  - image → `write_file(session, Path(write_path), <bytes>)` then `send_text(write_path)` once; returns `IMAGE`.
  - text / `None` → no `write_file`, no `send_text`; returns `PASSTHROUGH`.
  - `write_file` raising `SessionBackendError` → propagates (not swallowed).
  - real `HostBackend` + `tmp_path` `write_path` + image → file on disk with exact bytes, `send_text` got that path.
- **`tests/test_backends.py`** — `CommandBackend.write_file`: fake runner gets `(*noninteractive_prefix, "sh")` with a `stdin=` script containing `base64 -d > '<path>'` and the base64 of the input; non-zero exit → `SessionBackendError` with stderr. `HostBackend.write_file`: real bytes incl. NUL to a `tmp_path` file, round-trips.
- **`tests/test_focused.py`** — `focused_session_and_backend()` returns `(session, backend)` for a `p:<name>` workspace with a record; `None` for a non-`p:` workspace, a missing record, a `None` backend. Existing `paths_exist` tests stay green.
- **`tests/test_kitty.py`** — default config → `StubLauncher` records a `--override map … kitten <…/hop/kitten/paste/main.py>` for **both** `ctrl+v` and `ctrl+shift+v`, and one `--override clipboard_control write-clipboard write-primary read-clipboard read-primary`. Custom `[keys].paste` list honored key-for-key; empty list → no map override. `[clipboard].allow_read = false` → no `clipboard_control` override.
- **`tests/test_config.py`** — `[keys].paste`: round-trips as a list; bare string normalizes; defaults to `["ctrl+v", "ctrl+shift+v"]` when absent; project replaces global wholesale; non-string / non-list rejected. `[clipboard].allow_read`: defaults to `true` when `[clipboard]` or the key is absent; project overrides global; non-bool rejected.

## Manual verification (needs a real clipboard + display)

Per `feedback_verify_external_cli_flags`, run it once:

- PNG on the clipboard, <kbd>Ctrl-V</kbd> in a Codex window → image attaches, across all four session shapes (host, local container, remote container, remote host). Repeat in Claude Code.
- <kbd>Ctrl-V</kbd> in a plain shell window → path at the prompt, no visible brackets, no premature execution.
- Text-only clipboard → native passthrough. Empty clipboard → silent no-op.
- Works regardless of whether the target window recently had focus.
- Send the bare path (no `--bracketed-paste=auto`) → confirm Codex and Claude Code still attach. If so, drop the flag.
- In an in-container nvim with `g:clipboard = 'osc52'`: `"+p` pastes with no permission prompt (default `allow_read = true`); set `[clipboard].allow_read = false`, re-enter the session, confirm `"+p` now prompts.

## Out of scope

- Inline image thumbnails in the target app — it renders/attaches from the path itself.
- Making Codex's native `arboard` path work (Wayland/X forwarding). The kitten intercepts the key first.
- A `hop paste` subcommand. The only trigger is the key binding; reusable logic lives in `hop/clipboard.py` / `hop/paste.py`.
- A `hop clipboard` subcommand / bridge-backed `g:clipboard` provider. OSC 52 plus hop's injected `clipboard_control` covers the editor text clipboard; revisit only if a non-OSC-52 editor path is ever needed.
- Suppressing the paste binding for host sessions only. It is uniform across session shapes; disable it globally with an empty `[keys].paste` or change the keys.
- Migrating the `open_selection` (hints) binding onto `[keys]`. Separate follow-up task; this one only introduces `[keys]` with `paste`.
- A fallback for a host with no `wl-clipboard`. Missing tool → `HopError` with an install hint. (No X11 path — hop's host is a Sway session.)
- General-purpose clipboard access for arbitrary in-container programs.
- macOS host support (`pbpaste` / `pngpaste`). hop's host is a Linux Sway session.
- Relocating the bridge socket out of `${XDG_RUNTIME_DIR}/hop/`. This task narrows the mount; it does not remove it.

## Task Type

implement

## Principles

- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)

## Blocked By

(none)

## Definition of Done

- Every key in `[keys].paste` (default `["ctrl+v", "ctrl+shift+v"]`) runs the bundled `hop/kitten/paste/main.py` kitten in any window of a hop session, each bound via a `--override map … kitten <path>` injected at session-kitty bootstrap; an empty list injects nothing.
- With an image on the clipboard, the kitten materializes it at `/tmp/hop-paste-<ns>.png` in the focused window's filesystem namespace via `backend.write_file` and pastes that path into the window; the focused app receives a usable path. Verified against a host session, a local container, a remote container, and a remote host — no branching on session shape.
- The path is sent with `send-text --bracketed-paste=auto` unless manual verification shows an unbracketed path also triggers the Codex/Claude Code attach, in which case the bare path is sent. No literal brackets appear and a plain shell receives just the path with no premature execution.
- The clipboard read runs on the host via `hop/clipboard.py` (`wl-paste`), does not depend on window focus, and raises `HopError` with an install hint when `wl-paste` is missing.
- Text (or anything unexpected, or no hop session) falls back to `paste_from_clipboard`; an empty clipboard is a silent no-op.
- `SessionBackend.write_file` writes into the session's filesystem namespace: `HostBackend` locally; `CommandBackend` via a base64 heredoc through `<noninteractive_prefix> sh`, raising `SessionBackendError` on non-zero exit. One code path for all session shapes.
- `_bootstrap_session_kitty` injects `--override clipboard_control write-clipboard write-primary read-clipboard read-primary` iff `[clipboard].allow_read` (default true); an in-container nvim with `g:clipboard = 'osc52'` then pastes without the per-paste prompt, and `allow_read = false` restores it.
- `hop.focused.focused_session_and_backend()` exists, is used by the paste kitten and `paths_exist`, and returns `None` outside a focused hop session.
- `[keys].paste` accepts a string or list, normalizes to a list, defaults to `["ctrl+v", "ctrl+shift+v"]`, is replaced wholesale by a project value, and disables the binding when empty. `[clipboard].allow_read` is a bool defaulting to `true`, project-overridable.
- `hop_spec.md`, `docs/ssh-devcontainer.md`, `docs/devcontainer.md`, and `README.md` are updated as listed: `${XDG_RUNTIME_DIR}/hop/` mount, no `WAYLAND_DISPLAY` stub, `clipboard_control` now hop-injected via `[clipboard].allow_read`, stale-`DISPLAY` note, "no `kitty.conf` changes needed" note, paste kitten + `[keys].paste` documented.
- New and updated tests follow the repo's no-mock conventions and pass under `make`.
- `bunx dust lint` passes for this task file.
