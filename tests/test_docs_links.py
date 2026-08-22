"""Documentation links that MkDocs strict mode cannot validate from raw HTML."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"


def test_raw_html_hero_primary_cta_resolves_to_a_docs_source() -> None:
    """Raw HTML bypasses MkDocs' relative-link tree processor, so resolve it here."""
    index = (DOCS / "index.md").read_text()
    hero = index[index.index('<section class="hero">') : index.index("</section>")]
    match = re.search(r'<a class="md-button md-button--primary" href="([^"]+)"', hero)
    assert match is not None, "the hero must keep its primary CTA"

    href = match.group(1)
    parsed = urlsplit(href)
    assert not parsed.scheme, "the primary CTA must target the local guide"
    assert not parsed.netloc, "the primary CTA must target the local guide"
    relative = unquote(parsed.path)
    source_relative = f"{relative.rstrip('/')}.md" if relative.endswith("/") else relative
    target = (DOCS / source_relative).resolve()
    assert target.is_relative_to(DOCS.resolve()), "the primary CTA must stay inside docs/"
    assert target.is_file(), f"hero CTA {href!r} has no documentation source at {target}"


def test_scoreboard_source_directories_use_strict_safe_github_urls() -> None:
    """Extensionless links escaping docs/ are not rejected by MkDocs strict mode."""
    scoreboard = (DOCS / "evals" / "scoreboard.md").read_text()
    expected = {
        "https://github.com/hellices/korvid/tree/main/src/korvid/evals/scenarios",
        "https://github.com/hellices/korvid/tree/main/src/korvid/evals/journeys",
    }
    for url in expected:
        assert f"]({url})" in scoreboard
    assert "](../../src/korvid/evals/" not in scoreboard


def test_dev_readme_does_not_link_missing_directory_indexes() -> None:
    """MkDocs does not create index pages for bare specs/ and plans/ directories."""
    dev_readme = (DOCS / "dev" / "README.md").read_text()
    for broken in ("](../)", "](specs/)", "](plans/)"):
        assert broken not in dev_readme
    assert "](../index.md)" in dev_readme
    assert "https://github.com/hellices/korvid/tree/main/docs/dev/specs" in dev_readme
    assert "https://github.com/hellices/korvid/tree/main/docs/dev/plans" in dev_readme
