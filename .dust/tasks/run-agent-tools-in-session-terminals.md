# Run agent tools in session terminals

Add `hop run --wait` and a bundled agent skill that routes Claude Code and Codex one-shot commands into a hop session's role terminals. The commands then run where the user can watch them and inside the session's real backend, not in a private subshell.

## Background

When Claude Code or Codex runs inside a hop session, every command it executes goes through its own subshell: invisible to the user, and — for container and remote sessions — outside the backend the session actually lives in, so the venv, `node_modules`, DB connections, and `dc exec` context are all wrong or cold. hop already has the pieces to route those commands into the session's role terminals:

- `hop run --role <role> "<cmd>"` (`hop/commands/run.py`) types the exact command into the session's role terminal (creating it if missing), persists a run-state JSON (`window_id`, `session`, `role`, `dispatched_at`) under `default_runs_dir()`, prints a run id, and returns without waiting.
- `hop tail <run-id>` (`hop/commands/tail.py`) looks up that state, polls `KittyWindowState.at_prompt` (OSC 133 prompt boundaries via Kitty shell integration), and once the command is back at the prompt writes its combined output to stdout. It exits 0 and deliberately does not propagate the inner command's exit status — the contract `vigun` depends on.
- `KittyWindowState` (`hop/kitty.py:131`) already carries `last_cmd_exit_status: int`, read from Kitty's `ls` output (OSC 133 `D`). `tail_command` fetches it today and discards it.

An agent needs a single blocking call that returns both the output and a real pass/fail signal. That is `hop run --wait`. The skill then only has to tell the agent which role to use.

Scope is a deliberately small trial: one-shot commands only, tests to the `test` window and everything else to the `shell` window, sharing `shell` with the user, advisory skill (no hook / MCP enforcement).

## Approach

1. **`hop run --wait`** — a blocking mode that dispatches the command, waits for the role terminal to return to its prompt, writes the command's combined output, and exits with the command's own exit status. It is `run_command` + `tail_command` composed, plus exit-status propagation. `hop run` without `--wait` and `hop tail` are unchanged.
2. **Exit status out of `tail_command`** — `tail_command` returns a `TailResult(output, exit_status)` instead of a bare string. Standalone `hop tail` still writes only `output` and exits 0; `hop run --wait` uses `exit_status`.
3. **Bigger capture buffer** — `_bootstrap_session_kitty` injects `--override scrollback_lines=100000` so a large test run does not scroll its head out of the buffer that `get-text --extent last_cmd_output` reads.
4. **Bundled skill** — a `SKILL.md` shipped in the `hop` package, in the format both Claude Code and Codex read (`name` / `description` frontmatter, plain-markdown body).
5. **`hop install --agents`** — copies the packaged skill directory into `~/.claude/skills/hop-run/` and `~/.agents/skills/hop-run/` under the home of wherever it runs. Two plain copies; re-run after a hop upgrade. `hop install` is a new umbrella command for local setup that hop can grow more targets on later (`--kitten`, `--kitty-config`, …).

Because the copy resolves `Path.home()` of the process, one command covers every backend: run `hop install --agents` once on the host, and add `<noninteractive_prefix> hop install --agents` as a `prepare` step (after the step that puts `hop` in the backend) for container / remote sessions so the skill lands in that backend's `$HOME`. No hop-owned bootstrap phase; delivery stays an explicit, opt-in step for the trial.

## Design

### `hop run --wait`

CLI (`hop/cli.py::build_parser`): `run_parser.add_argument("--wait", action="store_true", dest="wait")`. Composes with `--role` and the existing `--focus`; ordering irrelevant. `parse_command`'s `"run"` case sets `RunCommand.wait` from `namespace.wait`.

`hop/commands/__init__.py`: `RunCommand` grows `wait: bool = False`.

`hop/app.py::execute_command`, the `RunCommand` arm (currently `case RunCommand(role=role, command_text=command_text, focus=focus):`) also binds `wait`:

- Dispatch exactly as today: `run_command(...)`, then the existing `--focus` workspace-switch block.
- `wait is False` (unchanged): `print(dispatch.run_id)`.
- `wait is True`: do **not** print the run id. Call `tail_command(dispatch.run_id, kitty=services.kitty)`, `sys.stdout.write(result.output)`, and `return result.exit_status`.
- `wait is True` and `tail_command` raises `TailTimeoutError`: write whatever `services.kitty.get_last_cmd_output(...)` returns for the window, write a one-line notice to stderr (`hop run --wait: timed out after <n>s; command still running in the <role> window`), and `return 124` (the `timeout(1)` convention). `hop tail`'s own timeout behavior is untouched.

`execute_command` already returns an `int` that `hop/cli.py::main` propagates as the process exit code; the `RunCommand` arm just needs an explicit `return` on the `--wait` paths (the non-wait path keeps falling through to the shared trailing `return 0`).

### `TailResult`

`hop/commands/tail.py`: introduce

```python
@dataclass(frozen=True, slots=True)
class TailResult:
    output: str
    exit_status: int
```

`tail_command(...) -> TailResult`. At the point it currently does `return kitty.get_last_cmd_output(...)`, it instead returns `TailResult(output=kitty.get_last_cmd_output(session_name, window_id), exit_status=ws.last_cmd_exit_status)` — `ws` is the `KittyWindowState` from the same poll iteration that saw the prompt return. `UnknownRunError` and `TailTimeoutError` are unchanged.

`hop/app.py`, the `TailCommand` arm: `sys.stdout.write(tail_command(run_id, kitty=services.kitty).output)` — still exits 0, `vigun`'s contract intact.

### `scrollback_lines` override

`hop/kitty.py::_bootstrap_session_kitty`, alongside the other bootstrap `--override` values (`allow_remote_control`, the paste/hints maps, `clipboard_control`): append `--override`, `scrollback_lines=100000`. Process-global, so every later `kitty @ launch` window in the session inherits it. Add it to `session_kitty_overrides` if that is where the sibling overrides now live, otherwise inline in the bootstrap arg list next to them.

### The skill — `hop/agents/hop-run/SKILL.md`

Shipped verbatim as package data:

```markdown
---
name: hop-run
description: >-
  Run one-shot shell commands (tests, linters, type checks, builds, git,
  package managers, one-off scripts) inside the current hop session's terminal
  windows instead of a private subshell, so the user sees them run live. Use for
  any command that exits on its own. Do NOT use for servers, watch modes, REPLs,
  or other long-running or interactive processes.
---

# Running commands in the hop session

When `hop` is on PATH, route one-shot commands through it instead of your own
shell tool:

- Test commands (pytest, jest, vitest, go test, rspec, `make test`, ...):
  `hop run --wait --role test "<command>"`
- Everything else that exits on its own (linters, type checkers, builds, `git`,
  `npm` / `bun` / `uv`, one-off scripts):
  `hop run --wait --role shell "<command>"`

`hop run --wait` types the command into that session window, blocks until it
returns to the prompt, prints the command's combined output, and exits with the
command's own exit status. Use the exit status for pass/fail and the output for
detail.

Rules:

- One `hop run --wait` at a time. Wait for it to return before dispatching the
  next; the window runs one command at a time.
- Never start servers, `--watch` modes, REPLs, or anything that does not exit on
  its own this way. Run those as you normally would, or ask the user.
- If `hop` is not on PATH, or `hop run` reports it is not inside a session, use
  your normal shell tool.
- The `shell` window may also be in use by the user. Keep commands there short
  and non-destructive.
```

The body opens on the `hop`-on-PATH / "reports it is not inside a session" check rather than a `HOP_SESSION` marker — see Out of scope.

### `hop install --agents`

CLI (`hop/cli.py::build_parser`):

```python
install_parser = subparsers.add_parser("install")
install_parser.add_argument("--agents", action="store_true", dest="install_agents")
```

`parse_command`'s `"install"` case returns `InstallCommand(agents=namespace.install_agents)`. With no target flag set it raises `ValueError("hop install: choose a target (e.g. --agents)")` — bare `hop install` is not a silent no-op and not "install everything". Future targets are added as sibling `--kitten` / `--kitty-config` flags and matching `InstallCommand` fields.

`hop/commands/__init__.py`: `InstallCommand` dataclass with `agents: bool = False` (grows more bool fields later).

`hop/commands/install.py` (new): `install_agent_skill(*, home: Path | None = None) -> list[Path]`.

- Source: `resolve_asset_path("agents/hop-run")` — resolves to `hop/agents/hop-run/` (the dir holds `SKILL.md`; `resolve_asset_path` returns the dir when there is no `main.py` — confirm and adjust `resolve_asset_path` if it only returns files, or read `resolve_asset_path("agents/hop-run/SKILL.md")` directly).
- Targets: `<home>/.claude/skills/hop-run/` and `<home>/.agents/skills/hop-run/`, `home` defaulting to `Path.home()`.
- For each target: `mkdir -p` the parent, copy every file from the source dir into it (overwriting), and return the two target `SKILL.md` paths.
- `hop/app.py`: `case InstallCommand(agents=agents):` — when `agents`, call `install_agent_skill()` and print one line per written path. (No target selected can't reach here; `parse_command` already rejected it.)

### Packaging

`hop/agents/hop-run/SKILL.md` must ship in the wheel. Hatchling includes non-`.py` files under the package by default, but confirm with `uv build` + `unzip -l dist/*.whl | grep agents`; if absent, add

```toml
[tool.hatch.build.targets.wheel.force-include]
"hop/agents" = "hop/agents"
```

or an `artifacts` entry. `hop/agents/__init__.py` and `hop/agents/hop-run/__init__.py` are not needed (it is data, not an import package) but add empty ones if that is the pattern the kitten dirs follow.

### Spec

`hop_spec.md`:

- "Send command to terminal": add `--wait` to the synopsis and a Behavior bullet — "`--wait` blocks until the dispatched command returns to its prompt, writes its combined output to stdout, and exits with the command's own status (124 on timeout); it does not print a run id." Note `--wait` composes with `--focus`.
- "Tail command output": note that `tail_command` now returns output plus the captured `last_cmd_exit_status`, that standalone `hop tail` still writes only the output and exits 0, and that `hop run --wait` is the consumer of the exit status.
- New "Agent skill" subsection near "Kitty integration": `hop install --agents` writes the bundled `hop-run` skill into `~/.claude/skills/` and `~/.agents/skills/` of the home it runs under; the skill routes agents' one-shot commands through `hop run --wait --role {test,shell}`; it is advisory and self-gates on `hop` being on PATH inside a session. Document the host-once + `prepare`-step-per-backend delivery.
- Note the bootstrap `scrollback_lines=100000` override with the other injected Kitty settings.

### README

- `hop run` section: a `hop run --wait --role test "pytest -q"` example showing it blocks and returns the exit status.
- New short section "Run agent tools in the session": what `hop install --agents` does and where it writes; that the skill is shared by Claude Code and Codex; the host-once + `prepare` step (`<noninteractive_prefix> hop install --agents`) for container / remote backends; and that it is a trial-scope convention (tests -> `test`, everything else -> `shell`, one-shot only).

## Files to change

- `hop/cli.py` — `--wait` on the `run` subparser; new `install` subparser with `--agents`; `parse_command` cases for both (`install` with no target flag → `ValueError`).
- `hop/commands/__init__.py` — `RunCommand.wait: bool = False`; new `InstallCommand(agents: bool = False)`.
- `hop/commands/tail.py` — `TailResult`; `tail_command` returns it.
- `hop/commands/install.py` — new; `install_agent_skill(...)`.
- `hop/commands/path.py` — only if `resolve_asset_path` needs to return a directory for `agents/hop-run`.
- `hop/app.py` — `RunCommand` arm handles `wait` (output + exit status + 124-on-timeout); `TailCommand` arm uses `.output`; new `InstallCommand` arm.
- `hop/kitty.py` — inject `--override scrollback_lines=100000` at session-kitty bootstrap.
- `hop/agents/hop-run/SKILL.md` — new packaged skill asset.
- `pyproject.toml` — ensure `hop/agents/**` ships in the wheel.
- `hop_spec.md` — `--wait`, `TailResult`, "Agent skill" subsection, `scrollback_lines` note.
- `README.md` — `hop run --wait` example; "Run agent tools in the session" section.

## Tests

Real behavior, no mocks (project convention); reuse the suite's existing doubles (`StubLauncher` in `tests/test_kitty.py`, the fake Kitty adapter in `tests/test_run_commands.py` / the tail tests, `tmp_path` for filesystem).

- `tests/test_cli.py` — `parse_command(["run", "x"])` -> `RunCommand(command_text="x", role="shell", focus=False, wait=False)`; `["run", "--wait", "x"]` -> `wait=True`; `["run", "--wait", "--role", "test", "x"]` -> `role="test", wait=True`; `["install", "--agents"]` -> `InstallCommand(agents=True)`; `["install"]` -> `ValueError`.
- `tests/test_run_commands.py` (or the tail test module) — a fake Kitty adapter whose `get_window_state` returns `at_prompt=False` for the first N polls then `at_prompt=True` with `last_cmd_exit_status=3`, and `get_last_cmd_output` returns `"boom\n"`: `tail_command` returns `TailResult(output="boom\n", exit_status=3)`. Timeout case still raises `TailTimeoutError`.
- `tests/test_app.py` — `execute_command(RunCommand(role="test", command_text="pytest", focus=False, wait=True), ...)` with that fake adapter writes `"boom\n"` to stdout and returns `3`; the run id is not printed. Timeout: `execute_command` returns `124`, writes the partial `get_last_cmd_output`, and writes a notice to stderr. `wait=False` still prints the run id and returns `0`.
- `tests/test_kitty.py` — default bootstrap: `StubLauncher` records a `--override scrollback_lines=100000` in the session kitty's args.
- `tests/test_install.py` (new) — `install_agent_skill(home=tmp_path)` creates `tmp_path/.claude/skills/hop-run/SKILL.md` and `tmp_path/.agents/skills/hop-run/SKILL.md`, both byte-equal to `hop/agents/hop-run/SKILL.md`; returns the two paths; a second call overwrites without error. `SKILL.md` frontmatter parses and has non-empty `name` and `description` (`claude plugin validate` shape).

## Manual verification

Per `feedback_verify_external_cli_flags`, run the real CLI once:

- In a hop session: `hop run --wait --role test "pytest -q"` prints the output and blocks; `echo $?` afterwards matches pytest's own exit code.
- `hop run --wait --role shell "false"` exits 1; `hop run --wait --role shell "exit 7"` exits 7; a successful command exits 0.
- A command that does not return (e.g. `sleep 999`) — after the timeout, `hop run --wait` exits 124, prints a stderr notice, and the command is still visible running in the `shell` window.
- Output larger than 2000 lines is captured in full (confirms the `scrollback_lines` bump).
- `hop install --agents` writes both `SKILL.md` files; `hop install` with no flag errors; open Claude Code in a session and confirm `/hop-run` is listed; open Codex and confirm `$hop-run` is available. Run it as a container `prepare` step and confirm the skill lands in the container's `$HOME`.
- Verify across session shapes that `hop run --wait` targets the in-session (container / remote) shell, not a host subshell.

## Out of scope

- A `HOP_SESSION` (or any dedicated "inside a hop session") environment marker. It was considered; for this trial the skill self-detects via `hop` being on PATH and `hop run` resolving a session from the cwd (host) or `HOP_REMOTE_*` (remote). Revisit if that detection proves unreliable.
- Streaming `hop run --wait` output incrementally, and source-side capture via `script` / `tee`. This task only raises `scrollback_lines`; revisit only if buffer truncation actually bites.
- A dedicated per-agent terminal. The agent shares the `shell` window with the user for the trial; input collisions are accepted.
- Routing servers, watch modes, REPLs, or any long-running / interactive process. `hop run --wait` is for commands that exit on their own.
- Enforcement via a `PreToolUse` hook or an MCP server. The skill is advisory; promote later if the trial sticks.
- Per-project (repo-tree) skill installation. `hop install --agents` writes the home-relative skill dirs (`~/.claude/skills/`, `~/.agents/skills/`) — nothing lands in the checked-out working tree.
- A canonical-copy-plus-symlink install scheme. Two plain file copies, re-run after upgrade.
- A hop-owned backend-bootstrap phase that auto-installs the skill into every backend. For the trial, delivery to container / remote backends is a `prepare` step the user adds by hand (`<noninteractive_prefix> hop install --agents`).
- Other `hop install` targets (`--kitten`, `--kitty-config`, …). Only `--agents` lands here; the umbrella command is shaped to take them later.
- Changing `hop run` (no `--wait`) or `hop tail` output/exit semantics.

## Task Type

implement

## Principles

- [Role-based terminals are routing primitives](../principles/role-based-terminals-are-routing-primitives.md)
- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Prefer native integrations](../principles/prefer-native-integrations.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)

## Blocked By

(none)

## Definition of Done

- `hop run --wait [--role <role>] "<cmd>"` dispatches the command into the session's role terminal, blocks until that terminal returns to its prompt, writes the command's combined output to stdout, and exits with the command's own exit status. It does not print a run id. It composes with `--focus`.
- `hop run --wait` on a command that never returns exits `124` after the tail timeout, writes the partial captured output, and writes a one-line notice to stderr; the command keeps running in the role window.
- `hop run` without `--wait` is byte-for-byte unchanged: prints the run id, returns 0.
- `tail_command` returns `TailResult(output, exit_status)` with `exit_status` taken from the same `KittyWindowState` poll that observed the prompt return. Standalone `hop tail <id>` writes only `output` and exits 0.
- `_bootstrap_session_kitty` injects `--override scrollback_lines=100000`, and a captured command emitting more than 2000 lines is returned in full by `hop run --wait`.
- `hop/agents/hop-run/SKILL.md` ships in the built wheel, has valid `name` / `description` frontmatter, and instructs the agent to use `hop run --wait --role test` for test commands, `hop run --wait --role shell` otherwise, one at a time, never for long-running processes, with a fallback to the normal shell tool when `hop` is unavailable or not in a session.
- `hop install --agents` copies the packaged skill into `~/.claude/skills/hop-run/` and `~/.agents/skills/hop-run/` under the running process's `$HOME`, creating parent directories, overwriting on re-run, and printing the written paths; `hop install` with no target flag exits non-zero with a message. Running it inside a backend (host, container `exec`, remote `ssh`) installs into that backend's home.
- `hop_spec.md` and `README.md` document `--wait`, the `TailResult` exit-status surface, `hop install --agents` and the `hop-run` skill (including the host-once + per-backend `prepare`-step delivery), and the `scrollback_lines` bootstrap override.
- New and updated tests follow the repo's no-mock conventions and pass under `make`.
- `bunx dust lint` passes for this task file.
