"""Docs build configuration invariants.

Ensures that `make docs-build` and `make docs-serve` use `--frozen` so that
running them behind a corporate mirror cannot silently rewrite uv.lock to
private-index URLs.

Also ensures /site/ is listed in .gitignore so generated MkDocs output cannot
be accidentally committed.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_makefile_docs_build_uses_frozen() -> None:
    """docs-build target must pass --frozen to prevent lock rewrites."""
    makefile = (ROOT / "Makefile").read_text()
    # Find the docs-build recipe line
    assert "uv run --frozen --group docs mkdocs build" in makefile, (
        "docs-build must use 'uv run --frozen --group docs mkdocs build --strict'"
    )


def test_makefile_docs_serve_uses_frozen() -> None:
    """docs-serve target must pass --frozen to prevent lock rewrites."""
    makefile = (ROOT / "Makefile").read_text()
    assert "uv run --frozen --group docs mkdocs serve" in makefile, (
        "docs-serve must use 'uv run --frozen --group docs mkdocs serve'"
    )


def test_gitignore_excludes_site_dir() -> None:
    """/site/ must be in .gitignore to prevent committing generated output."""
    gitignore = (ROOT / ".gitignore").read_text()
    assert "/site/" in gitignore, (
        "/site/ must be listed in .gitignore so 'make docs-build' output cannot be committed"
    )
