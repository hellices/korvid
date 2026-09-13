from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent


class MkDocsLoader(yaml.SafeLoader):
    """Safe loader that tolerates MkDocs' Python name tags."""


MkDocsLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:",
    lambda loader, suffix, node: suffix,
)


def load_mkdocs_config() -> dict[str, object]:
    return yaml.load(
        (ROOT / "mkdocs.yml").read_text(encoding="utf-8"),
        Loader=MkDocsLoader,
    )


def test_stable_install_precedes_development_source() -> None:
    source = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    stable = source.index("## Install")
    development = source.index("## Development build")
    git_source = source.index("git+https://github.com/hellices/korvid")
    assert stable < development < git_source


def test_unreleased_page_identifies_main_and_stable_release() -> None:
    source = (ROOT / "docs" / "release-notes" / "unreleased.md").read_text(encoding="utf-8")
    opening = source.split("## ", 1)[0]
    assert "`main`" in opening
    assert "https://github.com/hellices/korvid/releases/latest" in opening
    assert "https://github.com/hellices/korvid/milestone/6" in opening


def test_release_navigation_labels_unreleased_main() -> None:
    config = load_mkdocs_config()
    release_notes = config["nav"][-1]["Project"][-1]["Release notes"]
    assert release_notes[0] == {"Unreleased (main)": "release-notes/unreleased.md"}


def test_internal_docs_use_one_inherited_search_policy() -> None:
    config = load_mkdocs_config()
    plugins = [
        item if isinstance(item, str) else next(iter(item))
        for item in config["plugins"]
    ]
    assert plugins.index("meta") < plugins.index("search")

    defaults = yaml.safe_load(
        (ROOT / "docs" / "dev" / ".meta.yml").read_text(encoding="utf-8")
    )
    assert defaults == {"search": {"exclude": True}}


def test_public_dev_entrypoints_override_the_internal_default() -> None:
    for relative in (
        "docs/dev/README.md",
        "docs/dev/specs/2026-08-12-korvid-architecture.md",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert source.startswith("---\nsearch:\n  exclude: false\n---\n")


def test_maintainer_release_runbook_is_not_search_indexed() -> None:
    source = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")
    assert source.startswith("---\nsearch:\n  exclude: true\n---\n")


def test_sitemap_uses_the_search_exclusion_policy() -> None:
    source = (ROOT / "docs" / "overrides" / "sitemap.xml").read_text(
        encoding="utf-8"
    )
    assert 'file.page.meta.get("search", {})' in source
    assert 'search.get("exclude", false)' in source
