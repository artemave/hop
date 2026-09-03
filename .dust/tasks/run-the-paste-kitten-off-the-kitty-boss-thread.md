# Run the paste kitten off the kitty boss thread

Move the paste kitten's clipboard read and backend file write off the kitty boss thread, so pasting into a session window can't freeze the kitty.

## Problem

`hop/kitten/paste/main.py`'s `handle_result` runs **in the kitty boss** — the single thread that also drives the GUI and services Wayland. It does two blocking things there, both unbounded:

1. `hop.clipboard.read()` → `subprocess.run(["wl-paste", …])` (no `timeout=`) to detect image-vs-text and, for text, to read the clipboard.
2. `hop.paste.paste_clipboard_image(...)` → `backend.write_file(...)`, which for a container backend is `<noninteractive_prefix> sh` (a `podman-compose exec` startup, ~1–3 s) and for an **ssh** backend is `ssh host … sh` — a network round-trip that hangs outright when the ControlMaster is dead or the link dropped.

### The self-deadlock (deterministic, reproduced repeatedly)

When the session's own kitty is the Wayland clipboard owner — i.e. you copied anything from any window in that session, the normal "copy here, paste into the agent there" flow — <kbd>Ctrl-V</kbd> deadlocks every time:

- The kitten calls `wl-paste` on the boss thread. `wl-paste` asks the compositor for the data; the compositor asks the clipboard owner — the **same kitty** — to write the bytes to a pipe.
- The kitty boss can't answer: it's blocked waiting for `wl-paste` to exit.
- `wl-paste` (and the `cat` it forks to pump the data pipe) wait forever; the boss waits forever. Every window in the session freezes.

Observed while hung: `wl-paste --list-types` reports `application/glfw+clipboard-<kitty-pid>` (GLFW = kitty's windowing lib), the boss main thread sits in `poll(nfds=2, timeout=-1)`, and a stuck `wl-paste --no-newline` plus its forked `cat` (PID = `wl-paste` PID + 1, reparented to `systemd --user` once `wl-paste` is killed, still holding the boss's stdout/stderr pipe read ends) are children of the session kitty. Recovery requires `kill -9` on both the `wl-paste` and the `cat`.

`paste_from_clipboard` (the current fallback) deadlocks the same way — it's still the boss asking itself for clipboard data.

The earlier task ([[paste-a-clipboard-image-as-a-file-path]]) called out boss-loop blocking as a known v1 shortcut ("Acceptable for a v1 on an explicit keypress"). It is not acceptable: the common case hangs, not an edge case.

## Approach

Two independent moves, both required:

### 1. Read the clipboard through kitty's in-process API, not a subprocess

`handle_result` already runs inside kitty's interpreter with the live `boss` object. Use kitty's own clipboard access instead of shelling out to `wl-paste`:

- **Text is not the kitten's job.** The kitten only shells out in the text case to decide "not an image, pass through" — that probe is exactly what deadlocks. Drop it. When the clipboard has no image, do nothing and let kitty paste natively (`paste_from_clipboard`), or better, don't intercept at all for that case. No `wl-paste`, no `--list-types`.
- **Image bytes** come from kitty's in-process clipboard object on `boss`. kitty's non-text clipboard read is **asynchronous** (the Wayland fetch happens on kitty's event loop; a callback fires with the bytes) — which is what breaks the deadlock: `handle_result` returns immediately, the boss goes back to its loop, services the fetch (answering itself instantly when it owns the selection), then invokes the callback.
  - Confirm the exact call against the installed kitty (currently **0.47.2**). `kitten @ get-clipboard` does **not** exist as a remote-control command; `kitten clipboard` is a separate subprocess (OSC 52, would deadlock the same way if blocked on) and is not suitable. The in-process path is `boss`'s `Clipboard` / clipboard-request machinery — the implementer must read kitty 0.47's source for the method name and callback signature and isolate it behind one function in the kitten.

### 2. Do `backend.write_file` + `send-text` on a worker thread

Even with the clipboard read fixed, `backend.write_file` over ssh/`podman-compose` still blocks the boss. Once the image bytes arrive:

- Run `backend.write_file(session, write_path, data)` on a `threading.Thread` (or equivalent), not on the boss.
- Add a hard `timeout=` to the backend subprocess for this path (the runner call in `CommandBackend.write_file`) so a dead ssh master fails instead of hanging.
- On success, marshal the `send-text <path>` call back onto the boss thread — kitty's remote-control / `boss` API is not thread-safe. Use kitty's boss-thread scheduling primitive (e.g. the same mechanism kittens use to run boss work from a thread; confirm against kitty 0.47).
- On failure or timeout, surface it: write a short error line into the target window (or a kitty notification). A silently-dropped paste is the wrong default, especially for a remote session where "laptop slept, link down" is routine.

The path written and pasted is unchanged: `/tmp/hop-paste-<ns>.png` in the focused window's filesystem namespace (local write for host, base64 heredoc through `<noninteractive_prefix> sh` for container/ssh), then `send-text --bracketed-paste=auto <path>`.

## Design

### `hop/kitten/paste/main.py`

`handle_result(args, answer, target_window_id, boss)`:

1. `window = boss.window_id_map.get(target_window_id)`; return if `None`.
2. Resolve the focused session + backend via `hop.focused.focused_session_and_backend()`. `None` → native `paste_from_clipboard`, return.
3. Kick off an async in-process clipboard read for an image MIME (`image/png`, plus whatever kitty exposes for "is there an image"). Register a callback. Return from `handle_result` immediately — no blocking call remains on the boss.
4. **Callback, image present:** spawn a worker thread that calls `hop.paste`'s materialize-and-paste helper with the bytes; the helper does `backend.write_file` then schedules `send-text` on the boss thread. On `SessionBackendError` / timeout, schedule an error line into the window.
5. **Callback, no image:** native `paste_from_clipboard` (scheduled on the boss thread if the callback isn't already on it).

Keep the existing rotating-log-on-exception behaviour.

### `hop/paste.py`

`paste_clipboard_image` currently calls `clipboard_read()` itself and runs `write_file` + `send_text` inline. Split:

- The clipboard read moves out (it's now kitty-async in the kitten).
- Keep a testable synchronous core: given `session`, `backend`, `write_path`, `data: bytes`, `send_text`, it calls `backend.write_file(session, Path(write_path), data)` then `send_text(write_path)`. No kitty imports, no threads (the kitten owns threading). `write_file` errors propagate.
- The `PasteOutcome` enum's `PASSTHROUGH` branch is subsumed by the kitten's "no image" callback path; drop it if nothing else needs it.

### `hop/clipboard.py`

`wl-paste` is no longer on the production path. Remove the module, or reduce it to nothing the kitten imports. `ClipboardImage` / `ClipboardText` were only consumed by `paste_clipboard_image`.

### `hop/backends.py`

`CommandBackend.write_file`: add a `timeout=` to the `self.runner(argv, …, stdin=script)` call for this method (a class-level constant, generous enough for a large screenshot over ssh, e.g. 15 s). A `subprocess.TimeoutExpired` becomes a `SessionBackendError` naming the path and the timeout. `HostBackend.write_file` (local `path.write_bytes`) is unaffected.

### `hop_spec.md`

Update the "Clipboard paste" subsection: the read is kitty's in-process clipboard API (not `wl-paste`), the materialize + paste runs off the boss thread, a failed/timed-out write surfaces in the window, and the non-image case is a native passthrough.

### Docs

- `README.md` "System clipboard on non-host backends" / the kitten setup note: drop any implication that the host needs `wl-clipboard` for paste (the hints/open-selection path and anything else that still uses it keeps its own requirement; paste no longer does). State that paste uses kitty's in-process clipboard API and runs its backend write asynchronously.
- `docs/ssh-devcontainer.md`, `docs/devcontainer.md`: adjust any `wl-clipboard`-for-paste mention; note the write can fail visibly on a dropped link.

## Files to change

- `hop/kitten/paste/main.py` — async in-process clipboard read; worker thread for write + boss-scheduled `send-text`; visible failure.
- `hop/paste.py` — split out the clipboard read; keep a sync materialize-and-paste core; drop `PASSTHROUGH` if unused.
- `hop/clipboard.py` — remove or gut; it's no longer imported by the kitten.
- `hop/backends.py` — `timeout=` on `CommandBackend.write_file`'s runner call; `TimeoutExpired` → `SessionBackendError`.
- `hop_spec.md`, `README.md`, `docs/ssh-devcontainer.md`, `docs/devcontainer.md` — as under Design → Docs.

## Tests

No mocks of hop's own code. External processes get the doubles the suite already uses (fake `CommandRunner` / `FakeBackend` in `tests/test_backends.py` / `tests/test_paste.py`).

- **`tests/test_paste.py`** — the sync core: image bytes + `FakeBackend` → `write_file(session, Path(write_path), <bytes>)` then `send_text(write_path)` once; `write_file` raising `SessionBackendError` propagates. Real `HostBackend` + `tmp_path` `write_path` → file on disk with exact bytes, `send_text` got that path.
- **`tests/test_backends.py`** — `CommandBackend.write_file`: fake runner that raises `subprocess.TimeoutExpired` → `SessionBackendError` naming the path; a slow-but-under-timeout runner still succeeds; the `timeout=` value is passed to the runner.
- **`tests/test_clipboard.py`** — deleted if the module goes; otherwise trimmed to whatever survives.
- **`tests/test_kitty.py`** — unchanged: the `--override map … kitten <…/hop/kitten/paste/main.py>` bindings and `clipboard_control` override still inject as before (bootstrap wiring is not what changes here).
- Kitten-internal threading / boss-callback wiring: exercised through the manual verification below; the boss object and kitty's async clipboard API have no in-suite double, and adding one would be mocking kitty. If a seam is needed, make the materialize-and-paste core (already sync and kitty-free) the unit under test and keep the kitten body a thin adapter.

## Manual verification (needs a real clipboard + display)

Per [[feedback_verify_external_cli_flags]], run it once:

- **Self-owned clipboard (the deadlock repro):** copy text from one window of a hop session, <kbd>Ctrl-V</kbd> into another window of the *same* session. It must paste (native passthrough) with no freeze. Repeat with an image copied from a kitty window → image attaches, no freeze.
- Image on the clipboard from an external app, <kbd>Ctrl-V</kbd> in a Codex and a Claude Code window, across all four session shapes: host, local container, remote container, remote host. Image attaches each time; the kitty stays responsive during the (async) backend write.
- Plain shell window, <kbd>Ctrl-V</kbd> with an image → path at the prompt, no freeze.
- Remote session with the ssh ControlMaster killed mid-paste → the write times out and an error line appears in the window; the kitty does not hang.
- Text-only clipboard → native passthrough. Empty clipboard → no-op.

## Out of scope

- Reworking the hints / open-selection kitten's clipboard or dispatch path. It has its own `wl-paste` / subprocess usage; this task only changes the paste kitten.
- A `hop paste` subcommand or any non-keypress trigger.
- macOS host support.
- Following the image into an inline thumbnail in the target app — it renders from the path.

## Task Type

implement

## Principles

- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `hop/kitten/paste/main.py`'s `handle_result` makes no blocking subprocess or blocking clipboard call on the boss thread. Copying from a window in a hop session and pasting into another window of the same session no longer freezes the kitty, for both text and image clipboards.
- The clipboard read uses kitty's in-process clipboard API on `boss` (asynchronous for image bytes), not `wl-paste` and not `kitten clipboard`.
- With an image on the clipboard, the kitten materializes it at `/tmp/hop-paste-<ns>.png` in the focused window's filesystem namespace via `backend.write_file` **on a worker thread**, then sends the path with `send-text --bracketed-paste=auto` marshalled back onto the boss thread. Verified against host, local container, remote container, and remote host sessions with the kitty staying responsive throughout.
- A non-image clipboard results in kitty's native `paste_from_clipboard`; an empty clipboard is a no-op.
- `CommandBackend.write_file` passes a `timeout=` to its runner call; a `subprocess.TimeoutExpired` becomes a `SessionBackendError` naming the path. A write failure or timeout is surfaced in the target window, not silently dropped — verified by killing a remote session's ssh ControlMaster mid-paste.
- `hop/paste.py` exposes a kitty-free, thread-free synchronous core (bytes + backend + `write_path` + `send_text` → write then paste) that the kitten calls; `hop/clipboard.py` is removed or no longer imported by the kitten.
- `hop_spec.md`, `README.md`, `docs/ssh-devcontainer.md`, and `docs/devcontainer.md` reflect the in-process read, the off-boss asynchronous write, and the visible-failure behaviour.
- New and updated tests follow the repo's no-mock conventions and pass under `make`.
- `bunx dust lint` passes for this task file.
