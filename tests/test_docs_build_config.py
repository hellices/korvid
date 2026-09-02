"""Docs build configuration invariants.

Ensures that `make docs-build` and `make docs-serve` use `--frozen` so that
running them behind a corporate mirror cannot silently rewrite uv.lock to
private-index URLs.

Also ensures `mkdocs.yml` never downgrades `validation.links.not_found`
below its strict-mode-visible default (which would let genuinely broken
internal doc links pass `mkdocs build --strict` silently), guards the
checksum-pinned, byte-reviewed vendor and storytelling assets it loads, and
keeps internal-only quality-gate content out of the published site.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml

from tests.docs_exclusions import is_published, parse_exclude_docs

ROOT = Path(__file__).parent.parent
MATERIAL_BUNDLE = ROOT / "docs" / "assets" / "javascripts" / "bundle.d7400e89.min.js"
MERMAID_VENDOR = ROOT / "docs" / "assets" / "javascripts" / "vendor" / "mermaid-11.17.0.min.js"
RESIZE_OBSERVER_VENDOR = (
    ROOT / "docs" / "assets" / "javascripts" / "vendor" / "resize-observer-polyfill-1.5.1.js"
)
VISUAL_STORYTELLING = ROOT / "docs" / "assets" / "javascripts" / "visual-storytelling.js"
SCENE_FALLBACK = ROOT / "docs" / "assets" / "javascripts" / "scene-fallback.js"


def test_makefile_docs_build_uses_frozen() -> None:
    """docs-build target must pass --frozen to prevent lock rewrites."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    # Find the docs-build recipe line
    assert "uv run --frozen --group docs mkdocs build" in makefile, (
        "docs-build must use 'uv run --frozen --group docs mkdocs build --strict'"
    )


def test_makefile_docs_serve_uses_frozen() -> None:
    """docs-serve target must pass --frozen to prevent lock rewrites."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "uv run --frozen --group docs mkdocs serve" in makefile, (
        "docs-serve must use 'uv run --frozen --group docs mkdocs serve'"
    )


def test_docs_dependency_matches_the_vendored_material_bundle() -> None:
    """The hashed bundle override is specific to Material 9.7.7."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["dependency-groups"]["docs"] == ["mkdocs-material==9.7.7"]


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

    add_multi_constructor = cast(
        "Callable[[str, Callable[[Any, str, Any], object]], None]",
        _TolerantLoader.add_multi_constructor,
    )
    add_multi_constructor("tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix)
    config = yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=_TolerantLoader)
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


def _plugin_options(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Return a configured plugin's options, using `{}` for its short form."""
    plugins = config.get("plugins")
    assert isinstance(plugins, list), "mkdocs.yml must declare its plugins as a list"
    for plugin in plugins:
        if plugin == name:
            return {}
        if isinstance(plugin, dict) and name in plugin:
            options = plugin[name]
            assert isinstance(options, dict), f"{name} plugin options must be a mapping"
            return options
    return None


def test_mkdocs_disables_remote_fonts_and_external_asset_fetches() -> None:
    """Builds and visitors must not fetch executable assets from third parties."""
    config = _load_mkdocs_config()
    theme = config.get("theme")
    assert isinstance(theme, dict), "mkdocs.yml must configure a theme"
    assert theme.get("font") is False, (
        "theme.font must be false so Material does not emit fonts.googleapis.com "
        "or fonts.gstatic.com runtime requests"
    )
    privacy = _plugin_options(config, "privacy")
    assert privacy is not None, (
        "Material's privacy plugin must download external Mermaid assets at build "
        "time and serve them locally"
    )
    assert privacy.get("assets", True) is True
    assert privacy.get("assets_fetch", True) is False


def test_material_bundle_uses_checksum_pinned_local_vendor_assets() -> None:
    """The Material bundle and both dynamically loaded scripts are immutable."""
    assert MATERIAL_BUNDLE.is_file(), (
        "docs must override Material's bundle with the reviewed URL-pinned copy"
    )
    bundle = MATERIAL_BUNDLE.read_bytes()
    assert b'new URL("assets/javascripts/vendor/mermaid-11.17.0.min.js",__md_scope).href' in bundle
    assert (
        b'new URL("assets/javascripts/vendor/resize-observer-polyfill-1.5.1.js",'
        b"__md_scope).href" in bundle
    )
    assert b"/korvid/assets/javascripts/vendor/" not in bundle
    assert b"https://unpkg.com/" not in bundle
    assert hashlib.sha256(bundle).hexdigest() == (
        "1c6de1ec928cbec390682d3ba17e617828eb391d8e0f9a5b718e31617b824b2c"
    ), "the reviewed Material bundle override must not drift without an explicit update"
    assert hashlib.sha256(MERMAID_VENDOR.read_bytes()).hexdigest() == (
        "8d8e0eec56d3a83b4b3c87f42050845546dee93ebe1875d2117c12e6947c0cb3"
    )
    assert hashlib.sha256(RESIZE_OBSERVER_VENDOR.read_bytes()).hexdigest() == (
        "2290b5c60e0cdc62851fb687800237273ac53797595b1b133860c4f1386de378"
    )


def test_material_bundle_checkout_preserves_reviewed_bytes() -> None:
    """Git must not rewrite the checksum-pinned JavaScript to CRLF on Windows."""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "docs/assets/javascripts/bundle.d7400e89.min.js -text" in attributes
    assert "docs/assets/javascripts/vendor/*.js -text" in attributes


def test_mkdocs_loads_only_the_reviewed_local_storytelling_scripts() -> None:
    config = _load_mkdocs_config()
    assert config.get("extra_javascript") == [
        "assets/javascripts/scene-fallback.js",
        "assets/javascripts/visual-storytelling.js",
    ]
    fallback = SCENE_FALLBACK.read_bytes()
    assert hashlib.sha256(fallback).hexdigest() == (
        "eda3fc2798316fa155e6e020245e95b548853cd8c2be2fb3237d94c0373d454e"
    )
    assert b"\r" not in fallback, (
        "the reviewed bytes are LF-only; a CRLF checkout would break the pin above"
    )
    assert VISUAL_STORYTELLING.is_file()
    script = VISUAL_STORYTELLING.read_bytes()
    assert hashlib.sha256(script).hexdigest() == (
        "edcf34fad0b4b520bd72a0565aaa754ae8eb71f8a043080c6c275a64bd4b6a64"
    )
    assert b"\r" not in script, (
        "the reviewed bytes are LF-only; a CRLF checkout would break the pin above"
    )


def test_visual_storytelling_plan_pins_the_current_controller_bytes() -> None:
    """Replaying the executable plan must retain the reviewed controller."""
    plan = (
        ROOT / "docs" / "superpowers" / "plans" / "2026-08-22-visual-storytelling.md"
    ).read_text(encoding="utf-8")
    digest = hashlib.sha256(VISUAL_STORYTELLING.read_bytes()).hexdigest()
    assert digest in plan


def test_storytelling_script_checkout_preserves_reviewed_bytes() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "docs/assets/javascripts/visual-storytelling.js text eol=lf" in attributes
    assert "docs/assets/javascripts/scene-fallback.js text eol=lf" in attributes


def _first_markdown_heading(text: str) -> str:
    """The page title MkDocs would derive from a source file's first `#` heading.

    Args:
        text: The raw Markdown source of one page.

    Returns:
        The heading text with its leading `#` markers stripped, or `""` if
        the page has no top-level heading.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def test_public_markdown_excludes_all_quality_gate_content_and_links() -> None:
    """No published page — not just `dev/quality-gates.md` — may leak the topic.

    `dev/quality-gates.md` is excluded from the build, but
    `dev/specs/2026-07-24-korvid-engineering-standards.md` documents the same
    internal quality gates under its own heading (`## 4. Quality Gates —
    Three Layers`) and used to build (and search-index) fine on its own, so
    the operator excluded it too rather than trim just that heading.

    This deliberately enforces a stronger boundary than MkDocs search: paths,
    Markdown link destinations, headings, and prose all remain free of the
    rejected internal topic. The dependency-free scan derives the published
    source set from `mkdocs.yml`'s actual `exclude_docs` entries.
    """
    config = _load_mkdocs_config()
    excluded = config.get("exclude_docs")
    assert isinstance(excluded, str)
    entries = parse_exclude_docs(excluded)
    required_private_files = {
        "dev/quality-gates.md",
        "dev/specs/2026-07-24-korvid-engineering-standards.md",
    }
    assert required_private_files <= set(entries), (
        "both internal quality-gate sources must be excluded by mkdocs.yml itself"
    )

    published: list[Path] = []
    for path in sorted((ROOT / "docs").rglob("*.md")):
        relative = path.relative_to(ROOT / "docs").as_posix()
        if not is_published(relative, entries):
            continue
        published.append(path)

    assert published, "the published Markdown source set must not be empty"
    # Sanity-check the fixture itself: both known offenders must actually be
    # gone, or the scan below would be vacuously true.
    published_relative = {p.relative_to(ROOT / "docs").as_posix() for p in published}
    assert required_private_files.isdisjoint(published_relative)

    for path in published:
        relative = path.relative_to(ROOT / "docs").as_posix()
        text = path.read_text(encoding="utf-8")
        title = _first_markdown_heading(text)
        haystack = f"{relative} {title} {text}".lower()
        assert "quality gate" not in haystack, (
            f"{relative!r} leaks 'quality gate' into the published docs source set"
        )
        assert "quality-gates" not in haystack, (
            f"{relative!r} leaks 'quality-gates' into the published docs source set"
        )
