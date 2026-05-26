# Remove no-arg `hop open` and focus the editor via `hop term --role editor`

Make `hop open` require a target, and route the "focus or launch the session's shared Neovim" action through `hop term --role editor` instead. The current `hop open` (no args) → focus editor behavior is confusing because it makes `hop open` overloaded: with a target it parses+dispatches (URL / Rails ref / `path[:line]`), but with no target it does an unrelated focus-window action. Splitting the focus action onto `hop term --role <role>` keeps that command the single "focus/launch window by role" verb for every role including the editor.

## Background

Today `hop open` has two unrelated behaviors:

- `hop open` (no args) → `SharedNeovimEditorAdapter.focus(session)` — ensure the session's nvim exists, focus it.
- `hop open <target>` → parse the target (URL / `Controller#action` / `path[:line]`) and dispatch it: URLs to the session browser (with localhost translation), everything else to the shared nvim.

Per-role window focus already has a canonical command: `hop term --role <role>`. For `shell`, `test`, `server`, etc. it focuses an existing kitty window for that role or launches one. The browser has its own verb (`hop browser`). The editor is the only role where the focus action lives on `hop open` instead of `hop term`. That asymmetry is what makes the no-arg form surprising — readers expect `hop open` to mean "open something."

The proposal: `hop open` requires a target. `hop term --role editor` becomes the way to focus / launch the shared nvim — it delegates to the existing `SharedNeovimEditorAdapter.focus(session)`, which already handles the "launch if missing, focus if present, recreate if quit" lifecycle. Vicinae's `Hop editor` script switches to `hop term --role editor`.

## Design

### CLI surface

`hop open` requires `<target>`. argparse rejects bare `hop open` with the standard "the following arguments are required" message. No silent no-op, no implicit focus.

`hop term --role editor` is the new spelling for "focus or launch the session's nvim." It does *not* go through `kitty.ensure_terminal` — the editor's launch path (`SharedNeovimEditorAdapter._ensure_editor`) is editor-specific (it composes `<editor>; <shell>`, sets the `_hop_editor:<session>` sway mark, etc.) and reuses the deterministic per-session nvim listen socket. `focus_terminal` special-cases `role == EDITOR_ROLE` and delegates to `neovim.focus(session)` instead.

### Files to change

**Code:**

- `hop/commands/__init__.py:33-35` — `OpenCommand.target: str | None = None` → `target: str` (no default).
- `hop/cli.py:57-58` — `open_parser.add_argument("target", nargs="?")` → drop `nargs="?"`. Bare `hop open` becomes an argparse error.
- `hop/commands/open.py`:
  - Remove the `OpenNeovimAdapter` wider protocol (the `focus(session)` extension). Only `OpenTargetNeovimAdapter` (open_target) remains; rename to `OpenNeovimAdapter` for the CLI signature.
  - `open_target_in_session(...)`: `target` becomes `str` (non-optional); drop the `if target is None: neovim.focus(...)` branch and its module docstring lines.
- `hop/commands/term.py`:
  - Add `TermNeovimAdapter` protocol with `focus(session)`.
  - `focus_terminal` gains a `neovim: TermNeovimAdapter` keyword arg.
  - When `role == EDITOR_ROLE` (import from `hop.config`), call `neovim.focus(session)` and return — skip `terminals.ensure_terminal` and the trailing sway focus escalation (the editor adapter does its own sway focus through `_sway.focus_window`).
- `hop/app.py:419-425` — pass `neovim=services.neovim` to `focus_terminal`.
- `hop/vicinae.py:176-177` — `body = "exec setsid -f hop open\n"` → `body = "exec setsid -f hop term --role editor\n"`.
- `hop/editor.py:204-218` — update the two "Run `hop open` from a shell first" error messages in `BossKittyEditorIO.launch_editor` / `send_text_to_editor` to say `hop term --role editor` instead.
- `hop/app.py:330`, `hop/commands/session.py:161-162` — comments reference the new spelling.

**Tests:**

- `tests/test_cli.py:30` — drop `(["open"], OpenCommand())`. Add a case that bare `hop open` raises `SystemExit` from argparse (mirrors how other required-arg tests work in the suite, if any; otherwise just remove the bare-open case).
- `tests/test_app.py:768-778` (`test_execute_command_focuses_shared_editor_in_current_session`) — convert to `TermCommand(role="editor")`, asserting `services.neovim.focused_sessions == [...]`. Keep the workspace-not-switched and nested-cwd assertions.
- `tests/test_app.py:775` `OpenCommand()` literal → either remove or replace with `TermCommand(role="editor")`.
- `tests/test_open_command.py`:
  - Delete `test_no_target_focuses_session_editor` (lines 45-57) and `test_nested_directories_are_distinct_sessions` (lines 166-176) — both exercise the removed no-arg form.
  - Stub `StubNeovimAdapter` no longer needs `focus`; can drop the method.
- `tests/test_window_reuse.py`:
  - `test_repeated_hop_open_reuses_existing_editor` (lines 226-238), `test_hop_open_recreates_editor_after_quit` (lines 241-254), `test_session_switch_does_not_mix_editor_instances` (lines 364-378) — convert from `open_target_in_session(target=None, ...)` to `focus_terminal(..., role=EDITOR_ROLE, neovim=..., terminals=..., sway=...)`. The `IdempotentNeovimAdapter.focus` lifecycle remains identical, so the assertions don't change.
  - `test_hop_open_routes_target_to_existing_editor_without_relaunch` (lines 257-274) — first call becomes `focus_terminal(role=EDITOR_ROLE)`, second remains `open_target_in_session(target="...")`.
  - Rename the test-section comment "Editor reuse and recreation: hop open" to "Editor reuse and recreation: hop term --role editor".
- `tests/test_vicinae.py:86` — `"exec setsid -f hop open\n"` → `"exec setsid -f hop term --role editor\n"`.
- `tests/test_editor_internals.py:405,452,460` — error-message `match=` strings updated to the new "run `hop term --role editor`" wording.

**Docs:**

- `README.md:78` — vicinae bullet listing: drop `hop open` from the focus-window verbs; add nothing in its place (editor focus is `hop term --role editor`, already covered by `hop term --role <name>`).
- `README.md:291` — bullet rewritten as: "`hop open <target>` - route the target to the right place: a URL goes to the session browser (with the backend's localhost translation applied), a Rails `Controller#action` ref or `path[:line]` goes to the shared Neovim." (No "with no target" clause; no optional brackets.)
- `hop_spec.md:273` — replace `hop open (editor)` in the launcher dispatch list with `hop term --role editor`.
- `hop_spec.md:277-302` — rework the `hop open` synopsis: only `hop open <target>` exists; drop the no-arg example.
- `hop_spec.md:330` — keep the "no workspace switch" note for `hop open`; replace any "focus editor" framing.
- `hop_spec.md:447` — vicinae dispatch sentence: editor role dispatches via `hop term --role editor` now, not `hop open`.
- `hop_spec.md:596-603` — "editor is started when needed (e.g. via `hop term --role editor`)"; "next `hop term --role editor` recreates the editor"; bare `hop open` example removed.
- `docs/devcontainer.md:15,143,214,377` — replace bare `hop open` examples with `hop term --role editor`; the table row at line 15 stays mapped to "the backend's `editor` command" but the verb in the left column becomes `hop term --role editor`.
- `docs/ssh.md:76,111` — same swap (the line 69 example, `hop open ~/projects/foo/some/file.rb`, stays unchanged; only the bare `hop open` line at 111 changes).
- `.dust/principles/one-shared-editor-per-session.md:5` — replace `hop open` with `hop term --role editor` in the "should ensure the session editor exists, focus it..." sentence. The "direct file and file-plus-line targets into that shared instance" part stays scoped to `hop open <target>`.
- `.dust/facts/hop-target-dispatch-and-behavior-guarantees.md:9` — describe editor focus through `hop term --role editor`; `hop open` retains target dispatch only.
- `.dust/facts/hop-session-model-and-command-contract.md:5,15` — the command surface line and the no-workspace-switch note get the verb swapped.

## Task Type

implement

## Principles

- [One shared editor per session](../principles/one-shared-editor-per-session.md)
- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)

## Blocked By

(none)

## Definition of Done

- `hop open` with no arguments exits non-zero with argparse's standard "required" error; it does not focus the editor.
- `hop term --role editor` launches the shared Neovim if missing, focuses it if present, and recreates it after `:qa` — identical lifecycle guarantees to today's no-arg `hop open`.
- `hop open <target>` continues to dispatch URLs to the session browser (with backend localhost translation) and Rails refs / `path[:line]` to the shared Neovim.
- Vicinae's `Hop editor` script body is `exec setsid -f hop term --role editor`; the focus/launch behavior driven from vicinae is unchanged from the user's perspective.
- `BossKittyEditorIO` error messages tell users to "run `hop term --role editor` from a shell first" rather than `hop open`.
- README, `hop_spec.md`, `docs/devcontainer.md`, `docs/ssh.md`, the `one-shared-editor-per-session` principle, and the `hop-target-dispatch-and-behavior-guarantees` / `hop-session-model-and-command-contract` facts all reflect the new spelling.
- Test suite updated: removed no-arg `hop open` cases, new `hop term --role editor` cases cover editor reuse / recreation / cross-session isolation, vicinae script body assertion updated, editor error-message assertions updated.
- `make` is green.
