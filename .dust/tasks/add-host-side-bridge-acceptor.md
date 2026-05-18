# Add host-side bridge acceptor

Add a unix-socket HTTP acceptor to `hopd` so editor plugins inside non-host backends can dispatch `hop` CLI calls back to the host.

## Background

Editor plugins like `vigun` invoke `hop run --role test "<cmd>"` from inside the editor. With the `host` backend this works because the editor runs on the host. With `devcontainer` or `ssh` backends the editor runs on the backend side, where neither `hop` nor host kitty/Sway state is reachable. The [bridge design](../ideas/bridge-hop-cli-calls-from-non-host-backends.md) introduces a host-side acceptor and a small backend-side shim that forwards CLI invocations over a unix socket.

This task implements only the host side: the acceptor, the wire protocol, and session-identity resolution from the focused Sway window. The backend shim, recipe templates, and user-facing docs are out of scope and tracked separately by the umbrella idea.

## Design

### Wire protocol — HTTP over `AF_UNIX`

The acceptor speaks HTTP/1.0 on `AF_UNIX` at `$XDG_RUNTIME_DIR/hop/api.sock`. HTTP is the transport because the realistic shim dependency is `curl --unix-socket` — present in essentially every dev container (verified: `ruby:slim` has `curl` but neither `socat` nor `nc`). Raw-socket framing would require shipping `socat`/`nc` we can't assume.

**Request** — `POST /call`, `Content-Type: application/octet-stream`. Body is the shim's argv NUL-separated, no trailing NUL required. The first element is ignored (it's the shim's own `$0`); subsequent elements are the `hop` arguments.

**Response** — always `200 OK` on a successful dispatch (HTTP status reflects transport success, not hop's exit code). Body is the hop subprocess's stdout bytes. Two response headers carry the rest:

- `X-Hop-Exit: <integer>` — hop's exit code.
- `X-Hop-Stderr: <base64>` — base64-encoded stderr bytes. Base64 because stderr can contain anything and HTTP headers are text-only.

On acceptor-level failures (no focused session, malformed argv, hop subprocess could not be launched) the acceptor returns `400` (caller's fault — bad context) or `500` (acceptor's fault) with a plain-text `text/plain; charset=utf-8` body. The shim treats any non-2xx as a hard failure and routes the body to its own stderr.

### Session identity — focused Sway window

Each accepted call resolves to a `ProjectSession` by querying Sway for the focused window's marks:

1. `SwayIpcAdapter.list_windows()` already exposes `focused: bool` and `marks: tuple[str, ...]` on each `SwayWindow` (`hop/sway.py:59`). The acceptor walks the list and picks the focused window.
2. The focused window must carry an `_hop_editor:<session>` mark (set by `hop/editor.py:368` / `:396` via `_editor_mark`). The acceptor strips the `_hop_editor:` prefix to recover the session name.
3. Session name → `SessionState` via `load_sessions()` (`hop/state.py:118`). Session state → `ProjectSession` via `resolve_project_session(state.project_root)` (`hop/session.py:33`). Mirrors the pattern at `hop/focused.py:73`.

Failure modes (each returns `400` with a plain-text body):

- No focused window: `no focused Sway window`
- Focused window has no `_hop_editor:` mark: `focus your editor window first; bridge calls from role terminals aren't supported yet`
- Mark points to a session not in `load_sessions()`: `session <name> from focused window is not in hop state`

This deliberately rejects calls from role terminals (which don't carry session marks in Sway today). A future enhancement can add per-session-kitty-socket probing to support role-terminal-focused calls; out of scope here.

### Dispatch

For each call the acceptor:

1. Parses argv from the request body by `body.split(b'\x00')`.
2. Spawns `subprocess.run([sys.executable, "-m", "hop", *argv[1:]], cwd=session.project_root, input=b"", capture_output=True)`. Using `sys.executable -m hop` rather than the `hop` script in PATH avoids surprises when the daemon's PATH differs from the operator's.
3. Builds the response: status `200`, body is `result.stdout`, `X-Hop-Exit: result.returncode`, `X-Hop-Stderr: base64(result.stderr)`.

The acceptor buffers full stdout/stderr — no streaming. Acceptable for the targeted commands (`hop run` returns ~32 bytes; `hop edit` returns nothing; `hop tail`'s payload is the wrapped command's recent output, which is small).

### Files changed

- **`hop/bridge.py` (new)** — the acceptor. Public surface:
  - `BridgeServer` — a `socketserver.ThreadingMixIn` + `socketserver.UnixStreamServer` subclass with a `BridgeRequestHandler(BaseHTTPRequestHandler)`. The server takes `socket_path: Path`, `sway_source: Callable[[], Sequence[SwayWindow]]`, and `dispatcher: Callable[[ProjectSession, Sequence[str]], CompletedProcess[bytes]]` in the constructor. The callable indirection is for tests.
  - `serve_forever(socket_path, sway_source, dispatcher) -> None` — top-level convenience that unlinks any stale socket file, binds, and runs `serve_forever()` on the server. Used by `hopd`.
  - `dispatch_via_subprocess(session: ProjectSession, argv: Sequence[str]) -> CompletedProcess[bytes]` — the production dispatcher. Lives here so it has a single integration test.
- **`hop/daemon.py`** — extend `main()` to start a `threading.Thread(target=bridge.serve_forever, daemon=True)` after the daemon lock is acquired. The thread terminates when `hopd` exits. The socket path is `Path(XDG_RUNTIME_DIR) / "hop" / "api.sock"`; the directory is created (`mkdir(parents=True, exist_ok=True)`) and any stale socket file is unlinked before bind.
- **`hop/sway.py`** — no API change. The bridge consumes `list_windows()` directly.

### What this task does *not* do

- No backend-side shim or shim install step. Tracked separately.
- No devcontainer/ssh recipe templates or user-facing doc updates.
- No support for bridge calls originating from role terminals (Sway marks alone can't disambiguate which session a kitty role window belongs to).
- No streaming responses.

## Test Plan

Per the project's no-mocks convention (see `tests/test_editor.py:66-113` for the fake-adapter pattern), tests use:

- **Real unix sockets** under `tmp_path` for transport. **Real `curl`** invoked via `subprocess.run` (`shutil.which("curl")` — skip the suite if absent, never mock).
- A **`StubSwayAdapter`-style fake** for `sway_source`. Each test sets up a list of `SwayWindow` records with desired marks and focused state.
- A **closure-recorded `dispatcher` fake** that returns scripted `CompletedProcess` objects and records the `session` / `argv` it was called with. The production `dispatch_via_subprocess` gets a separate integration test that runs a real `python -m hop --help` subprocess.

Test cases in `tests/test_bridge.py`:

1. **Round-trip with focused editor window** — `BridgeServer` started in a thread, request via real `curl --unix-socket`. Asserts: response body equals scripted stdout, `X-Hop-Exit: 0`, `X-Hop-Stderr: ""`.
2. **Non-zero exit propagates** — dispatcher returns `CompletedProcess(returncode=2, stdout=b"out", stderr=b"err")`. Asserts: `X-Hop-Exit: 2`, stderr decodes back to `b"err"`.
3. **No focused window** — sway source returns all-unfocused. Asserts: `400`, body mentions `no focused Sway window`.
4. **Focused window has no `_hop_editor:` mark** — focused window has unrelated marks. Asserts: `400`, body mentions the editor-window requirement.
5. **Mark points to nonexistent session** — focused window mark `_hop_editor:ghost`, `load_sessions()` returns `{}`. Asserts: `400`, body names the missing session.
6. **Dispatcher receives the resolved session** — dispatcher is a closure recording its `session` argument. Asserts: `session.project_root` equals the path stored under the named session in `load_sessions()`.
7. **Stderr round-trips through base64 header** — stderr containing binary bytes (NULs, non-UTF8). Asserts: client base64-decodes the header back to the original bytes.
8. **`dispatch_via_subprocess` integration** — calls the production dispatcher directly with `argv=["hop", "--help"]` and a `ProjectSession` for `tmp_path`. Asserts: returncode is 0, stdout contains `usage:`.
9. **Stale socket file is unlinked** — pre-create the socket path as a regular file. `serve_forever` removes it and binds successfully.

## Task Type

implement

## Principles

- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)

## Blocked By

(none)

## Definition of Done

- `hop/bridge.py` exists with `BridgeServer`, `serve_forever`, `dispatch_via_subprocess` as described.
- `hop/daemon.py` starts the bridge acceptor in a daemon thread after lock acquisition; the thread terminates cleanly on `hopd` shutdown.
- All test cases above pass.
- No new runtime dependencies beyond the Python stdlib.
- `make` (the default target — test, typecheck, lint, format-check) is green.
