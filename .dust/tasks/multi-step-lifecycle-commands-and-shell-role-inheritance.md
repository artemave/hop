# Multi-step lifecycle commands and shell-role inheritance

Accept list-form `prepare`/`teardown`/`port_translate`/`host_translate`, and inherit the shell role's command for empty-command non-shell roles.

This unblocks enabling Kitty shell integration end-to-end inside backend role terminals (via the user wrapping the shell role with `kitten run-shell` and installing the kitten binary as one of several sequential `prepare` steps) without leaking kitty into the user's compose file.

## Background

`hop tail` polls Kitty's OSC 133 `at_prompt` flag (`hop/commands/tail.py:51-60`). Inside `dc exec`-backed role shells (e.g., the starfish backend) Kitty's shell integration never runs, so `at_prompt` stays `False`, every `hop tail` times out at 600s, and the headless popup surfaces `TailTimeoutError`. Observed twice on `starfish_chilly:window=3:role=test` within three minutes on 2026-05-19.

Kitty's documented answer for this is `kitten run-shell --shell=<shell>` for container-style backends and `kitten ssh <host>` (as a transport) for SSH backends. Both are user-configurable today via existing knobs (the shell role's `command`, the backend's `interactive_prefix`) — *except* for two infrastructure gaps:

1. **`prepare` is a single string.** Installing the prerequisites in a container (the `hop` shim binary plus the `kitten` standalone binary, plus future additions) is naturally a sequence of independent shell pipelines. Today's config forces `&&`-chained multi-line `"""…"""` strings that are hard to read, hard to fail-fast cleanly, and impossible to extend without rewriting the whole string.
2. **Empty-command non-shell roles bypass the shell role's command.** `_command_for_role` (`hop/kitty.py:596-607`) returns `spec.command` even when it's `""`, and `backend.wrap("", session)` (`hop/backends.py:188-189`) hardcodes `SHELL_FALLBACK = "${SHELL:-sh}"`. So a `test` role with `command = ""` launches a plain `${SHELL:-sh}` regardless of how the user configured the `shell` role. If the user wraps the shell role with `kitten run-shell`, the `test` window doesn't get the wrap.

Today's `_parse_command` (`hop/config.py:551-575`) *actively rejects* list-form values with a comment pointing users at triple-quoted strings. That decision is being reversed for the lifecycle/translate fields (only) — string and command-text fields stay string-only.

## Design

### `string | list[str]` for lifecycle/translate fields

Affected fields: `backend.prepare`, `backend.teardown`, `backend.port_translate`, `backend.host_translate`.

**Not affected:** `backend.interactive_prefix`, `backend.noninteractive_prefix` (prefixes are wraps, not sequences — a list there has no natural meaning); `backend.activate`, `layout.activate`, `window.activate`, `window.command` (single shell commands by construction).

Storage: `BackendConfig.prepare` / `.teardown` / `.port_translate` / `.host_translate` change from `str | None` to `tuple[str, ...]`. Empty tuple = unset (replaces the current `None`). A single-string config produces a 1-tuple; the rest of the code sees one shape.

Semantics:
- **`prepare` / `teardown`:** run each element as its own `flock <lock> sh -c '<element>'` invocation, in order. On the first non-zero exit, stop and surface the failure (the popup's held-open shell still shows the failing step's output as today).
- **`port_translate` / `host_translate`:** run sequentially the same way; the *last* element's stdout is the translation result. Earlier elements' stdout is discarded (the popup-style debug log still captures them per existing `debug.log_command` behavior).

Validation:
- Empty list (`prepare = []`) → `HopConfigError` (`"… field 'prepare' must not be an empty list"`).
- Empty / whitespace-only string inside a list → same `HopConfigError` shape as the existing empty-string check.
- Mixed types in the list (e.g. `["foo", 7]`) → `HopConfigError`.

### Empty-command non-shell roles inherit the shell role

Change `_command_for_role` (`hop/kitty.py:596`): when the role's resolved `spec.command` is empty *and* the role is not the shell role, fall through to the shell role's command instead of returning `""`. Concretely:

```python
def _command_for_role(self, session: ProjectSession, role: str) -> str:
    windows = self._session_windows_for(session)
    spec = find_window(windows, role)
    if spec is not None and spec.command:
        return spec.command
    if role != SHELL_ROLE:
        shell_spec = find_window(windows, SHELL_ROLE)
        if shell_spec is not None and shell_spec.command:
            return shell_spec.command
    return ""
```

Effect: a user who writes

```toml
[layouts.rails.windows.shell]
command = "kitten run-shell --shell=${SHELL:-sh}"

[layouts.rails.windows.test]
command = ""
```

…gets the kitten-wrapped shell in both `shell` *and* `test` windows. Today only `shell` would.

Note: the post-exit-fallback path at `hop/kitty.py:591` already consults the shell role for non-empty-command roles (`log`, `server`, `console`), so it's only the empty-command branch that needs touching. The non-shell empty-command flow then naturally routes back through `backend.wrap(<shell-role-command>, session)`, picking up `interactive_prefix` exactly like any other role.

### What does NOT change

- `hop tail` still polls Kitty's `at_prompt`. With `kitten run-shell` wrapping the in-backend shell, OSC 133 markers fire, and tail works for backend role terminals exactly as it does for host ones.
- `hop_spec.md`'s "requires Kitty shell integration in the role terminal" line stays accurate; it's now achievable for backend sessions without touching compose or dotfiles.
- `*_prefix` semantics, validation, and storage.
- `window.command` validation (still string, still allows empty for "inherit shell role").

### Files changed

- **`hop/config.py`** — `_parse_command` grows an `allow_list: bool = False` parameter and returns `tuple[str, ...] | str | None` accordingly. Call sites for `prepare`/`teardown`/`port_translate`/`host_translate` pass `allow_list=True`. The "list is rejected" error message is updated to apply only when `allow_list=False`. List-form values are validated element-by-element using the existing empty-string check.
- **`hop/state.py`** — `SessionState.prepare` / `.teardown` / translate fields become `tuple[str, ...]`. Persistence: write a TOML/JSON list when non-empty, omit when empty. Reader accepts the legacy string form (one-element tuple) for backward compatibility across in-flight sessions.
- **`hop/backends.py`** — `CommandBackend.prepare_command` / `.teardown_command` become `tuple[str, ...]`. `prepare()` and `teardown()` iterate the tuple; first non-zero return aborts and raises `SessionBackendError` naming the failing step (1-indexed) and command. Translate helpers (`translate_localhost_url`, the `port_translate` path) iterate and use the last step's stdout.
- **`hop/popup.py:_lifecycle_script`** — accept `Sequence[str]` instead of `str`; emit one `flock`-guarded `sh -c` block per element with an early `exit "$status"` between elements. The held-open-on-failure prompt remains; it shows the failing step's command, not the whole sequence.
- **`hop/kitty.py:_command_for_role`** — empty-command non-shell role inherits the shell role's command (the snippet above).
- **`hop_spec.md`** — config grammar section: document that lifecycle and translate fields accept either a string or a list of strings; document the "empty role command inherits shell role" rule.
- **`README.md`** — short note in the config example showing list-form `prepare`.

## Test Plan

No-mocks: parse real TOML, run real shell invocations through real `flock` for the lifecycle pieces.

1. **`_parse_command` accepts a list for `allow_list=True` fields.** TOML `prepare = ["a", "b"]` → tuple `("a", "b")`.
2. **`_parse_command` still rejects lists for `allow_list=False` fields.** TOML `command = ["a"]` → `HopConfigError` with the existing message.
3. **Empty list rejected.** TOML `prepare = []` → `HopConfigError` mentioning the field.
4. **Empty / whitespace element rejected.** TOML `prepare = ["foo", ""]` → `HopConfigError` naming the element index.
5. **Mixed-type element rejected.** TOML `prepare = ["foo", 1]` → `HopConfigError`.
6. **Single-string config produces a 1-tuple.** TOML `prepare = "foo"` round-trips through `BackendConfig` as `("foo",)`.
7. **`CommandBackend.prepare()` runs steps in order via real subprocesses.** Each step writes a unique marker file under `tmp_path`; assert all markers exist in declaration order after `prepare()`.
8. **`prepare()` stops on first failure.** A 3-step prepare where step 2 exits non-zero leaves no step-3 marker, raises `SessionBackendError` naming step 2's command and exit code.
9. **`teardown()` mirrors `prepare()` behavior** — same two cases above.
10. **`port_translate` returns the last step's stdout.** Two-step translate where step 1 echoes `"first"` and step 2 echoes `"second"` returns `"second"`.
11. **`port_translate` failure mid-sequence raises.** A failing intermediate step aborts the sequence.
12. **`_lifecycle_script` emits N flock/exec/exit blocks.** Given `("a", "b")`, render the script; assert it contains two `flock` invocations with `exit "$status"` guards and that the held-open `sh` only triggers when *any* step fails (i.e. the final `exit 0` only fires when all steps succeeded).
13. **Session state round-trips list-form lifecycle.** Persist a `SessionState` with `prepare=("a","b")`; reload; identical tuple.
14. **Session state reads legacy string form.** Persist `{"prepare": "foo"}` directly; loader returns `("foo",)`.
15. **`_command_for_role` returns shell-role command when target role's command is empty.** Layout has `shell.command = "wrap $SHELL"` and `test.command = ""` → `_command_for_role(test)` returns `"wrap $SHELL"`.
16. **`_command_for_role` returns role's own command when non-empty.** `test.command = "pytest"` → returns `"pytest"`.
17. **Shell role itself never falls through.** Shell role with `command = ""` returns `""` (the existing host-default behavior).
18. **`_launch_args` for empty-command non-shell role uses the wrapped shell.** Integration through `_launch_args` with a stubbed backend: `shell.command = "kitten run-shell --shell=${SHELL:-sh}"`, `test.command = ""` → launch argv shows `<prefix> kitten run-shell --shell=…`, not `<prefix> ${SHELL:-sh}`.

## Task Type

implement

## Principles

- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)

## Blocked By

(none)

## Definition of Done

- `prepare`, `teardown`, `port_translate`, `host_translate` accept either a string or a list of strings in TOML; parse to `tuple[str, ...]` internally.
- `*_prefix`, `*.command`, `*.activate` continue to reject lists with the existing error.
- `CommandBackend.prepare()` / `.teardown()` execute steps sequentially, fail fast, surface the failing step's command and exit code.
- Translate helpers run each step in order and return the last step's stdout.
- `_lifecycle_script` emits an N-step script with the existing held-open-on-failure semantics preserved per step.
- Session state persistence and loading handle both shapes (legacy string and new list).
- `_command_for_role` falls through to the shell role's command when a non-shell role's resolved command is empty.
- `hop_spec.md` and `README.md` reflect the new list-form grammar and the empty-command inheritance rule.
- All test cases above pass.
- `make` (default target — test, typecheck, lint, format-check, coverage gate) is green.
