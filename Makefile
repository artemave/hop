.PHONY: check

check:
	uv run pytest --cov=hop --cov-branch --cov-fail-under=100 && uv run pyright && uv run ruff check && uv run ruff format --check
