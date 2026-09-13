"""Public documentation invariants for release-facing copy and indexing."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parent.parent


def _load_mkdocs_config() -> dict[str, Any]:
    """Parse `mkdocs.yml`, tolerating MkDocs' `!!python/name` tag."""

    class _TolerantLoader(yaml.SafeLoader):
        pass

    add_multi_constructor = cast(
        "Callable[[str, Callable[[Any, str, Any], object]], None]",
        _TolerantLoader.add_multi_constructor,
    )
    add_multi_constructor("tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix)
    config = yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=_TolerantLoader)
    assert isinstance(config, dict), "mkdocs.yml must parse to a mapping"
    return config


def test_stable_install_precedes_development_source() -> None:
    """Published-install guidance must appear before the development build path."""

    source = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    stable = source.index("## Install")
    development = source.index("## Development build")
    git_source = source.index("git+https://github.com/hellices/korvid")
    assert stable < development < git_source


def test_getting_started_current_release_copy_distinguishes_stable_and_dev_paths() -> None:
    """Getting Started must separate Homebrew/PyPI installs from unreleased `main` builds."""

    source = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    assert "Homebrew installs the latest published release from the live tap." in source
    assert (
        "The `uv tool` and `pipx` commands below install the latest published package from PyPI."
        in source
    )
    assert (
        "Most users should stop at **Install**; use **Development build** only when you need an unreleased change from `main`."
        in source
    )


def test_unreleased_page_heading_matches_navigation_label() -> None:
    """The Unreleased page H1 must match the release-notes navigation label."""

    source = (ROOT / "docs" / "release-notes" / "unreleased.md").read_text(encoding="utf-8")
    assert source.startswith("# Unreleased (main)\n")


def test_unreleased_page_identifies_main_and_stable_release() -> None:
    """The release-notes landing copy must explain `main` and point to the stable release."""

    source = (ROOT / "docs" / "release-notes" / "unreleased.md").read_text(encoding="utf-8")
    opening = source.split("## ", 1)[0]
    assert "`main`" in opening
    assert "https://github.com/hellices/korvid/releases/latest" in opening
    assert "https://github.com/hellices/korvid/milestone/6" in opening


def test_release_navigation_labels_unreleased_main() -> None:
    """The release-notes nav entry must advertise that the page tracks `main`."""

    config = _load_mkdocs_config()
    release_notes = config["nav"][-1]["Project"][-1]["Release notes"]
    assert release_notes[0] == {"Unreleased (main)": "release-notes/unreleased.md"}


def test_internal_docs_use_one_inherited_search_policy() -> None:
    """Internal docs must inherit a single meta-search exclusion policy."""

    config = _load_mkdocs_config()
    plugins = [item if isinstance(item, str) else next(iter(item)) for item in config["plugins"]]
    assert plugins.index("meta") < plugins.index("search")

    defaults = yaml.safe_load((ROOT / "docs" / "dev" / ".meta.yml").read_text(encoding="utf-8"))
    assert defaults == {"search": {"exclude": True}}


def test_public_dev_entrypoints_override_the_internal_default() -> None:
    """Public contributor entrypoints must opt back into the search index."""

    for relative in (
        "docs/dev/README.md",
        "docs/dev/specs/2026-08-12-korvid-architecture.md",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert source.startswith("---\nsearch:\n  exclude: false\n---\n")


def test_maintainer_release_runbook_is_not_search_indexed() -> None:
    """The maintainer-only release runbook must stay out of public search."""

    source = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")
    assert source.startswith("---\nsearch:\n  exclude: true\n---\n")


def test_sitemap_uses_the_search_exclusion_policy() -> None:
    """The sitemap override must keep using the shared search-exclusion policy."""

    config = _load_mkdocs_config()
    assert config["theme"]["custom_dir"] == "docs/overrides"

    source = (ROOT / "docs" / "overrides" / "sitemap.xml").read_text(encoding="utf-8")
    assert 'file.page.meta.get("search", {})' in source
    assert 'search.get("exclude", false)' in source
