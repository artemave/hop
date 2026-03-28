# 100% Test Coverage

Enforce 100% test coverage across the `hop` Python package to ensure every code path is exercised by tests. This acts as a safety net for agents and contributors making changes, consistent with the `comprehensive-test-coverage` and `unit-test-coverage` principles.

## Context

The project currently has 106 passing tests across 18 test files covering `hop`'s core modules (`app`, `browser`, `cli`, `editor`, `kitty`, `session`, `sway`, `targets`, `commands/*`). Tests use a custom minimal pytest runner (`pytest/__main__.py`) that supports parametrize but no plugins such as `pytest-cov`.

The `Makefile` `check` target runs: `uv run pytest && uv run pyright && uv run ruff check && uv run ruff format --check`. Coverage is not currently measured or enforced.

Source code is ~1,900 lines across 19 files in `hop/` and `hop/commands/`. Much of it wraps external processes (Sway IPC, Kitty, Neovim), which tests stub out via dependency injection.

## What's Needed

1. **Coverage measurement**: Integrate `coverage.py` to produce a coverage report.
2. **Gap analysis**: Identify uncovered lines and branches, then write tests or mark explicit exclusions.
3. **Enforcement**: Add a coverage threshold to the `check` target so CI fails on regressions.

## Open Questions

### How should coverage be measured given the custom pytest runner?

#### Replace the custom runner with real pytest

The project uses `pytest/__main__.py` rather than standard pytest, so `pytest-cov` is incompatible with the current setup. Switching to the installed `pytest` (already a dev dependency) gives access to `pytest-cov` directly with no extra wiring.

#### Wrap the custom runner with `coverage run`

Change the `check` target to `uv run coverage run -m pytest && uv run coverage report --fail-under=100`. Keeps the custom runner intact; adds `coverage` to dev dependencies. Avoids touching `pytest/__main__.py`.

#### Extend the custom runner to emit coverage data

Instrument `pytest/__main__.py` to start/stop `coverage.Coverage()` itself. Keeps the runner self-contained but adds internal complexity to an already custom piece of infrastructure.

### What coverage metric should be enforced?

#### Line coverage at 100%

Simplest to achieve and report. Misses some branch paths (e.g., an `if` that's always `True` in tests).

#### Branch coverage at 100%

Catches missed conditional paths. Harder to achieve — some branches may be unreachable in practice (defensive `else` clauses, platform guards).

#### Line coverage at 100% with branch coverage as a non-blocking report

Enforce lines, surface branches as informational. Pragmatic middle ground.

### How should legitimately untestable code be handled?

#### Mark with `# pragma: no cover` and document why

Some paths require live Sway/Kitty processes and cannot be unit-tested without significant refactoring.

Standard `coverage.py` exclusion. Makes exclusions explicit and reviewable in code.

#### Refactor to push untestable paths into a thin imperative shell

Consistent with the `functional-core-imperative-shell` principle. Moves platform-specific I/O to a boundary layer that's excluded by design, reducing the need for ad-hoc exclusions.

#### Set coverage threshold below 100%

E.g., 95%. Pragmatic but may silently accumulate uncovered code over time and undermines the stated goal.
