# Add Typechecking and Lint for Python Code

The `hop` codebase has thorough type annotations but no static analysis enforcement. Add a type checker and linter to catch type errors and style issues before runtime.

## Description

The `hop` codebase is written in Python with type annotations throughout (Protocol classes, dataclasses with typed fields, annotated function signatures, `Sequence`, `Path`, etc.). However, there is currently no static analysis enforcement — no type checker and no linter configured. This means type errors and style issues can silently exist in the codebase without being caught.

Adding typechecking and linting would:
- Catch type errors before runtime
- Enforce consistent code style
- Surface bugs that tests may not cover (e.g. wrong argument types passed to Protocol implementations)
- Align with the `lint-everything` principle: "Prefer static analysis over runtime checks"

The project uses `uv` and `pyproject.toml` for dependency management, and `pytest` for tests. Any tooling added should integrate naturally into this setup.

## Relevant Codebase Context

- `pyproject.toml` uses `hatchling` for build and `uv` for dev dependencies; currently only `pytest>=8.4.0` is in `dev` group
- Python 3.12+ is required
- Source files use `from __future__ import annotations`, typed dataclasses, Protocol classes, and `match` statements
- Tests are in `tests/` and co-located with source under `hop/`
- The project has no `.flake8`, `.pylintrc`, `mypy.ini`, `ruff.toml`, or similar config files yet
- Type stubs would be needed for any untyped third-party dependencies (currently none — `dependencies = []`)

## Open Questions

### Which type checker to use?

#### Option: mypy
The de facto standard Python type checker. Mature, widely supported, integrates well with editors. Can be configured in `pyproject.toml` under `[tool.mypy]`.

#### Option: pyright / pylance
Faster, stricter, developed by Microsoft. Used by Pylance in VS Code. Can be run via `pyright` CLI. Supports `pyrightconfig.json` or `pyproject.toml` configuration.

### Which linter/formatter to use?

#### Option: ruff
Modern, extremely fast linter and formatter written in Rust. Replaces flake8, isort, and can also replace black. Configurable via `pyproject.toml` under `[tool.ruff]`. Growing adoption, minimal overhead.

#### Option: flake8 + isort + black
Traditional combination: `flake8` for linting, `isort` for import ordering, `black` for formatting. Well-established but slower and requires more config.

### How to integrate into the check workflow?

#### Option: Add as `uv run` commands in a Makefile or shell script
Simple, explicit. Each tool is run manually or in CI.

#### Option: Configure as `bunx dust check` steps
If dust supports check hooks or custom check scripts, type checking and linting could be surfaced as part of the standard `dust check` workflow, keeping them visible to agents running checks.

#### Option: Use pre-commit hooks
Enforce checks before every commit. Adds friction but catches issues early. Requires installing `pre-commit`.

### Strictness level for the type checker?

#### Option: Start strict (e.g. `mypy --strict` or `pyright` in strict mode)
Since the codebase already has thorough type annotations, strict mode may be achievable now and prevents annotation debt from accumulating.

#### Option: Start permissive and tighten incrementally
Add `# type: ignore` suppressions only where needed, tighten over time. Lower initial friction but risks deferring real issues.
