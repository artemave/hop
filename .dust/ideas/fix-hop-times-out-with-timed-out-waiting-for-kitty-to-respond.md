# Fix: hop times out with "Timed out waiting for Kitty to respond"

Running `hop` from a Kitty terminal fails with "Timed out waiting for Kitty to respond" when Kitty remote control is not enabled.

## Context

Running `hop` with no arguments from inside a Kitty terminal currently fails:

```
❯ hop
Timed out waiting for Kitty to respond.
```

The command takes ~2 seconds (the full `COMMAND_TIMEOUT_SECONDS` timeout) before printing the error.

## Root Cause

`_build_default_transport()` in `hop/kitty.py` selects the transport by checking `KITTY_WINDOW_ID`. When that env var is set it uses `ControllingTtyKittyTransport`, which sends a remote control escape sequence to `/dev/tty` and waits up to 2 seconds for Kitty to reply. If Kitty's remote control is not enabled (or is set to `socket-only`), Kitty silently ignores the escape sequence, the `select()` call times out, and the bare error "Timed out waiting for Kitty to respond." is raised.

`KITTY_WINDOW_ID` is set by Kitty unconditionally in every window it spawns — it is not a reliable indicator that remote control is active.

The error also violates the `actionable-errors` principle: it tells the user what went wrong but not what to do next.

## Relevant Code

- `hop/kitty.py:27` — `COMMAND_TIMEOUT_SECONDS = 2.0`
- `hop/kitty.py:314–317` — `_build_default_transport()`
- `hop/kitty.py:464–469` — `_read_tty_chunk()` raises the timeout error
- `hop/kitty.py:253–256` — `ControllingTtyKittyTransport.send_command()` calls `_read_until`

## Open Questions

### Should the timeout error message tell the user how to fix the Kitty configuration?

#### Option: Improve the message at the raise site

Change the message in `_read_tty_chunk` to include a setup hint:

```
Timed out waiting for Kitty to respond.
Make sure `allow_remote_control yes` is set in kitty.conf and Kitty has been restarted.
```

#### Option: Catch and augment the error at the CLI boundary

Catch `KittyConnectionError` in `cli.py` and append setup instructions to the message there, keeping the low-level raise site clean.

### Should hop fall back to SocketKittyTransport when the TTY transport times out?

#### Option: Prefer socket when KITTY_LISTEN_ON is set

Change `_build_default_transport()` to check `KITTY_LISTEN_ON` first, falling back to the TTY transport only when the socket env var is absent. No retry cost; covers the common case where both env vars are present.

#### Option: Try TTY first, fall back to socket on timeout

On `KittyConnectionError` from `ControllingTtyKittyTransport`, silently retry via `SocketKittyTransport`. Smooth UX but masks misconfiguration.

#### Option: No fallback — improve the error message only

Fallback adds complexity and hides config problems. Fix the error message and let the user correct `kitty.conf`.

### Should hop validate that Kitty remote control is enabled before attempting commands?

#### Option: Add a preflight probe

Issue a lightweight Kitty command (e.g. `version`) on startup; if it fails, raise a clear `KittyRemoteControlNotEnabled` error with setup instructions before any real work begins.

#### Option: No preflight — rely on the first real command failing

Avoid the extra round-trip and make the natural failure error message actionable instead.
