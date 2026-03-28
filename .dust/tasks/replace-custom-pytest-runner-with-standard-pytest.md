# Replace Custom Pytest Runner with Standard Pytest

Replace `pytest/__main__.py` with standard pytest invocation so that `pytest-cov` and other pytest plugins become available.

## Context

The project uses a custom minimal pytest runner at `pytest/__main__.py` that supports parametrize but no plugins. The standard `pytest` is already a dev dependency. Switching to it unlocks `pytest-cov` for coverage enforcement.

## What To Do

1. Remove `pytest/__main__.py` (and `pytest/__init__.py` if it only exists for the runner)
2. Update `pyproject.toml` (or `uv.lock`) so `uv run pytest` resolves to the installed pytest binary rather than the custom module
3. Verify `uv run pytest` runs all tests and passes
4. Update the `Makefile` `check` target if needed

## Definition of Done

- `uv run pytest` runs all existing tests and they pass
- The custom `pytest/__main__.py` runner is deleted
- `bunx dust check` passes

## Principles

- make-the-change-easy — prerequisite change to unlock coverage tooling
- comprehensive-test-coverage — a project's test suite is its primary safety net
- fast-feedback-loops — the check loop should be fast

## Task Type

implement

## Blocked By

(none)
