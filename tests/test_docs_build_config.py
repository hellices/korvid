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
from typing import Any

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


def _load_mkdocs_config() -> dict[str, Any]:
    """Parse `mkdocs.yml`, tolerating its `!!python/name:...` custom tag.

    `SafeLoader` refuses arbitrary Python-object tags. This config only uses
    one (`pymdownx.superfences`'s code-format callable), which the tests
    below don't need to resolve, so map it to an inert placeholder string.

    Returns:
        The parsed mapping.
    """

    class _TolerantLoader(yaml.SafeLoader):
        pass

    # PyYAML's stubs leave `add_multi_constructor` unannotated, so mypy
    # --strict rejects the call rather than the code.
    _TolerantLoader.add_multi_constructor(  # type: ignore[no-untyped-call]  # untyped in types-PyYAML
        "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix
    )
    config = yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=_TolerantLoader)
    assert isinstance(config, dict), "mkdocs.yml must parse to a mapping"
    return config


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


def test_getting_started_version_consistent_with_readme_publication_state() -> None:
    """Cross-check getting-started.md against README's publication marker and Git fallback.

    README can contain the marker ``Until `<version>` is published on PyPI`` to
    signal that the in-repo source version has not yet been pushed to PyPI.

    **While that marker is present** (unpublished state):
    - getting-started.md must *not* claim that version is "the current published
      release" — doing so misleads users into pinning a version that doesn't exist
      on PyPI yet.
    - getting-started.md must include the exact Git fallback install command from
      README so users can install from source in the meantime.
    - Pinned `==<version>` install commands in getting-started.md must use the
      last *actually published* version (the one not covered by the marker), not
      the in-repo version awaiting publication.

    **Once the marker is removed** (published state):
    - getting-started.md must declare the project version as the current
      published release.
    - Pinned commands must reference the project version.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project_version = pyproject["project"]["version"]

    readme = (ROOT / "README.md").read_text()
    getting_started = (ROOT / "docs" / "getting-started.md").read_text()

    # Detect the README's "not yet published" marker, e.g.:
    #   Until `0.2.0` is published on PyPI, install the reviewed `main` source instead:
    unpublished_match = re.search(
        r"Until `([^`]+)` is published on PyPI",
        readme,
    )

    # Extract the Git fallback command from README (the canonical source of truth)
    git_fallback_match = re.search(
        r"uv tool install '(korvid\[all\] @ git\+[^']+)'",
        readme,
    )

    if unpublished_match is not None:
        unpublished_version = unpublished_match.group(1)
        # The guide must NOT present the unpublished version as already published
        false_published_pattern = re.search(
            rf"\*\*`{re.escape(unpublished_version)}`\*\* is the current published release",
            getting_started,
        )
        assert false_published_pattern is None, (
            f"getting-started.md claims {unpublished_version!r} is 'the current "
            f"published release', but README's publication marker says it is not yet "
            f"on PyPI ('Until `{unpublished_version}` is published on PyPI'). "
            "Update getting-started.md to use the last published version in pinned "
            "commands and include the Git fallback for current-main installs."
        )
        # The guide must include the Git fallback so users can reach current main
        assert git_fallback_match is not None, (
            "README contains 'Until `...` is published on PyPI' but no "
            "uv tool install '...@ git+...' fallback command was found in README — "
            "README itself is inconsistent."
        )
        git_fallback_spec = git_fallback_match.group(1)
        assert git_fallback_spec in getting_started, (
            f"README's Git fallback install spec {git_fallback_spec!r} must appear "
            "in getting-started.md so readers installing current main know the "
            "correct command while the PyPI release is pending."
        )
        # Pinned commands in getting-started.md must NOT use the unpublished version
        pinned_unpublished = re.search(
            rf"=={re.escape(unpublished_version)}(?:['\"]|$)",
            getting_started,
            re.MULTILINE,
        )
        assert pinned_unpublished is None, (
            f"getting-started.md contains pinned install commands for "
            f"{unpublished_version!r} (e.g. `korvid[all]=={unpublished_version}`), "
            f"but README says this version is not yet on PyPI. Use the last published "
            "version in pinned commands and add the Git fallback for current main."
        )
    else:
        # Publication marker removed — guide must advertise the project version
        published_match = re.search(
            r"## Current release\s*\n\s*\*\*`([^`]+)`\*\* is the current published release",
            getting_started,
        )
        assert published_match is not None, (
            "README's 'Until ... is published on PyPI' marker has been removed, "
            "meaning the project version is now on PyPI. getting-started.md must "
            "state the current published release version under a '## Current release' heading."
        )
        assert published_match.group(1) == project_version, (
            f"getting-started.md advertises {published_match.group(1)!r} as current, "
            f"but pyproject.toml's project.version is {project_version!r}. "
            "Sync getting-started.md with the published project version."
        )
