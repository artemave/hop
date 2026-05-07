# Agent Instructions

This project uses [dust](https://github.com/joshski/dust) for planning and documentation.

Always immediately run `bunx dust agent` when you start working in this repository.

## Final check: `make` must pass

Every task ends with `make` (the default target — runs test, typecheck, lint, format-check). Do not declare the task done until it is green. Use `make`, not direct `uv run pytest` / `pyright` / `ruff` invocations — `make` adds flags (e.g. `--cov-fail-under=100`) that direct calls skip.

If `make` is red because of something pre-existing on `main`, that's still part of the current task: fix it, or ask the user before stopping.