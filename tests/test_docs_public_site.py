"""Public documentation invariants for release-facing copy and indexing."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from korvid import __version__

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
GETTING_STARTED_HOMEBREW_COMMAND = "brew install hellices/korvid/korvid"
GETTING_STARTED_HOMEBREW_TOKEN = "hellices/korvid/korvid"
RELEASE_LABEL_RE = re.compile(r"^v\d+\.\d+\.\d+(?: \(unpublished\))?$")


def _source(relative: str) -> str:
    return (DOCS / relative).read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _release_notes_nav() -> list[dict[str, str]]:
    config = _load_mkdocs_config()
    release_notes = config["nav"][-1]["Project"][-1]["Release notes"]
    assert isinstance(release_notes, list), "Release notes nav must stay an ordered list"
    return release_notes


def _assert_release_label_matches_path_version(label: str, path: str) -> None:
    match = re.fullmatch(r"release-notes/(v\d+\.\d+\.\d+)\.md", path)
    assert match is not None, f"unexpected release-notes path {path!r}"
    version = match.group(1)
    assert RELEASE_LABEL_RE.fullmatch(label), f"unexpected release-notes label {label!r}"
    assert label in {version, f"{version} (unpublished)"}, (
        f"release-notes label {label!r} must match the version encoded in {path!r}"
    )


def _highlight_shell(command: str) -> str:
    program = """
from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers.shell import BashLexer
import sys

sys.stdout.write(highlight(sys.stdin.read(), BashLexer(), HtmlFormatter()))
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        check=True,
        capture_output=True,
        input=command,
        text=True,
    )
    return result.stdout


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

    source = _source("getting-started.md")
    stable = source.index("## Install")
    extras = source.index("## Choose your extras")
    first_run = source.index("## First run")
    development = source.index("## Development build")
    git_source = source.index("git+https://github.com/hellices/korvid")
    assert stable < extras < first_run < development < git_source


def test_getting_started_current_release_copy_distinguishes_stable_and_dev_paths() -> None:
    """Getting Started must separate Homebrew/PyPI installs from unreleased `main` builds."""

    source = _normalized(_source("getting-started.md"))
    assert "Homebrew installs the version currently published by the live tap." in source
    assert (
        "`uv tool` and `pipx` commands below install the latest published package from PyPI"
        in source
    )
    assert "Homebrew installs the latest published release" not in source
    assert "Most users should stop at **Install**" in source
    assert "unreleased change from `main`" in source


def test_getting_started_homebrew_probe_token_is_present_in_source_and_survives_highlighting() -> (
    None
):
    """The smoke probe must use a token that exists in both source and rendered HTML."""

    source = _source("getting-started.md")
    assert GETTING_STARTED_HOMEBREW_COMMAND in source
    assert GETTING_STARTED_HOMEBREW_TOKEN in source

    highlighted = _highlight_shell(f"{GETTING_STARTED_HOMEBREW_COMMAND}\n")
    assert GETTING_STARTED_HOMEBREW_TOKEN in highlighted
    assert GETTING_STARTED_HOMEBREW_COMMAND not in highlighted


def test_unreleased_page_heading_matches_navigation_label() -> None:
    """The Unreleased page H1 must match the release-notes navigation label."""

    source = _source("release-notes/unreleased.md")
    assert source.startswith("# Unreleased (main)\n")


def test_unreleased_page_identifies_main_and_stable_release() -> None:
    """The release-notes landing copy must explain `main` and point to the stable release."""

    source = _source("release-notes/unreleased.md")
    opening = source.split("## ", 1)[0]
    assert "`main`" in opening
    assert "https://github.com/hellices/korvid/releases/latest" in opening
    assert "https://github.com/hellices/korvid/milestone/6" in opening
    assert "current release milestone" in opening.lower()


def test_release_navigation_labels_unreleased_main() -> None:
    """The release-notes nav entry must advertise that the page tracks `main`."""

    release_notes = _release_notes_nav()
    assert release_notes[0] == {"Unreleased (main)": "release-notes/unreleased.md"}


def test_release_navigation_tracks_the_live_release_without_hard_coding_versions() -> None:
    """Release-notes nav labels must match their files and the current project version."""

    release_notes = _release_notes_nav()
    assert release_notes[1] == {f"v{__version__}": f"release-notes/v{__version__}.md"}
    for entry in release_notes[1:]:
        assert len(entry) == 1, "each release-notes nav entry must stay a single label/path mapping"
        label, path = next(iter(entry.items()))
        _assert_release_label_matches_path_version(label, path)


def test_release_label_contract_rejects_numeric_prefix_mismatches() -> None:
    with pytest.raises(
        AssertionError,
        match=r"release-notes label 'v0\.5\.10' must match the version encoded in "
        r"'release-notes/v0\.5\.1\.md'",
    ):
        _assert_release_label_matches_path_version(
            "v0.5.10",
            "release-notes/v0.5.1.md",
        )


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


def test_top_level_dev_markdown_names_are_reserved_against_meta_prefix_matching() -> None:
    """A future `docs/dev*.md` page would silently inherit `docs/dev/.meta.yml`."""

    top_level_dev_pages = sorted(path.name for path in DOCS.glob("dev*.md"))
    assert top_level_dev_pages == [], (
        "reserve top-level docs/dev*.md names; Material's meta prefix matching would let "
        f"{top_level_dev_pages!r} inherit docs/dev/.meta.yml unexpectedly"
    )


def test_maintainer_release_runbook_is_not_search_indexed() -> None:
    """The maintainer-only release runbook must stay out of public search."""

    source = _source("release.md")
    assert source.startswith("---\nsearch:\n  exclude: true\n---\n")


def test_sitemap_uses_the_search_exclusion_policy() -> None:
    """The sitemap override must keep using the shared search-exclusion policy."""

    config = _load_mkdocs_config()
    assert config["theme"]["custom_dir"] == "docs/overrides"

    source = _source("overrides/sitemap.xml")
    meta_lookup = 'file.page.meta.get("search", {}).get("exclude", false)'
    assert meta_lookup in source
    assert "{% set search " not in source
    assert source.index("not file.page.is_link") < source.index(meta_lookup)
    assert source.index("(file.page.abs_url or file.page.canonical_url)") < source.index(
        meta_lookup
    )
