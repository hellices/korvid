"""Documentation links that MkDocs strict mode cannot validate from raw HTML."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import pytest
import yaml

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"

#: MkDocs treats both of these stems as a directory index page.
_INDEX_STEMS = frozenset({"index", "README"})

_FENCE = re.compile(r"^(?P<fence>`{3,}|~{3,}).*?^(?P=fence)", re.DOTALL | re.MULTILINE)
_MEDIA_ATTRIBUTE = re.compile(r"(?<![-\w])(?:src|poster|data-poster)=\"([^\"]+)\"")
#: A raw-HTML link that points into the shared asset tree (e.g. the mosaic's
#: full-resolution capture links) resolves exactly like `src` does.
_ASSET_HREF = re.compile(r"(?<![-\w])href=\"([^\"]*assets/[^\"]+)\"")


def _load_mkdocs_config() -> dict[str, Any]:
    """Parse `mkdocs.yml`, tolerating its `!!python/name:...` custom tag."""

    class _TolerantLoader(yaml.SafeLoader):
        pass

    add_multi_constructor = cast(
        "Callable[[str, Callable[[Any, str, Any], object]], None]",
        _TolerantLoader.add_multi_constructor,
    )
    add_multi_constructor("tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix)
    config = yaml.load(MKDOCS.read_text(encoding="utf-8"), Loader=_TolerantLoader)
    assert isinstance(config, dict), "mkdocs.yml must parse to a mapping"
    return config


def _uses_directory_urls() -> bool:
    """Whether MkDocs serves each page from its own directory.

    Returns:
        `mkdocs.yml`'s `use_directory_urls`, falling back to MkDocs' own
        default of `True` when the key is absent.
    """
    value = _load_mkdocs_config().get("use_directory_urls", True)
    assert isinstance(value, bool), (
        f"use_directory_urls must be a boolean, found {value!r} in mkdocs.yml"
    )
    return value


def _parse_exclude_docs(block: str) -> tuple[str, ...]:
    """Docs-relative paths an `exclude_docs` block keeps out of the site.

    MkDocs reads `exclude_docs` with gitignore syntax. This repository only
    uses plain entries — a directory (`superpowers/`) or a single page
    (`dev/scratch.md`) — and gitignore matches a slash-less entry as both a
    file and a directory, so every entry is normalised to one path that
    excludes itself and everything beneath it. Whether that path is anchored
    to the docs root or matched at any segment is decided by `_is_published`,
    which follows gitignore's own slash rule.

    Anything using wildcard or negation syntax cannot be resolved to a
    concrete docs path here. Such a line is rejected rather than dropped:
    silently ignoring it would leave the published-page walk asserting on a
    page MkDocs never builds.

    Args:
        block: The raw newline-separated `exclude_docs` value.

    Returns:
        One normalised docs-relative path per meaningful line.
    """
    entries: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        is_pattern = line.startswith("!") or any(char in line for char in "*?[")
        assert not is_pattern, (
            f"exclude_docs entry {line!r} uses gitignore pattern syntax this walk "
            "cannot resolve to a concrete docs path; teach `_parse_exclude_docs` "
            "the pattern instead of letting the walk assert on an unpublished page"
        )
        entries.append(line.strip("/"))
    return tuple(entries)


def _matches_exclude_entry(relative: str, entry: str) -> bool:
    """Whether one normalised `exclude_docs` entry unpublishes a page.

    Gitignore anchors a pattern that carries an internal separator to the
    file the pattern was declared in, and matches a slash-less pattern
    against every path segment. `dev/plans` therefore only removes the
    docs-root `dev/plans/**`, while a bare `superpowers` removes
    `superpowers/**` *and* `guide/superpowers/**` — and a bare `scratch.md`
    removes that filename at any depth.

    Args:
        relative: A page path relative to `docs/`, e.g. `dev/README.md`.
        entry: One normalised entry from `_parse_exclude_docs`.

    Returns:
        `True` when the entry matches the page itself or one of its parents.
    """
    if "/" in entry:
        return relative == entry or relative.startswith(f"{entry}/")
    return entry in relative.split("/")


def _is_published(relative: str, excluded: tuple[str, ...]) -> bool:
    """Whether a docs-relative page survives the `exclude_docs` entries.

    Args:
        relative: A page path relative to `docs/`, e.g. `dev/README.md`.
        excluded: Normalised entries from `_parse_exclude_docs`.

    Returns:
        `True` when no entry matches the page itself or one of its parents.
    """
    return not any(_matches_exclude_entry(relative, entry) for entry in excluded)


def _excluded_prefixes() -> tuple[str, ...]:
    """Docs-relative paths `mkdocs.yml` keeps out of the published site."""
    excluded = _load_mkdocs_config().get("exclude_docs") or ""
    assert isinstance(excluded, str), "exclude_docs must stay a newline-separated block"
    return _parse_exclude_docs(excluded)


def _public_markdown_sources() -> Iterator[Path]:
    """Every `docs/**/*.md` page MkDocs actually publishes."""
    excluded = _excluded_prefixes()
    for path in sorted(DOCS.rglob("*.md")):
        if _is_published(path.relative_to(DOCS).as_posix(), excluded):
            yield path


def _built_directory_url(source: Path) -> str:
    """The site-root-relative directory a page is served from.

    `use_directory_urls` is on (MkDocs' default), so `docs/tui.md` is served
    at `/tui/` and every relative asset URL in its raw HTML resolves against
    that directory — one level deeper than the Markdown source's own
    directory. `index.md`/`README.md` stay at their parent directory.

    Args:
        source: A Markdown page under `docs/`.

    Returns:
        A posix directory path relative to the site root, always ending in
        `/` (`""` for the site root itself).
    """
    relative = source.relative_to(DOCS)
    parent = relative.parent.as_posix()
    parent = "" if parent == "." else f"{parent}/"
    if relative.stem in _INDEX_STEMS:
        return parent
    return f"{parent}{relative.stem}/"


def _local_media_urls(source: Path) -> Iterator[str]:
    """Local raw-HTML media URLs on a page, ignoring code samples.

    Covers `src`, `poster`, the deferred `data-poster` the scene controller
    promotes on selection, and any `href` into the asset tree — a typo in
    the deferred attribute or in a full-resolution capture link would
    otherwise 404 only after a visitor picks that scene or clicks through.
    """
    text = _FENCE.sub("", source.read_text(encoding="utf-8"))
    for raw in [*_MEDIA_ATTRIBUTE.findall(text), *_ASSET_HREF.findall(text)]:
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        yield unquote(parsed.path)


def test_raw_html_hero_primary_cta_resolves_to_a_docs_source() -> None:
    """Raw HTML bypasses MkDocs' relative-link tree processor, so resolve it here."""
    index = (DOCS / "index.md").read_text(encoding="utf-8")
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


def test_raw_html_media_resolves_from_every_published_page_url() -> None:
    """Every raw-HTML `src`/`poster` must resolve from the page's built URL.

    MkDocs' relative-path tree processor only rewrites Markdown links, so a
    raw-HTML `<img src="assets/…">` is emitted verbatim. With
    `use_directory_urls` on, `docs/tui.md` is served from `/tui/`, so that
    same string resolves to `/tui/assets/…` — a 404 that neither
    `mkdocs build --strict` nor a substring grep can see. This walks every
    published page, resolves each local media URL exactly as a browser
    would, and asserts the docs source exists.
    """
    assert _uses_directory_urls() is True, (
        "every raw-HTML media URL on a concept page is written as `../assets/…`, "
        "which only resolves because MkDocs serves `docs/tui.md` from `/tui/`. "
        "With `use_directory_urls: false` those pages are served as `/tui.html` "
        "and each `../assets/…` escapes the site root into a 404 that this walk "
        "would no longer model; fix the page URLs before turning it off"
    )
    checked = 0
    for source in _public_markdown_sources():
        page_url = _built_directory_url(source)
        for url in _local_media_urls(source):
            resolved = posixpath.normpath(posixpath.join(f"/{page_url}", url)).lstrip("/")
            asset = (DOCS / resolved).resolve()
            assert asset.is_relative_to(DOCS.resolve()), (
                f"{source.relative_to(ROOT)} references {url!r}, which escapes docs/"
            )
            assert asset.is_file(), (
                f"{source.relative_to(ROOT)} is served from /{page_url} where {url!r} "
                f"resolves to /{resolved} — no such asset (expected {asset})"
            )
            checked += 1
    assert checked >= 13, f"the media walk must cover the storytelling assets, saw {checked}"


def test_exclude_docs_honours_single_page_entries_not_just_directories() -> None:
    """A file entry must remove that page from the walk, not be dropped.

    `exclude_docs` is gitignore-style, so `dev/scratch.md` and a slash-less
    `drafts` entry are both legitimate ways to unpublish content. Keeping
    only lines that end in `/` silently published them here, and the walk
    would then assert on raw HTML MkDocs never builds.
    """
    excluded = _parse_exclude_docs("overrides/\ndev/plans/\ndev/scratch.md\ndrafts\n\n# comment\n")
    assert excluded == ("overrides", "dev/plans", "dev/scratch.md", "drafts")
    assert not _is_published("dev/scratch.md", excluded), (
        "a single-page exclude_docs entry must remove exactly that page"
    )
    assert not _is_published("drafts/idea.md", excluded), (
        "a slash-less entry excludes the directory it names, as gitignore does"
    )
    assert not _is_published("dev/plans/2026-01-01-plan.md", excluded)
    assert _is_published("dev/README.md", excluded), (
        "excluding dev/scratch.md must not unpublish its siblings"
    )
    assert _is_published("dev/scratch.md.md", excluded), (
        "prefix matching must respect path segments"
    )


def test_exclude_docs_matches_slash_less_entries_at_every_path_segment() -> None:
    """Gitignore matches a slash-less entry at any depth, not only at the root.

    `mkdocs.yml` excludes a bare `superpowers`, so MkDocs drops both
    `superpowers/**` and any nested `**/superpowers/**` page. Anchoring the
    entry to the docs root left the nested pages in this walk's published
    set, which would then assert on raw HTML MkDocs never builds.

    An entry that carries an internal separator stays anchored to the docs
    root, exactly as gitignore anchors a pattern containing a slash — so the
    two behaviours have to be distinguished rather than merged.
    """
    excluded = _parse_exclude_docs("superpowers/\ndev/plans/\nscratch.md\n")
    assert excluded == ("superpowers", "dev/plans", "scratch.md")

    assert not _is_published("superpowers/specs/design.md", excluded), (
        "a slash-less entry still excludes the directory it names at the root"
    )
    assert not _is_published("guide/superpowers/page.md", excluded), (
        "gitignore matches a slash-less entry at any path segment, so a nested "
        "superpowers/ directory is unpublished too"
    )
    assert not _is_published("a/b/c/superpowers/deep/page.md", excluded)
    assert not _is_published("superpowers", excluded), (
        "the entry must keep matching the path it names exactly"
    )
    assert not _is_published("dev/notes/scratch.md", excluded), (
        "a slash-less file entry matches that filename at any depth as well"
    )
    assert not _is_published("scratch.md", excluded), (
        "segment matching must not weaken the root file exclusion"
    )

    assert _is_published("superpowers-guide/page.md", excluded), (
        "segment matching must not fire on a longer sibling directory name"
    )
    assert _is_published("guide/superpowers-guide/page.md", excluded)
    assert _is_published("guide/my-superpowers/page.md", excluded)
    assert _is_published("guide/scratch.md.bak.md", excluded)
    assert _is_published("guide/dev/plans/page.md", excluded), (
        "an entry containing a slash stays anchored at the docs root, as gitignore does"
    )


def test_exclude_docs_rejects_patterns_the_walk_cannot_resolve() -> None:
    """A glob or negation must fail loudly instead of being ignored."""
    for pattern in ("dev/*.md", "!dev/keep.md", "dev/plan-?.md", "dev/[ab].md"):
        with pytest.raises(AssertionError, match="gitignore pattern syntax"):
            _parse_exclude_docs(f"overrides/\n{pattern}\n")


def test_published_sources_match_the_committed_exclude_docs_block() -> None:
    """The real config must keep producing the published set the walk asserts on."""
    assert _excluded_prefixes() == ("overrides", "dev/plans", "superpowers")
    published = {path.relative_to(DOCS).as_posix() for path in _public_markdown_sources()}
    assert "index.md" in published
    assert "tui.md" in published
    assert not any(
        page.startswith(("overrides/", "dev/plans/", "superpowers/")) for page in published
    )


def test_scoreboard_source_directories_use_strict_safe_github_urls() -> None:
    """Extensionless links escaping docs/ are not rejected by MkDocs strict mode."""
    scoreboard = (DOCS / "evals" / "scoreboard.md").read_text(encoding="utf-8")
    expected = {
        "https://github.com/hellices/korvid/tree/main/src/korvid/evals/scenarios",
        "https://github.com/hellices/korvid/tree/main/src/korvid/evals/journeys",
    }
    for url in expected:
        assert f"]({url})" in scoreboard
    assert "](../../src/korvid/evals/" not in scoreboard


def test_dev_readme_does_not_link_missing_directory_indexes() -> None:
    """MkDocs does not create index pages for bare specs/ and plans/ directories."""
    dev_readme = (DOCS / "dev" / "README.md").read_text(encoding="utf-8")
    for broken in ("](../)", "](specs/)", "](plans/)"):
        assert broken not in dev_readme
    assert "](../index.md)" in dev_readme
    assert "https://github.com/hellices/korvid/tree/main/docs/dev/specs" in dev_readme
    assert "https://github.com/hellices/korvid/tree/main/docs/dev/plans" in dev_readme
