# Implement directory-rooted session resolution

Make `hop` treat the caller's current working directory as the session root for all session-scoped commands.

Update `hop/session.py` so session resolution uses the provided directory directly instead of searching for `.git`, `.dust`, or `pyproject.toml`. Adjust the command flows in `hop/commands/session.py`, `hop/commands/edit.py`, `hop/commands/term.py`, `hop/commands/run.py`, and `hop/app.py` as needed so `hop`, `hop edit`, `hop term`, `hop run`, and `hop browser` consistently treat the invocation directory as the session root. Refresh the affected tests in `tests/test_session.py`, `tests/test_session_commands.py`, `tests/test_edit_commands.py`, `tests/test_term_commands.py`, `tests/test_run_commands.py`, and `tests/test_app.py`, and align the user-facing docs in `README.md` plus the derived contract fact in [hop session model and command contract](../facts/hop-session-model-and-command-contract.md).

This task implements the updated session model in [hop_spec.md](../../hop_spec.md).

## Principles

- [Keep the spec aligned](../principles/keep-the-spec-aligned.md)
- [Session-oriented workspaces](../principles/session-oriented-workspaces.md)

## Task Type

implement

## Blocked By

(none)

## Definition of Done

- `hop` and every session-scoped command use the invocation directory as the session root
- Session resolution no longer depends on ancestor markers such as `.git`, `.dust`, or `pyproject.toml`
- Tests cover repeated invocation from the same directory and distinct invocation from nested directories
- `README.md`, [hop_spec.md](../../hop_spec.md), and the derived dust facts describe the same session-root behavior
