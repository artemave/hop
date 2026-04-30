.PHONY: check test typecheck lint format-check

check: test typecheck lint format-check

test:
	uv run pytest --cov=hop --cov-branch --cov-fail-under=100

typecheck:
	uv run pyright

lint:
	uv run ruff check

format-check:
	uv run ruff format --check
