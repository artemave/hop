# Add hop move command with a dmenu vicinae entry

Add a `hop move <session_name>` CLI that relocates the focused Sway window to `p:<session>`. Surface it via a single `hop-move` vicinae entry that dmenu-prompts for the target session.

## Background

`hop` curates session workspaces but offers no first-class way to send a foreign window (Bitwarden, a chat client, a one-off browser window) onto a session's workspace. `$mod+Shift+<n>` doesn't apply: `p:<session>` workspaces are named, not numbered. The only Sway-native option is dropping into the command bar and typing `move container to workspace "p:foo"` by hand — nobody actually does that.

`hop move <session_name>` is the named action: focus the window you want to relocate, invoke `hop move foo`, and Sway moves it to `p:foo`. Same primitive Sway already exposes (`move container to workspace`), wrapped in hop's session-name vocabulary so the user never has to spell out the destination workspace's actual name.

A single `hop-move` vicinae entry surfaces this in the launcher. Activating it opens a `vicinae dmenu` pick over the live session list and then runs `hop move <chosen>` — the same pattern `hop-create` uses to delegate "pick a directory" to dmenu rather than enumerating one entry per candidate.

## Codebase Context

- **Sway IPC**: `SwayIpcAdapter.move_window_to_workspace(window_id, workspace_name)` already exists (`hop/sway.py:216`). Issues `[con_id=N] move container to workspace "name"`. Default Sway behavior: focus stays on the moved container, but since the container is now on the destination workspace, the user's view of the *current* workspace shows the window as having vanished. The user stays on the source workspace; this matches Sway's native `move` semantics.
- **Focused window**: `SwayWindow.focused: bool` (`hop/sway.py:59`); pick the unique one from `sway.list_windows()`. Pattern is already used in `hop/focused.py`.
- **Session listing**: `hop/commands/session.py::list_sessions(sway)` returns `Sequence[SessionListing]` with `name`, `workspace`, `project_root`. `hop` already uses this for `hop list` and for vicinae's switch-script enumeration.
- **Session workspace prefix**: `SESSION_WORKSPACE_PREFIX = "p:"` (`hop/commands/session.py`). `p:<name>` ↔ session `<name>`.
- **Vicinae script generation**: `hop/vicinae.py::compute_target_scripts` (line 61) builds the per-state set. Switch entries are emitted by `_switch_script` (line 268) for every session except the focused one. Move entries follow the exact same enumeration: same loop, same exclusion rule.
- **Switch script shape** (`_switch_script`, vicinae.py:268-284) is the right template. It uses `_render_no_cd` because the action is a pure Sway op, not a project subprocess, and sets `package_name=""` because the title (`Hop switch to <name>`) already names the target. `hop-move-<session>` follows that same shape.
- **CLI plumbing**: subparser in `hop/cli.py::build_parser`, dataclass in `hop/commands/__init__.py`, match arm in `hop/cli.py::parse_command`, executor entry in `hop/app.py::execute_command`. `SwitchSessionCommand` is the closest analogue — it also takes a `session_name` argument.
- **HopError surface**: `hop/cli.py::main` (lines 144-154) catches `HopError`, prints to stderr, and shows a kitten popup when non-interactive — exactly what's needed for "no such session" / "no focused window" failures invoked from vicinae.

### Interaction with the mark reconciler

This task pairs with `reconcile-editor-and-browser-marks-on-sway-window-events.md`. When `hop move bar` relocates a window holding `hop:editor:foo` from `p:foo` to `p:bar`, the reconciler observes the resulting `window` event and clears the now-misplaced mark — the moved editor becomes a regular window on `p:bar`, just like a raw Sway move. The two tasks are independently shippable but produce the cleanest semantics when both are in.

## Design

### CLI surface

```bash
hop move <session_name>
```

- Resolves `session_name` against the live session list (`list_sessions`). If no match, `HopError("hop move: no session named <name>.")`.
- Reads the focused window from `sway.list_windows()`. If none, `HopError("hop move: no focused window.")`.
- Calls `sway.move_window_to_workspace(focused.id, f"p:{session_name}")`.
- No focus follow. Sway's default for `move container to workspace` is "stay on the source workspace, focus stays on the moved container," which (since the container is no longer on the source ws) effectively means the user's current view shows the window gone. To follow, the user chains `hop switch <session>`.

#### Self-move

If the user invokes `hop move foo` while the focused window is already on `p:foo`, the move is a no-op from Sway's perspective. Don't special-case it. Same shape as `hop switch <focused_session>` — Sway handles the redundancy silently.

### Command dataclass + CLI

`hop/commands/__init__.py`:

```python
@dataclass(frozen=True, slots=True)
class MoveCommand:
    session_name: str
```

Append `MoveCommand` to the `Command` union.

`hop/cli.py::build_parser`:

```python
move_parser = subparsers.add_parser("move")
move_parser.add_argument("session_name")
```

`hop/cli.py::parse_command`:

```python
case "move":
    return MoveCommand(session_name=namespace.session_name)
```

### Executor

`hop/commands/move.py` (new file), structured like `hop/commands/term.py`:

```python
class MoveSwayAdapter(Protocol):
    def list_windows(self) -> Sequence[SwayWindow]: ...
    def move_window_to_workspace(self, window_id: int, workspace_name: str) -> None: ...

class MoveSessionsAdapter(Protocol):
    def list_sessions(self) -> Sequence[SessionListing]: ...

def move_focused_window(*, session_name: str, sway: MoveSwayAdapter, sessions: MoveSessionsAdapter) -> None:
    listings = sessions.list_sessions()
    if not any(listing.name == session_name for listing in listings):
        raise HopError(f"hop move: no session named {session_name!r}.")
    focused = next((w for w in sway.list_windows() if w.focused), None)
    if focused is None:
        raise HopError("hop move: no focused window.")
    sway.move_window_to_workspace(focused.id, f"{SESSION_WORKSPACE_PREFIX}{session_name}")
```

`hop/app.py::execute_command`:

```python
case MoveCommand():
    move_focused_window(
        session_name=command.session_name,
        sway=services.sway,
        sessions=services.sessions,
    )
    return 0
```

The exact services adapter for `list_sessions` may need a tiny shim — confirm at implementation time whether `services` already exposes a session-listing surface or whether `list_sessions(sway=...)` is called directly.

### Vicinae script entry

A single `hop-move` entry that, on activation, dmenu-prompts the user for the target session and then invokes `hop move <chosen>`. Same shape as the existing `hop-create` (vicinae.py:207-265): one script per topic that delegates the actual pick to a `vicinae dmenu` subprocess.

Always emitted, regardless of focused workspace. The dmenu list is whatever `hop list` outputs (one session name per line). If there are no live sessions, the script exits cleanly with no pick.

```python
MOVE_FILENAME = "hop-move"

def _move_script() -> GeneratedScript:
    return GeneratedScript(
        filename=MOVE_FILENAME,
        content=(
            "#!/usr/bin/env bash\n"
            "# @vicinae.schemaVersion 1\n"
            "# @vicinae.title Hop move window to session\n"
            "# @vicinae.description Move the focused window to a hop session's workspace.\n"
            "# @vicinae.packageName \n"
            f"# @vicinae.icon {_ICON_PATH}\n"
            "# @vicinae.mode silent\n"
            "\n"
            "set -euo pipefail\n"
            "\n"
            'candidates=$(hop list)\n'
            'if [ -z "$candidates" ]; then\n'
            "    exit 0\n"
            "fi\n"
            "\n"
            'if ! chosen=$(printf \'%s\\n\' "$candidates" '
            '| vicinae dmenu --placeholder "Move window to session"); then\n'
            "    exit 0\n"
            "fi\n"
            "\n"
            'if [ -z "$chosen" ]; then\n'
            "    exit 0\n"
            "fi\n"
            "\n"
            "exec setsid -f hop move \"$chosen\"\n"
        ),
    )
```

`MOVE_FILENAME = "hop-move"` — declared alongside `KILL_FILENAME = "hop-kill"` (vicinae.py:29).

Emission in `compute_target_scripts`: append the move script once, near the unconditional `_create_script()` call at vicinae.py:101. Both are "always present" entries that delegate session/destination choice to dmenu.

`setsid -f` is included per the existing rationale (vicinae.py:172-176): vicinae SIGTERMs the action process when its UI closes, and we don't want that to kill `hop move` mid-IPC. Cheap insurance, matches the surrounding pattern.

#### Why not exclude the focused session from the dmenu list

`hop list` doesn't currently expose a "focused" indicator and the script doesn't have sway IPC access on its own. The simplest mode of failure is benign: if the user picks the session they're already on, `hop move <focused_session>` is a no-op (the focused window's workspace already matches). Filtering would require either a new `hop list --exclude-focused` flag or shelling out to `swaymsg`/`jq`. Not worth the added surface. Mirror hop-create's "let the user pick, no filter" stance.

### Spec / docs

`hop_spec.md`: add a `hop move <session_name>` entry to the commands section. Behavior: "Moves the currently-focused Sway window to the named session's `p:<session>` workspace. Errors if the session does not exist or no window is focused. Does not switch the user's view to the destination."

`README.md`: extend the commands list with a one-line description.

## Test Plan

Real-behavior tests, no mocks. Existing stubs (`StubSwayAdapter` in `tests/test_editor.py`, `tests/test_app.py`; `tests/test_vicinae.py`'s fixtures) record calls and can be extended.

1. **`parse_command(["move", "foo"])` → `MoveCommand(session_name="foo")`** (`tests/test_cli.py`).
2. **Move relocates focused window to `p:<session>`** (`tests/test_move_command.py`, new):
   - `StubSessionsAdapter` reports sessions `["foo", "bar"]`. `StubSwayAdapter` reports a focused window with `id=42` on workspace `2`.
   - `move_focused_window(session_name="foo", ...)` results in `sway.moved_windows == [(42, "p:foo")]`.
3. **Unknown session raises HopError** — `session_name="ghost"` with no matching listing. `HopError` raised, no `move_window_to_workspace` call.
4. **No focused window raises HopError** — no window has `focused=True`. `HopError` raised, no move call.
5. **Self-move is one IPC call** — focused window already on `p:foo`, call `hop move foo`. Asserts `move_window_to_workspace(id, "p:foo")` is still issued (Sway no-ops it). No special-case in the executor.
6. **`execute_command(MoveCommand("foo"), ...)`** (`tests/test_app.py`) routes through `services.sway` and `services.sessions` and the move lands.
7. **Vicinae script set always contains a single `hop-move` entry** (`tests/test_vicinae.py`):
   - On `p:foo` with sessions `["foo", "bar"]`, on workspace `2` with sessions `["foo"]`, and with no sessions at all — `compute_target_scripts(...)` always includes exactly one `GeneratedScript(filename="hop-move", ...)`.
   - The script body contains `hop list`, `vicinae dmenu --placeholder "Move window to session"`, and `exec setsid -f hop move "$chosen"`.
8. **CLI integration with HopError surface** (`tests/test_cli.py` or `tests/test_app.py`): unknown-session call returns 1, error printed to stderr, popup invoked when non-interactive.

## Out of scope

- Picking a window other than the focused one. The focused-window convention is the whole interaction model.
- Following the moved window onto the destination workspace. Compose with `hop switch <session>` if you want to follow.
- Sway keybindings for `hop move`. Document the command; the user wires their own bindings.

## Task Type

implement

## Principles

- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [No defensive don'ts](../principles/no-defensive-donts.md)

## Blocked By

(none)

## Definition of Done

- `hop move <session_name>` is a new CLI subcommand.
- It moves the currently focused Sway window to `p:<session>`. Errors with a `HopError` if the session does not exist or no window is focused.
- Vicinae emits exactly one `hop-move` entry — always present, regardless of focused workspace. Activation dmenu-prompts the user over `hop list` output and runs `hop move <chosen>`.
- `hop_spec.md` and `README.md` describe `hop move`.
- All test cases in the Test Plan pass.
- `make` is green.
