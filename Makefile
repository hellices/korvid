.PHONY: source-size lint format typecheck test check docs-build docs-serve

source-size:
	uv run --frozen python scripts/check_source_size.py

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/

typecheck:
	uv run mypy

test:
	uv run pytest -x -q

check: source-size lint typecheck test
	uv run tach check

docs-build:
	uv run --frozen --group docs mkdocs build --strict

docs-serve:
	uv run --frozen --group docs mkdocs serve
