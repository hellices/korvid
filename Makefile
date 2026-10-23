.PHONY: source-size lint format typecheck test check docs-build docs-serve

source-size:
	uv run --frozen python scripts/check_source_size.py

lint:
	uv run --frozen ruff check src/ tests/

format:
	uv run --frozen ruff check --fix src/ tests/ && uv run --frozen ruff format src/ tests/

typecheck:
	uv run --frozen mypy

test:
	uv run --frozen pytest -x -q

check: source-size lint typecheck test
	uv run --frozen tach check

docs-build:
	uv run --frozen --group docs mkdocs build --strict
	uv run --frozen python scripts/check_docs_site.py

docs-serve:
	uv run --frozen --group docs mkdocs serve
