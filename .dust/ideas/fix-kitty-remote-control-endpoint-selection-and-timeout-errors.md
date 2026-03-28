# Fix Kitty remote-control endpoint selection and timeout errors

`hop` should stop choosing the controlling TTY when Kitty already exposed a socket endpoint.
It should also turn genuine remote-control failures into actionable errors.

## Problem

Running `hop` from inside Kitty can currently fail after a full 2 second delay with:

```text
Timed out waiting for Kitty to respond.
```

This is not limited to `hop` with no arguments. Any command path that needs Kitty remote control can hit the same failure, including session entry, role terminal reuse, command routing, editor reuse, window inspection, and session teardown.

## Codebase Context

- `hop/kitty.py` currently chooses the default transport in `_build_default_transport()`.
- That function returns `ControllingTtyKittyTransport` whenever `KITTY_WINDOW_ID` is present, otherwise `SocketKittyTransport`.
- `ControllingTtyKittyTransport.send_command()` writes a Kitty remote-control escape sequence to `/dev/tty` and `_read_tty_chunk()` waits up to `COMMAND_TIMEOUT_SECONDS = 2.0` for a response.
- If Kitty ignores the TTY request, `_read_tty_chunk()` raises the bare `KittyConnectionError("Timed out waiting for Kitty to respond.")`.
- `hop/cli.py` prints `HopError` messages verbatim, so the low-level timeout string becomes the entire user-facing error.
- `SocketKittyTransport` already supports both `fd:` and `unix:` values from `KITTY_LISTEN_ON`.
- The existing test suite encodes the current transport choice in `tests/test_kitty_internals.py::test_build_default_transport_prefers_controlling_tty_inside_kitty`.

## External Behavior To Align With

Kitty's own `kitten @` lookup order checks `KITTY_LISTEN_ON` before falling back to the controlling terminal. The official remote-control docs also show `allow_remote_control=socket-only --listen-on ...` as a supported setup, which means "inside Kitty" is not equivalent to "must use the controlling TTY".

That makes `KITTY_WINDOW_ID` the wrong signal for transport selection. It identifies the current Kitty window, but it does not tell `hop` which remote-control endpoint is actually usable.

## Proposal

Change `hop` to mirror Kitty's endpoint selection order:

1. Prefer `SocketKittyTransport` whenever `KITTY_LISTEN_ON` is present.
2. Fall back to `ControllingTtyKittyTransport` only when no socket endpoint is available and `hop` is running inside Kitty.
3. Keep endpoint failures specific. If the chosen socket or TTY endpoint fails, surface that exact failure instead of silently retrying a different transport.

This keeps `hop` aligned with Kitty's native behavior, fixes the broken `socket-only` case, and avoids masking stale or invalid environment variables.

## User-Facing Error Behavior

When `hop` does have to use the controlling terminal and Kitty does not answer, the resulting error should explain what the user can do next instead of only reporting a timeout. The message should make it clear that `hop` could not reach a usable Kitty remote-control endpoint and should point the user toward one of the supported fixes:

- expose a socket endpoint so `KITTY_LISTEN_ON` is available, or
- enable controlling-terminal remote control in Kitty and restart it.

The CLI does not need extra error handling for this. `hop` already prints `HopError` messages directly, so the right place to make the error actionable is in the Kitty adapter layer.

## Tests And Docs To Update

- Update the transport-selection test to cover socket-first precedence when both `KITTY_LISTEN_ON` and `KITTY_WINDOW_ID` are set.
- Add coverage for the improved timeout error text.
- Preserve existing socket transport coverage for `fd:` and `unix:` endpoints.
- Update the user-facing docs to state that `hop` requires a reachable Kitty remote-control endpoint and that socket-based remote control is a supported configuration.

## Open Questions

### Should this fix also support password-protected Kitty remote control?

#### Option: Keep this fix scoped to non-password endpoints

Treat encrypted password mode as a separate feature. This task stays focused on endpoint selection and actionable failures for the already-supported unauthenticated flows (`allow_remote_control yes`, `socket`, or `socket-only`).

#### Option: Add password support as part of the same change

Implement Kitty's encrypted remote-control protocol and integrate password discovery. This would widen compatibility, but it is materially larger than the transport-selection bug described here.
