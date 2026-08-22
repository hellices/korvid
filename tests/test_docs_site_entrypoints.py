"""Canonical documentation entry points (Task 3, issue #169 follow-on).

Once the MkDocs site is deployed, `pyproject.toml` and the README's feature
index should point readers at the hosted site
(`https://hellices.github.io/korvid/<slug>/`) rather than a GitHub blob link
frozen to whatever commit happens to be on `main`. Links that are
intentionally source-adjacent — the security-reporting pointer, the demo
source directory, the git-source install fallback, and the architecture
design-plan doc — must keep pointing at GitHub, because they are about the
repository itself, not the rendered user guide.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
SITE_URL = "https://hellices.github.io/korvid/"

# Feature-index doc slugs that must resolve to the hosted site. Each maps to
# the docs/<slug>.md source file that MkDocs renders at /<slug>/.
HOSTED_FEATURE_SLUGS = [
    "overview",
    "keybindings",
    "tui",
    "ops",
    "resource-relationships",
    "helm-operators",
    "agent",
    "provider-plugins",
    "observability",
    "mcp",
    "airgap",
    "performance",
    "threat-model",
]


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _features_section() -> str:
    readme = _readme()
    match = re.search(r"## Features\n(.*?)\n## Status", readme, re.DOTALL)
    assert match is not None, "README must have a '## Features' section followed by '## Status'"
    return match.group(1)


def test_pyproject_documentation_url_is_the_official_site() -> None:
    """`[project.urls].Documentation` must point at the hosted MkDocs site."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["urls"]["Documentation"] == SITE_URL


def test_readme_links_to_official_site_near_the_top() -> None:
    """A `Documentation` link to the hosted site must appear before '## Why korvid'."""
    readme = _readme()
    why_index = readme.index("## Why korvid")
    top = readme[:why_index]
    assert SITE_URL in top, (
        "README must advertise the hosted documentation site "
        f"({SITE_URL}) before the '## Why korvid' section"
    )


def test_feature_index_links_point_to_the_hosted_site() -> None:
    """Every user-guide slug in the feature index must use its hosted-site URL."""
    section = _features_section()
    for slug in HOSTED_FEATURE_SLUGS:
        hosted = f"{SITE_URL}{slug}/"
        assert hosted in section, f"feature index must link to {hosted}"
        blob = f"https://github.com/hellices/korvid/blob/main/docs/{slug}.md"
        assert blob not in section, (
            f"feature index still links to the GitHub blob {blob} instead of {hosted}"
        )


def test_feature_index_preserves_security_and_development_plan_github_links() -> None:
    """Security-reporting and the architecture design-plan doc stay on GitHub."""
    section = _features_section()
    assert "https://github.com/hellices/korvid/blob/main/SECURITY.md" in section
    assert (
        "https://github.com/hellices/korvid/blob/main/docs/dev/specs/"
        "2026-08-12-korvid-architecture.md" in section
    )


def test_readme_preserves_demo_source_and_git_install_fallback_github_links() -> None:
    """Demo source and the git-source install fallback stay on GitHub, whole-README."""
    readme = _readme()
    assert "https://github.com/hellices/korvid/tree/main/docs/demo" in readme
    assert "uv tool install 'korvid[all] @ git+https://github.com/hellices/korvid'" in readme
