# Make editor open-file keystrokes configurable

Promote the editor adapter's hardcoded open-file keystroke sequence to a config field on `[windows.editor]`, defaulting to today's nvim bytes. With it in place, any TUI editor (vim, helix, kakoune, emacs -nw, ...) is reachable without code changes.

## Background

Today `hop edit <file>[:<line>]` and the `kitten/hints` dispatch path drive the session's editor via `KittyEditorIO.send_text_to_editor`, which writes raw bytes to the editor's kitty window. The byte sequence is built by `_build_open_keystrokes(path, line_number)` in `hop/editor.py:398-412`, which is hardcoded to vim/nvim syntax:

```python
quoted = path.replace("'", "''")
sequence = f"{_NORMAL_MODE}:exec 'drop '.fnameescape('{quoted}'){_CR}"
if line_number is not None:
    sequence += f":{line_number}{_CR}"
```

Nothing else in the editor adapter is nvim-specific — `launch_editor` runs a configurable command (`hop/editor.py:332`, default `"nvim"`), window discovery is by sway `app_id` (`EDITOR_OS_WINDOW_NAME = "hop:editor"`), and focus/move use plain sway IPC. So the keystroke builder is the only thing standing between hop and TUI-editor-agnosticism. The README currently lists Neovim under **Requirements**; with this change it becomes the default editor, not a hard dependency.

The architectural ceiling: GUI editors (VSCode, Zed, etc.) need a different launch path (separate sway window, not a child of kitty) and a different open-file mechanism (`code -g path:line`, not stdin keystrokes). Those are explicitly out of scope here — this task targets only TUI editors that fit the existing "child process inside a kitty window driven by stdin" model.

## Design

### Config surface

`WindowConfig` (`hop/config.py:39-50`) grows two optional fields, only meaningful on the `editor` role:

- `open_keys: str | None = None` — keystroke template applied when the open target has no line number. Template placeholders: `{path}`.
- `open_keys_with_line: str | None = None` — keystroke template applied when the open target has a line number. Template placeholders: `{path}`, `{line}`.

Both are optional. When omitted, the editor adapter uses built-in defaults that reproduce today's nvim sequence exactly:

- `open_keys` default: `"\x1b:exec 'drop '.fnameescape('{path}')\r"`
- `open_keys_with_line` default: `"\x1b:exec 'drop '.fnameescape('{path}')\r:{line}\r"`

Two fields rather than one with an "omit on no line" rule keeps each template literal and trivially copy-pastable into a config; the template language stays "Python `str.format` with `{path}` and `{line}`", no conditional syntax to invent.

### User config example

A user pointing hop at helix would write (in `~/.config/hop/config.toml` or a project's `.hop.toml`):

```toml
[windows.editor]
command             = "helix"
open_keys           = "\u001b:open {path}\r"
open_keys_with_line = "\u001b:open {path}:{line}\r"
```

TOML basic strings disallow literal control bytes, so Escape has to be written as `\u001b` (TOML only defines `\b \t \n \f \r \" \\ \uXXXX \UXXXXXXXX` — no `\x1b` or `\e`). Reading the helix template left to right: `\u001b` drops helix out of insert mode in case the user was mid-edit when the kitten dispatched, `:` enters helix's command mode, `open {path}` is the open-file command, `\r` submits. `command = "helix"` replaces the default `"nvim"` launch command; the two template fields replace the default keystroke bytes.

A minimal vim-on-nvim swap (same syntax, different binary) only needs the `command` line — the default templates are vim-compatible:

```toml
[windows.editor]
command = "vim"
```

And the all-defaults nvim case stays a no-op: no `[windows.editor]` block needed at all.

### Path substitution

`{path}` substitutes the raw path string. The user's template owns escaping for their editor's command-line conventions. The nvim default's `'{path}'` wrapping relies on vim's single-quoted string semantics, so paths containing literal `'` would land as a stray quote in the rendered template — to preserve today's exact behavior, the *substitution step* doubles single quotes specifically for the `{path}` slot. Document this explicitly in the README's editor section so users writing non-vim templates know `'` becomes `''` before substitution; templates that don't wrap `{path}` in `'...'` aren't affected (the doubling is a no-op for paths without `'`, which is the overwhelmingly common case).

The `{line}` slot substitutes the decimal integer with no escaping.

### Editor adapter wiring

1. `_build_open_keystrokes(path, line_number)` becomes `_build_open_keystrokes(path, line_number, *, open_keys: str, open_keys_with_line: str)`. It picks the template by `line_number is None`, applies the single-quote-doubling to the path, and formats.
2. `SharedNeovimEditorAdapter.open_target` (`hop/editor.py:286-294`) resolves the editor window spec (the existing `_session_windows_for` factory already returns the editor's `WindowSpec`), reads its `open_keys` / `open_keys_with_line`, and falls back to the module-level defaults when either is `None`. Then it calls `_build_open_keystrokes` with the chosen pair.
3. The defaults live as named module constants in `hop/editor.py` (e.g. `DEFAULT_OPEN_KEYS`, `DEFAULT_OPEN_KEYS_WITH_LINE`) so the test suite and the README example can reference one source of truth.

`WindowSpec` in `hop/layouts.py` grows the same two optional fields and the resolver in `hop/layouts.py:resolve_windows` (around line 28 where the `(EDITOR_ROLE, "nvim", True)` default is declared) propagates them from `WindowConfig` into `WindowSpec`. The nvim launch-command default in `hop/layouts.py:28` stays as-is — it's the editor *command* default, orthogonal to the keystroke template.

### Config parsing

`hop/config.py`'s TOML parser already walks `[windows.<role>]` and `[layouts.<name>.windows.<role>]` and constructs `WindowConfig` instances. Extend it to read `open_keys` and `open_keys_with_line` as optional strings, with the same merge semantics as `command` / `activate` (project config overrides global; per-role overrides layout-scoped). Reject these fields on roles other than `editor` with a clear `ConfigError` — they're meaningless elsewhere and a stray `open_keys` on `[windows.shell]` is almost certainly a typo.

### Documentation

- README **Requirements** section: drop the `[Neovim](https://neovim.io/)` bullet. Add a one-line note under the editor configuration discussion (currently around the per-window fields list, "Built-in roles ...") that nvim is the default and the `open_keys` / `open_keys_with_line` fields let users target any TUI editor that can open files via stdin keystrokes.
- A new short subsection under `## Configuration` walking through the nvim default as a worked example, plus one alternative (helix or kakoune, whichever has the cleaner open-file command) so users see the shape of a custom template.
- `hop_spec.md`: update the editor section to describe the templated keystroke step rather than asserting nvim.

### Caveat to document explicitly

Vim's `:drop` reuses an existing buffer when the file is already open in a window; not every editor has that semantic. Users on editors without an equivalent will get a new buffer per open in some scenarios. Call this out in the README example so it's not a silent surprise.

## Task Type

implement

## Principles

- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)

## Blocked By

(none)

## Definition of Done

- `WindowConfig` and `WindowSpec` carry optional `open_keys` and `open_keys_with_line` fields, parsed from TOML at both `[windows.editor]` and `[layouts.<name>.windows.editor]` scopes.
- Config parsing rejects `open_keys` / `open_keys_with_line` on roles other than `editor` with a clear error message.
- `_build_open_keystrokes` reads the templates from the resolved editor `WindowSpec`, falling back to module-level defaults that reproduce today's nvim sequence byte-for-byte.
- A new test in the editor test suite exercises a custom template (e.g. a helix-shaped `"\x1b:open {path}\r{line}gg\r"`) end-to-end through `SharedNeovimEditorAdapter.open_target`, asserting the bytes sent to `KittyEditorIO.send_text_to_editor`.
- An existing-behavior regression test covers the default templates: `hop edit foo.rb` and `hop edit foo.rb:42` produce the same bytes they do on `main` today.
- README **Requirements** no longer lists Neovim; the editor section documents the two template fields with a worked nvim default and one non-nvim example, and calls out the `:drop`-style "reuse buffer" semantic as editor-specific.
- `hop_spec.md` describes the templated keystroke step instead of asserting nvim.
- `make` is green.
