# Mirror Kitty's native remote-control endpoint selection and actionable timeout errors

`hop` should mirror Kitty's endpoint lookup order for remote control.
It should also turn controlling-terminal timeouts into actionable endpoint errors.

## Problem

Running `hop` from inside Kitty can currently fail after a full 2 second delay with:

```text
Timed out waiting for Kitty to respond.
```

This is not limited to `hop` with no arguments. Any command path that needs Kitty remote control can hit the same failure, including session entry, role terminal reuse, command routing, editor reuse, window inspection, and session teardown.

## Verified Kitty Behavior

Kitty's own remote-control client resolves endpoints in this order:

1. Use `KITTY_LISTEN_ON` if it is present.
2. Otherwise send the request over the controlling terminal.

The official docs also confirm three details that matter here:

- `KITTY_LISTEN_ON` is only present when Kitty remote control is exposed over a socket, either through global `listen_on` / `kitty --listen-on` configuration or through `launch --allow-remote-control`, which provides an `fd:` endpoint to the child process.
- `allow_remote_control=socket-only` is a supported mode. In that mode, socket requests are accepted and TTY requests are denied.
- Kitty supports `unix:`, abstract `unix:@...`, `fd:`, and documented `tcp:` listen addresses.

That means the absence of `KITTY_LISTEN_ON` in a normal Kitty shell is expected unless the shell was launched with a socket-backed remote-control endpoint. It also means "inside Kitty" is not enough information to decide which transport `hop` should use.

## Codebase Context

- `hop/kitty.py` chooses the default transport in `_build_default_transport()`.
- That function currently returns `ControllingTtyKittyTransport` whenever `KITTY_WINDOW_ID` is present, otherwise `SocketKittyTransport`.
- `ControllingTtyKittyTransport.send_command()` writes a Kitty remote-control escape sequence to `/dev/tty` and `_read_tty_chunk()` waits up to `COMMAND_TIMEOUT_SECONDS = 2.0` for a response.
- If Kitty ignores the TTY request, `_read_tty_chunk()` raises the bare `KittyConnectionError("Timed out waiting for Kitty to respond.")`.
- `hop/cli.py` prints `HopError` messages verbatim, so the low-level timeout string becomes the entire user-facing error.
- `SocketKittyTransport` already supports `fd:` and local `unix:` endpoints, including abstract UNIX sockets via `unix:@...`, but it does not implement Kitty's documented `tcp:` addresses.
- `KittyRemoteControlAdapter._launch_window()` and the shared-editor launcher both set `allow_remote_control=True`, so hop-managed Kitty windows are already designed to run with remote control enabled.
- The existing test suite encodes the current transport choice in `tests/test_kitty_internals.py::test_build_default_transport_prefers_controlling_tty_inside_kitty`.

## Refined Proposal

Change `hop` to mirror Kitty's native endpoint selection order for the endpoint types `hop` supports today:

1. Prefer `SocketKittyTransport` whenever `KITTY_LISTEN_ON` is present and points to a supported socket transport.
2. Fall back to `ControllingTtyKittyTransport` only when no supported socket endpoint is available and `hop` is running inside Kitty.
3. Keep endpoint failures specific. If the chosen socket or TTY endpoint fails, surface that exact failure instead of silently retrying a different transport.

This fixes the broken `socket-only` case, aligns `hop` with Kitty's documented lookup order, and avoids masking stale or invalid endpoint configuration.

## User-Facing Error Behavior

When `hop` has to use the controlling terminal and Kitty does not answer, the resulting error should explain what the user can do next instead of only reporting a timeout. The message should make it clear that `hop` could not reach a usable Kitty remote-control endpoint and should point the user toward one of the supported fixes:

- expose a socket endpoint so `KITTY_LISTEN_ON` is available, or
- enable controlling-terminal remote control in Kitty and restart it.

The CLI does not need extra error handling for this. `hop` already prints `HopError` messages directly, so the right place to make the error actionable is in the Kitty adapter layer.

## Scope Boundaries

- Password-protected Kitty remote control is out of scope for this fix. Kitty's encrypted password flow uses `KITTY_PUBLIC_KEY` and a different request format; this proposal is about endpoint selection and actionable failures for the current local unauthenticated flows.
- This proposal should not introduce silent cross-transport retries. A bad socket endpoint and a blocked TTY endpoint should fail differently.

## Tests And Docs To Update

- Update the transport-selection test to cover socket-first precedence when both `KITTY_LISTEN_ON` and `KITTY_WINDOW_ID` are set.
- Add coverage for the improved controlling-terminal timeout error text.
- Preserve existing socket transport coverage for `fd:` and `unix:` endpoints.
- Document that `hop` requires a reachable Kitty remote-control endpoint and that socket-backed remote control is a supported configuration.

## Open Questions

### Should this change also add support for Kitty's documented `tcp:` listen addresses?

#### Option: Keep this change scoped to local `fd:` and `unix:` endpoints

Limit the socket-first behavior to the socket types `hop` already supports and treat `tcp:` support as a separate idea. This keeps the change focused on the local desktop workflows the project already targets, but it means the refined implementation should explicitly avoid regressing into a broken `tcp:` path.

#### Option: Add `tcp:` support as part of the same change

Extend `SocketKittyTransport` to parse Kitty `tcp:host:port` listen addresses and connect over TCP so `hop` matches Kitty's documented socket surface. This closes a compatibility gap, but it widens the implementation and test surface beyond the original timeout bug.
