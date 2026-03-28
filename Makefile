.PHONY: check

check:
	uv run pytest && uv run pyright && uv run ruff check && uv run ruff format --check
