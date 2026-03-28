# Enforce 100% Line and Branch Coverage

Integrate `pytest-cov` and enforce 100% line and branch coverage in the `check` target. Refactor any untestable code paths into a thin imperative shell so that exclusions are structural rather than ad-hoc.

## Context

After replacing the custom pytest runner with standard pytest, `pytest-cov` becomes available. The source is ~1,900 lines across 19 files in `hop/` and `hop/commands/`. Much of it wraps external processes (Sway IPC, Kitty, Neovim) which tests already stub via dependency injection.

Coverage decisions:
- Enforce **line coverage 100% + branch coverage 100%**
- Push legitimately untestable paths (thin I/O boundary code) into an imperative shell rather than sprinkling `# pragma: no cover`

## What To Do

1. Add `pytest-cov` to dev dependencies
2. Run `uv run pytest --cov=hop --cov-branch --cov-report=term-missing` and identify all uncovered lines/branches
3. For each gap: either write tests or refactor the code to push the untestable path into a thin imperative shell entry point
4. Update `Makefile` `check` target to include `--cov=hop --cov-branch --cov-fail-under=100`

## Definition of Done

- `uv run pytest --cov=hop --cov-branch --cov-fail-under=100` passes with 100% line and branch coverage
- The `check` target in `Makefile` enforces coverage so CI fails on regressions
- No `# pragma: no cover` annotations (untestable paths are structural, not inline exclusions)
- `bunx dust check` passes

## Principles

- comprehensive-test-coverage — a project's test suite is its primary safety net
- unit-test-coverage — complete unit test coverage ensures direct feedback as code changes
- functional-core-imperative-shell — separate code into a pure functional core and thin imperative shell
- design-for-testability — design code to be testable first
- make-changes-with-confidence — developers should be able to modify code without fear
- dependency-injection — avoid global mocks

## Task Type

implement

## Blocked By

(none)
