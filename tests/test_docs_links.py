"""Documentation links that MkDocs strict mode cannot validate from raw HTML."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"

#: MkDocs treats both of these stems as a directory index page.
_INDEX_STEMS = frozenset({"index", "README"})

_FENCE = re.compile(r"^(?P<fence>`{3,}|~{3,}).*?^(?P=fence)", re.DOTALL | re.MULTILINE)
_MEDIA_ATTRIBUTE = re.compile(r"(?<![-\w])(?:src|poster|data-poster)=\"([^\"]+)\"")


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


def _excluded_prefixes() -> tuple[str, ...]:
    """Directory prefixes `mkdocs.yml` keeps out of the published site."""
    excluded = _load_mkdocs_config().get("exclude_docs") or ""
    assert isinstance(excluded, str), "exclude_docs must stay a newline-separated block"
    return tuple(
        line.strip().rstrip("/") for line in excluded.splitlines() if line.strip().endswith("/")
    )


def _public_markdown_sources() -> Iterator[Path]:
    """Every `docs/**/*.md` page MkDocs actually publishes."""
    excluded = _excluded_prefixes()
    for path in sorted(DOCS.rglob("*.md")):
        relative = path.relative_to(DOCS).as_posix()
        if any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in excluded):
            continue
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

    Covers `src`, `poster`, and the deferred `data-poster` the scene
    controller promotes on selection — a typo in the deferred attribute
    would otherwise 404 only after a visitor picks that scene.
    """
    text = _FENCE.sub("", source.read_text(encoding="utf-8"))
    for raw in _MEDIA_ATTRIBUTE.findall(text):
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
