# Inject the open-selection keybinding at bootstrap

Move the open-selection kitty binding from a manual `kitty.conf` line to a `[keys].open_selection` entry hop injects at bootstrap. This matches `[keys].paste`, so hop owns every session-kitty keybinding and the user only names a key.

## Background

hop bootstraps each session's kitty with `kitty --listen-on=… --override allow_remote_control=yes …` (`hop/kitty.py::_bootstrap_session_kitty`), no `--config`, so the user's `kitty.conf` still loads and CLI `--override` stacks on top. The paste-kitten task ([[paste-a-clipboard-image-as-a-file-path]]) uses this to inject `map <key> kitten <path>` for every key in `[keys].paste`, so the paste feature needs zero `kitty.conf` setup.

The open-selection kitten predates that pattern. Today the README ("Open visible-output targets from Kitty") tells the user to add, by hand:

```conf
map ctrl+shift+o kitten hints --customize-processing /printed/by/hop/path
```

where the path comes from `hop path kitten/hints`. This is the last session-kitty keybinding hop does not own. Bringing it onto `[keys]` removes the manual step and the "paste the output of `hop path` into your config" awkwardness.

`[keys]` is introduced by the paste task with just `paste`; this task adds `open_selection`. This task is **Blocked By** that one.

## Design

### Config — `[keys].open_selection`

Add to the `[keys]` table in `hop/config.py`:

- `open_selection` — a kitty key spec or a list of them; default `["ctrl+shift+o"]`. Empty string / empty list ⇒ no binding.

Same handling as `[keys].paste`: accept a bare string as a one-element list, normalize to a list, merge project-over-global wholesale (no element merge), expose via `load_global_config`, no state-file persistence.

```toml
[keys]
open_selection = "ctrl+shift+o"
```

### Bootstrap wiring

In `hop/kitty.py::_bootstrap_session_kitty`, for each key in `[keys].open_selection`, append to `kitty_args`:

- `--override`, `map <key> kitten hints --customize-processing <HINTS_KITTEN_PATH>`

`<HINTS_KITTEN_PATH>` resolves via `resolve_asset_path("kitten/hints")` — the same value `hop path kitten/hints` prints. The binding shape is kitty's built-in `hints` kitten plus the `--customize-processing` hook (unchanged from what the README currently documents); only *who writes the map line* changes.

Do this alongside the `[keys].paste` loop so both sets of keybinding overrides are appended in one place.

### Docs

- `README.md` "Open visible-output targets from Kitty": drop the `map … kitten hints --customize-processing …` instruction and the "run `hop path kitten/hints` and paste it" paragraph. Replace with: the picker is bound automatically in every hop session; `[keys].open_selection` (default `ctrl+shift+o`) changes or disables the key. Keep the rest of the section (what the picker scans, the dispatch behavior, binary-files-open-on-host).
- `README.md` config reference: add `[keys].open_selection` next to `[keys].paste`.
- `README.md` near the kitten setup: the existing/added "no `kitty.conf` changes needed — hop's session kitties get remote control, a listen socket, and the kitten keybindings from bootstrap" note now covers open-selection too.
- `hop_spec.md` §"Selection (hints)": note the binding is injected at bootstrap from `[keys].open_selection`; fix the stale `kittens/open_selection/main.py` path to `hop/kitten/hints/main.py` while here.
- Check `docs/*.md` for any "add this to your kitty.conf" open-selection instruction and update likewise (the ssh / devcontainer guides reference the kitten's behavior but do not appear to instruct the binding — confirm during implementation).

### `hop path kitten/hints`

Keep it. It is a general bundled-asset path tool and other things (docs, debugging) still use it. This task only removes the instruction to wire it into `kitty.conf` by hand.

## Files to change

- `hop/config.py` — add `open_selection` to the `[keys]` table: parse + normalize + merge + expose.
- `hop/kitty.py` — `_bootstrap_session_kitty` appends one `map <key> kitten hints --customize-processing <path>` override per key in `[keys].open_selection`.
- `README.md` — rewrite the "Open visible-output targets from Kitty" setup paragraph; add `[keys].open_selection` to the config reference.
- `hop_spec.md` — §"Selection (hints)": bootstrap-injection note; fix the stale kitten path.

## Tests

No mocks of hop's own code; use `StubLauncher` (`tests/test_kitty.py`) and the config fixtures (`tests/test_config.py`).

- **`tests/test_kitty.py`** — default `[keys].open_selection` → `StubLauncher` records `--override map ctrl+shift+o kitten hints --customize-processing <…/hop/kitten/hints/main.py>`; a custom list is honored key-for-key; an empty list → no such override. `[keys].paste` overrides still present in the same bootstrap (the two are independent).
- **`tests/test_config.py`** — `[keys].open_selection` round-trips as a list; a bare string normalizes; defaults to `["ctrl+shift+o"]` when absent; a project value replaces the global list wholesale; non-string / non-list rejected. `[keys].paste` behavior unchanged.

## Manual verification

In a fresh hop session with a stock `kitty.conf` (no `map … kitten hints` line), press <kbd>Ctrl-Shift-O</kbd> and confirm the picker appears and dispatches a file path to the editor, exactly as before the change. Set `[keys].open_selection = "ctrl+shift+p"`, re-enter the session, confirm the new key works and the old one does not.

## Out of scope

- Changing the open-selection kitten's behavior, matching, or dispatch — only how its keybinding is established.
- Removing `hop path` or the `kitten/hints` asset.
- Any change to `[keys].paste` (owned by [[paste-a-clipboard-image-as-a-file-path]]).
- A general `[keys].<name>` mechanism for arbitrary user-defined kitten bindings. `[keys]` holds hop's own bindings (`paste`, `open_selection`); user-defined kitten maps stay in the user's `kitty.conf`.

## Task Type

implement

## Principles

- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

- [Paste a clipboard image as a file path](paste-a-clipboard-image-as-a-file-path.md)

## Definition of Done

- `[keys].open_selection` (default `["ctrl+shift+o"]`) is parsed from global and project config, accepts a string or list, normalizes to a list, is replaced wholesale by a project value, and disables the binding when empty.
- `_bootstrap_session_kitty` injects one `--override map <key> kitten hints --customize-processing <resolve_asset_path("kitten/hints")>` per key in `[keys].open_selection`, alongside the `[keys].paste` overrides.
- A fresh hop session with no `map … kitten hints` line in `kitty.conf` has a working open-selection picker on the configured key.
- `README.md` no longer instructs adding the `hints` map to `kitty.conf`; it documents `[keys].open_selection` and the "no `kitty.conf` changes needed" note covers open-selection.
- `hop_spec.md` §"Selection (hints)" states the binding is bootstrap-injected from `[keys].open_selection` and no longer cites the stale `kittens/open_selection/main.py` path.
- New and updated tests follow the repo's no-mock conventions and pass under `make`.
- `bunx dust lint` passes for this task file.
