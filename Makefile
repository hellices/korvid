.PHONY: lint format typecheck test check docs-build docs-serve

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/

typecheck:
	uv run mypy

test:
	uv run pytest -x -q

check: lint typecheck test
	uv run tach check

docs-build:
	uv run --frozen --group docs mkdocs build --strict

docs-serve:
	uv run --frozen --group docs mkdocs serve
