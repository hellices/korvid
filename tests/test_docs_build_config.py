"""Docs build configuration invariants.

Ensures that `make docs-build` and `make docs-serve` use `--frozen` so that
running them behind a corporate mirror cannot silently rewrite uv.lock to
private-index URLs.

Also ensures /site/ is listed in .gitignore so generated MkDocs output cannot
be accidentally committed.

Finally, ensures `mkdocs.yml` never downgrades `validation.links.not_found`
below its strict-mode-visible default (which would let genuinely broken
internal doc links pass `mkdocs build --strict` silently), and that
`docs/getting-started.md` always advertises the same install version as
`pyproject.toml`'s `project.version`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

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


def _load_mkdocs_config() -> dict[str, object]:
    """Parse `mkdocs.yml`, tolerating its `!!python/name:...` custom tag.

    `SafeLoader` refuses arbitrary Python-object tags. This config only uses
    one (`pymdownx.superfences`'s code-format callable), which the tests
    below don't need to resolve, so map it to an inert placeholder string.
    """

    class _TolerantLoader(yaml.SafeLoader):
        pass

    _TolerantLoader.add_multi_constructor(
        "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix
    )
    return yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=_TolerantLoader)


def test_mkdocs_config_does_not_downgrade_link_validation() -> None:
    """`validation.links.not_found` must not be lowered below the strict-mode default.

    MkDocs's own default for `not_found` is `warn`, which `strict: true`
    escalates to a build failure. Downgrading it (e.g. to `info`) would let
    genuinely broken internal doc links pass `make docs-build` silently.
    """
    config = _load_mkdocs_config()
    not_found = (config.get("validation") or {}).get("links", {}).get("not_found")
    assert not_found in (None, "warn", "warning", "error"), (
        "mkdocs.yml must not downgrade validation.links.not_found below its "
        f"strict-mode-visible default; found {not_found!r}"
    )


def test_getting_started_install_version_matches_pyproject() -> None:
    """`getting-started.md`'s current install version must equal `project.version`."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project_version = pyproject["project"]["version"]

    getting_started = (ROOT / "docs" / "getting-started.md").read_text()
    match = re.search(
        r"## Current release\s*\n\s*\*\*`([^`]+)`\*\* is the current published release",
        getting_started,
    )
    assert match is not None, (
        "getting-started.md must state the current published release version "
        "under a '## Current release' heading"
    )
    assert match.group(1) == project_version, (
        f"getting-started.md advertises {match.group(1)!r} as current, but "
        f"pyproject.toml's project.version is {project_version!r}"
    )
