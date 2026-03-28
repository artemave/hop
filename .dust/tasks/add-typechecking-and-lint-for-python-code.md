# Add Typechecking and Lint for Python Code

Add `pyright` (strict mode) and `ruff` to the `hop` project and wire them up as `uv run` commands so they can be run locally and in CI.

## Description

The `hop` codebase has thorough type annotations but no static analysis enforcement. This task adds:

- **pyright** in strict mode as the type checker
- **ruff** as the linter and formatter

Both tools are added as dev dependencies in `pyproject.toml` and invoked via `uv run`. A `Makefile` (or shell script) exposes a single `check` target that runs tests, type checking, and linting together.

Any type errors or lint violations surfaced by the tools should be fixed as part of this task so `bunx dust check` passes cleanly after the change.

## Steps

1. Add `pyright` and `ruff` to `[dependency-groups] dev` in `pyproject.toml` and run `uv sync`
2. Configure `pyright` in `pyproject.toml` under `[tool.pyright]` with `strict = true` and `pythonVersion = "3.12"`
3. Configure `ruff` in `pyproject.toml` under `[tool.ruff]` (select at minimum `E`, `F`, `I` rules; enable format)
4. Add a `Makefile` with a `check` target: `uv run pytest && uv run pyright && uv run ruff check && uv run ruff format --check`
5. Fix any type errors and lint violations reported by the tools
6. Verify `make check` passes end-to-end

## Principles

- lint-everything — prefer static analysis over runtime checks
- fast-feedback-loops — the check loop should be fast
- reproducible-checks — checks must produce the same result on every machine

## Definition of Done

- `pyright` and `ruff` are listed as dev dependencies in `pyproject.toml`
- `pyright` runs in strict mode with no errors
- `ruff check` and `ruff format --check` pass with no violations
- A `Makefile` (or equivalent) exposes a single `check` target that runs all checks
- `make check` passes end-to-end from a clean checkout

## Task Type

implement

## Blocked By

(none)
