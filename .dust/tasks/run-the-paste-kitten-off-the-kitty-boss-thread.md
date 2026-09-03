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
- **Image bytes** come from kitty's in-process `Clipboard` object. The API (confirmed against the installed kitty **0.47.2** by disassembling `kitty/clipboard.py` — the frozen build has no readable source):

  ```python
  from kitty.clipboard import Clipboard, ClipboardType
  cb = Clipboard(ClipboardType.clipboard)          # or ClipboardType.primary_selection
  cb.get_available_mime_types_for_paste() -> tuple[str, ...]     # like `wl-paste --list-types`
  cb.get_mime(mime: str, output: Callable[[bytes], None]) -> None
  ```

  `get_mime` is the read to use, and it is **deadlock-proof by construction**:
  - When kitty owns the selection — the exact case that hangs `wl-paste` — `get_mime` catches the C layer's `is_self_offer` and reads straight from kitty's own in-memory buffer, calling `output` **synchronously**. No Wayland round-trip.
  - Otherwise it hands off to the C `get_clipboard_mime` and returns immediately; `output` is invoked later from the event loop as chunks arrive. Non-blocking.
  - **End-of-stream is `output(b"")`** — an empty-bytes call. Accumulate non-empty chunks; the empty chunk is the signal to hand off to the background process (§2). (Same contract on both paths — visible in the self-offer chunker loop `q = b' '; while q: q = chunker(); output(q)`.)

  Do **not** use `Clipboard.get_mime_data(mime) -> bytes` — it's the synchronous convenience wrapper and blocks the boss in the not-owned case. `get_text()` is fine for text but the kitten shouldn't be reading text at all.

  Not viable: `kitten @ get-clipboard` does not exist as a remote-control command in 0.47; `kitten clipboard` is a separate subprocess (OSC 52) that would deadlock exactly like `wl-paste` if blocked on.

### 2. Do `backend.write_file` + `send-text` via `boss.run_background_process`, supervised

Even with the clipboard read fixed, `backend.write_file` over ssh/`podman-compose` still blocks the boss. When `get_mime` finishes (the `output(b"")` chunk), the callback runs **on the boss thread** with the full image bytes in memory. It must not do the backend write there — but a plain detached `Popen` is also wrong: nothing reaps it (zombies) and nothing sees it fail (silent drop, especially on a dropped ssh link).

kitty 0.47 has the right primitive — **`Boss.run_background_process`** (confirmed by introspection):

```python
Boss.run_background_process(
    cmd: list[str], *, cwd=None, env=None, stdin: bytes | None = None,
    allow_remote_control: bool = False, remote_control_passwords=None,
    notify_on_death: Callable[[int, Exception | None], None] | None = None,
    stdout: int | None = None, stderr: int | None = None,
) -> None
```

- Non-blocking (on Wayland it wraps the spawn in `run_with_activation_token`, still async).
- kitty's `ChildMonitor` **reaps** the process — tracked in `background_process_death_notify_map`, cleared by `on_child_death`. **No zombies.**
- `notify_on_death(exit_code, spawn_exc)` fires **on the boss thread** (dispatched from `on_child_death` on the main loop). **Failure is seen.**
- `stdin=<image bytes>` — the bytes go straight in; **no host tempfile**.
- `allow_remote_control=True` (with a scoped `remote_control_passwords`) lets the helper `kitten @ send-text` back into this kitty with no separate socket wiring.
- `stdout`/`stderr` can be set to a pipe/fd for the `notify_on_death` handler to read a failure message from.

Flow:

1. In `output`'s `b""` branch: `boss.run_background_process(cmd=[sys.executable, "-m", "hop", "<paste-helper>", session_name, write_path, str(window.id)], stdin=bytes(buf), allow_remote_control=True, remote_control_passwords={...: ["send-text"]}, notify_on_death=on_paste_done, stderr=<pipe>)`. Return.
2. Helper: read image bytes from **stdin**, rebuild session+backend from state (as `hop.focused` does), `backend.write_file(session, write_path, data)`, then `kitten @ send-text --match id:<window.id> --bracketed-paste=auto <write_path>` (using the granted remote control). Non-zero exit + one stderr line on any failure.
3. `CommandBackend.write_file` gets a hard `timeout=` on its runner call (class constant, ~15 s) so a dead ssh master fails instead of hanging; `TimeoutExpired` → `SessionBackendError`. The helper always exits, so `notify_on_death` always fires.
4. `on_paste_done(exit_code, exc)` on the boss: non-zero / `exc` → read the helper's stderr line, `send-text` it into the window, and log it. Silent drop is not an option.

The path written and pasted is unchanged: `/tmp/hop-paste-<ns>.png` in the focused window's filesystem namespace (local write for host, base64 heredoc through `<noninteractive_prefix> sh` for container/ssh), then `send-text --bracketed-paste=auto <path>`.

## Design

### `hop/kitten/paste/main.py`

`handle_result(args, answer, target_window_id, boss)`:

1. `window = boss.window_id_map.get(target_window_id)`; return if `None`.
2. Resolve the focused session + backend via `hop.focused.focused_session_and_backend()`. `None` → native `paste_from_clipboard`, return.
3. Check `Clipboard(ClipboardType.clipboard).get_available_mime_types_for_paste()` for an image type. None → native `paste_from_clipboard`, return.
4. Call `cb.get_mime("image/png", output)` where `output` appends non-empty chunks to a `bytearray` and, on `b""`, calls `boss.run_background_process(...)` with the bytes as `stdin` and a `notify_on_death` handler (§2). Return from `handle_result` immediately.

`get_mime`'s `output` runs on the boss thread but only does an in-memory append and one non-blocking `run_background_process` call — no backend call, no wait.

Keep the existing rotating-log-on-exception behaviour.

### `hop/paste.py`

`paste_clipboard_image` currently calls `clipboard_read()` itself and runs `write_file` + `send_text` inline. Split:

- The clipboard read moves out (it's now kitty-in-process in the kitten).
- Keep a testable synchronous core: given `session`, `backend`, `write_path`, `data: bytes`, `send_text`, it calls `backend.write_file(session, Path(write_path), data)` then `send_text(write_path)`. No kitty imports. `write_file` errors propagate to the caller (the helper entrypoint), which turns them into a stderr line and a non-zero exit.
- The `PasteOutcome` enum's `PASSTHROUGH` branch is subsumed by the kitten's "no image" path; drop it if nothing else needs it.

### The paste helper — `hop/commands/` entrypoint (or a `hop/paste.py` `__main__` hook)

A hidden hop entrypoint launched by `boss.run_background_process`. Args: session name, write-path, window id; image bytes on **stdin**. It:

- loads the session from state and rebuilds the backend (same path `hop.focused` uses),
- reads stdin, calls the `hop/paste.py` sync core with a `send_text` backed by `kitten @ send-text --match id:<id> --bracketed-paste=auto` (via the `allow_remote_control` grant),
- on `SessionBackendError` (incl. the write timeout): one line to stderr, exit non-zero — the kitten's `notify_on_death` handler surfaces it in the window.

Reaped and supervised by kitty (`run_background_process` / `ChildMonitor`); the kitten never `wait`s on it and it can't outlive as a zombie.

### `hop/clipboard.py`

`wl-paste` is no longer on the production path. Remove the module, or reduce it to nothing the kitten imports. `ClipboardImage` / `ClipboardText` were only consumed by `paste_clipboard_image`.

### `hop/backends.py`

`CommandBackend.write_file`: add a `timeout=` to the `self.runner(argv, …, stdin=script)` call for this method (a class-level constant, generous enough for a large screenshot over ssh, e.g. 15 s). A `subprocess.TimeoutExpired` becomes a `SessionBackendError` naming the path and the timeout. `HostBackend.write_file` (local `path.write_bytes`) is unaffected.

### `hop_spec.md`

Update the "Clipboard paste" subsection: the read is kitty's in-process clipboard API (not `wl-paste`); the materialize + paste runs in a kitty-supervised background process (`boss.run_background_process`, reaped by `ChildMonitor`, `notify_on_death` handler); a failed/timed-out write surfaces in the window; the non-image case is a native passthrough.

### Docs

- `README.md` "System clipboard on non-host backends" / the kitten setup note: drop any implication that the host needs `wl-clipboard` for paste (the hints/open-selection path and anything else that still uses it keeps its own requirement; paste no longer does). State that paste uses kitty's in-process clipboard API and runs its backend write in a kitty-supervised background process.
- `docs/ssh-devcontainer.md`, `docs/devcontainer.md`: adjust any `wl-clipboard`-for-paste mention; note the write can fail visibly on a dropped link.

## Files to change

- `hop/kitten/paste/main.py` — `Clipboard.get_mime` in-process read; accumulate chunks; `boss.run_background_process` with `stdin=<bytes>` + `notify_on_death`; native passthrough when no image type is offered; `notify_on_death` handler that surfaces a failure line.
- `hop/paste.py` — split out the clipboard read; keep a sync materialize-and-paste core; drop `PASSTHROUGH` if unused.
- new paste-helper entrypoint (`hop/commands/` or `hop/paste.py` `__main__`) — reads bytes from stdin, does `backend.write_file` + `send-text`, exits non-zero with a stderr line on failure.
- `hop/clipboard.py` — remove or gut; it's no longer imported by the kitten.
- `hop/backends.py` — `timeout=` on `CommandBackend.write_file`'s runner call; `TimeoutExpired` → `SessionBackendError`.
- `hop_spec.md`, `README.md`, `docs/ssh-devcontainer.md`, `docs/devcontainer.md` — as under Design → Docs.

## Tests

No mocks of hop's own code. External processes get the doubles the suite already uses (fake `CommandRunner` / `FakeBackend` in `tests/test_backends.py` / `tests/test_paste.py`).

- **`tests/test_paste.py`** — the sync core: image bytes + `FakeBackend` → `write_file(session, Path(write_path), <bytes>)` then `send_text(write_path)` once; `write_file` raising `SessionBackendError` propagates. Real `HostBackend` + `tmp_path` `write_path` → file on disk with exact bytes, `send_text` got that path.
- **`tests/test_backends.py`** — `CommandBackend.write_file`: fake runner that raises `subprocess.TimeoutExpired` → `SessionBackendError` naming the path; a slow-but-under-timeout runner still succeeds; the `timeout=` value is passed to the runner.
- **`tests/test_clipboard.py`** — deleted if the module goes; otherwise trimmed to whatever survives.
- **`tests/test_kitty.py`** — unchanged: the `--override map … kitten <…/hop/kitten/paste/main.py>` bindings and `clipboard_control` override still inject as before (bootstrap wiring is not what changes here).
- **helper entrypoint** — image bytes on a fake stdin + `FakeBackend`: happy path calls `write_file` then a `send_text` with the bracketed-paste path and exits 0; `SessionBackendError` from `write_file` → one stderr line and non-zero exit, no `send_text` of the path.
- Kitten body (`Clipboard.get_mime` callback, `run_background_process`, `notify_on_death`): exercised through manual verification — the boss object and kitty's clipboard API have no in-suite double and faking them would be mocking kitty. The kitten stays a thin adapter over the tested sync core + helper.

## Manual verification (needs a real clipboard + display)

Per [[feedback_verify_external_cli_flags]], run it once:

- **Self-owned clipboard (the deadlock repro):** copy text from one window of a hop session, <kbd>Ctrl-V</kbd> into another window of the *same* session. It must paste (native passthrough) with no freeze. Repeat with an image copied from a kitty window → image attaches, no freeze.
- Image on the clipboard from an external app, <kbd>Ctrl-V</kbd> in a Codex and a Claude Code window, across all four session shapes: host, local container, remote container, remote host. Image attaches each time; the kitty stays responsive during the (async) backend write.
- Plain shell window, <kbd>Ctrl-V</kbd> with an image → path at the prompt, no freeze.
- Remote session with the ssh ControlMaster killed mid-paste → the write times out, `notify_on_death` fires, an error line appears in the window; the kitty does not hang.
- Text-only clipboard → native passthrough. Empty clipboard → no-op.
- After a dozen pastes (mix of success and the killed-master failure), `pgrep -P <kitty-pid>` shows no leftover/zombie helper processes.

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

- `hop/kitten/paste/main.py`'s `handle_result` (and any callback it registers) makes no blocking subprocess or blocking clipboard call on the boss thread. Copying from a window in a hop session and pasting into another window of the same session no longer freezes the kitty, for both text and image clipboards.
- The clipboard read uses `kitty.clipboard.Clipboard(ClipboardType.clipboard).get_mime(...)` in-process, not `wl-paste` and not `kitten clipboard`. The `output` callback accumulates chunks and treats `b""` as end-of-stream.
- With an image on the clipboard, the kitten hands the bytes to `boss.run_background_process` (`stdin=<bytes>`, `notify_on_death=`); the helper does `backend.write_file` at `/tmp/hop-paste-<ns>.png` in the focused window's filesystem namespace and then `send-text --bracketed-paste=auto <path>`. Verified against host, local container, remote container, and remote host sessions with the kitty staying responsive throughout.
- The helper process is reaped by kitty — no zombie or leftover helper accumulates across repeated pastes, including failed ones.
- A non-image clipboard (no image MIME in `get_available_mime_types_for_paste()`) results in kitty's native `paste_from_clipboard`; an empty clipboard is a no-op.
- `CommandBackend.write_file` passes a `timeout=` to its runner call; a `subprocess.TimeoutExpired` becomes a `SessionBackendError` naming the path. The helper always exits, so `notify_on_death` always fires; a write failure or timeout is surfaced as a line in the target window, not silently dropped — verified by killing a remote session's ssh ControlMaster mid-paste.
- `hop/paste.py` exposes a kitty-free synchronous core (bytes + backend + `write_path` + `send_text` → write then paste) that the helper calls; `hop/clipboard.py` is removed or no longer imported by the kitten.
- `hop_spec.md`, `README.md`, `docs/ssh-devcontainer.md`, and `docs/devcontainer.md` reflect the in-process read, the kitty-supervised background write, and the visible-failure behaviour.
- New and updated tests follow the repo's no-mock conventions and pass under `make`.
- `bunx dust lint` passes for this task file.
