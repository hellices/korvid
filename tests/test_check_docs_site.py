"""Unit tests for the generated-docs publication artifact checker."""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "check_docs_site.py"
SITE_URL = "https://hellices.github.io/korvid/"
PUBLIC_OVERVIEW_PATH = "overview/"
PUBLIC_GETTING_STARTED_PATH = "getting-started/"
PUBLIC_CONTRIBUTOR_PATH = "dev/"
PUBLIC_ARCHITECTURE_PATH = "dev/specs/2026-08-12-korvid-architecture/"
PUBLIC_UNRELEASED_PATH = "release-notes/unreleased/"
PUBLIC_VERSIONED_RELEASE_PATH = "release-notes/v0.4.1/"
PUBLIC_EVAL_PATHS = (
    "evals/methodology/",
    "evals/scenarios/",
    "evals/scoreboard/",
)
INTERNAL_CONTRACT_TESTS_PATH = "dev/contract-tests/"
INTERNAL_RELEASE_PATH = "release/"
PUBLIC_NAV_PATHS = (
    PUBLIC_OVERVIEW_PATH,
    PUBLIC_GETTING_STARTED_PATH,
    PUBLIC_CONTRIBUTOR_PATH,
    PUBLIC_ARCHITECTURE_PATH,
    PUBLIC_UNRELEASED_PATH,
    PUBLIC_VERSIONED_RELEASE_PATH,
)


def _module() -> types.ModuleType:
    assert SCRIPT.is_file(), f"{SCRIPT} must exist"
    spec = importlib.util.spec_from_file_location("check_docs_site", SCRIPT)
    assert spec is not None
    loader = spec.loader
    assert loader is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _write_site(
    root: Path,
    *,
    search_locations: list[str],
    sitemap_locations: list[str],
    home_nav_hrefs: list[str] | None = None,
) -> Path:
    site = root / "site"
    search = site / "search"
    search.mkdir(parents=True)
    nav_hrefs = list(PUBLIC_NAV_PATHS if home_nav_hrefs is None else home_nav_hrefs)
    links = "".join(f'<a class="md-nav__link" href="{escape(href)}">nav</a>' for href in nav_hrefs)
    (site / "index.html").write_text(
        f"<html><body><nav>{links}</nav></body></html>",
        encoding="utf-8",
    )
    (search / "search_index.json").write_text(
        json.dumps({"docs": [{"location": location} for location in search_locations]}),
        encoding="utf-8",
    )
    sitemap_urls = "".join(
        f"<url><loc>{escape(location)}</loc></url>" for location in sitemap_locations
    )
    (site / "sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><urlset>{sitemap_urls}</urlset>',
        encoding="utf-8",
    )
    return site


def _public_eval_urls() -> list[str]:
    return [f"{SITE_URL}{path}" for path in PUBLIC_EVAL_PATHS]


def _public_nav_urls() -> list[str]:
    return [f"{SITE_URL}{path}" for path in PUBLIC_NAV_PATHS]


def test_check_site_accepts_expected_publication_artifacts(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            *PUBLIC_NAV_PATHS,
            *PUBLIC_EVAL_PATHS,
        ],
        sitemap_locations=[
            *_public_nav_urls(),
            *_public_eval_urls(),
        ],
    )

    assert module.check_site(site) == []


def test_check_site_requires_the_public_dev_index_in_search_and_sitemap(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            PUBLIC_ARCHITECTURE_PATH,
            *PUBLIC_EVAL_PATHS,
        ],
        sitemap_locations=[
            f"{SITE_URL}{PUBLIC_ARCHITECTURE_PATH}",
            *_public_eval_urls(),
        ],
    )

    errors = module.check_site(site)
    assert "search/search_index.json is missing required dev location 'dev/'" in errors
    assert (
        "sitemap.xml is missing required dev URL 'https://hellices.github.io/korvid/dev/'" in errors
    )


def test_check_site_requires_all_cited_eval_pages_in_search_and_sitemap(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            PUBLIC_CONTRIBUTOR_PATH,
            PUBLIC_ARCHITECTURE_PATH,
            PUBLIC_EVAL_PATHS[0],
        ],
        sitemap_locations=[
            f"{SITE_URL}{PUBLIC_CONTRIBUTOR_PATH}",
            f"{SITE_URL}{PUBLIC_ARCHITECTURE_PATH}",
            f"{SITE_URL}{PUBLIC_EVAL_PATHS[0]}",
        ],
    )

    errors = module.check_site(site)
    assert "search/search_index.json is missing required location 'evals/scenarios/'" in errors
    assert "search/search_index.json is missing required location 'evals/scoreboard/'" in errors
    assert (
        "sitemap.xml is missing required URL 'https://hellices.github.io/korvid/evals/scenarios/'"
        in errors
    )
    assert (
        "sitemap.xml is missing required URL 'https://hellices.github.io/korvid/evals/scoreboard/'"
        in errors
    )


def test_check_site_rejects_extra_internal_dev_pages_in_search_and_sitemap(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            PUBLIC_CONTRIBUTOR_PATH,
            PUBLIC_ARCHITECTURE_PATH,
            *PUBLIC_EVAL_PATHS,
            INTERNAL_CONTRACT_TESTS_PATH,
        ],
        sitemap_locations=[
            f"{SITE_URL}{PUBLIC_CONTRIBUTOR_PATH}",
            f"{SITE_URL}{PUBLIC_ARCHITECTURE_PATH}",
            *_public_eval_urls(),
            f"{SITE_URL}{INTERNAL_CONTRACT_TESTS_PATH}",
        ],
    )

    errors = module.check_site(site)
    assert (
        "search/search_index.json unexpectedly includes non-public dev location "
        "'dev/contract-tests/'" in errors
    )
    assert (
        "sitemap.xml unexpectedly includes non-public dev URL "
        "'https://hellices.github.io/korvid/dev/contract-tests/'" in errors
    )


def test_check_site_reports_release_artifacts_as_forbidden(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            *PUBLIC_NAV_PATHS,
            *PUBLIC_EVAL_PATHS,
            INTERNAL_RELEASE_PATH,
        ],
        sitemap_locations=[
            *_public_nav_urls(),
            *_public_eval_urls(),
            f"{SITE_URL}{INTERNAL_RELEASE_PATH}",
        ],
    )

    errors = module.check_site(site)
    assert "search/search_index.json unexpectedly includes forbidden location 'release/'" in errors
    assert (
        "sitemap.xml unexpectedly includes forbidden URL "
        "'https://hellices.github.io/korvid/release/'" in errors
    )


def test_check_site_requires_primary_nav_guides_in_search_and_sitemap(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            path
            for path in (*PUBLIC_NAV_PATHS, *PUBLIC_EVAL_PATHS)
            if path != PUBLIC_GETTING_STARTED_PATH
        ],
        sitemap_locations=[
            url
            for url in (*_public_nav_urls(), *_public_eval_urls())
            if url != f"{SITE_URL}{PUBLIC_GETTING_STARTED_PATH}"
        ],
    )

    errors = module.check_site(site)
    assert "search/search_index.json is missing required location 'getting-started/'" in errors
    assert (
        "sitemap.xml is missing required URL "
        "'https://hellices.github.io/korvid/getting-started/'" in errors
    )


def test_check_site_requires_versioned_release_notes_in_search_and_sitemap(
    tmp_path: Path,
) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            path
            for path in (*PUBLIC_NAV_PATHS, *PUBLIC_EVAL_PATHS)
            if path != PUBLIC_VERSIONED_RELEASE_PATH
        ],
        sitemap_locations=[
            url
            for url in (*_public_nav_urls(), *_public_eval_urls())
            if url != f"{SITE_URL}{PUBLIC_VERSIONED_RELEASE_PATH}"
        ],
    )

    errors = module.check_site(site)
    assert "search/search_index.json is missing required location 'release-notes/v0.4.1/'" in errors
    assert (
        "sitemap.xml is missing required URL "
        "'https://hellices.github.io/korvid/release-notes/v0.4.1/'" in errors
    )


def test_main_prints_actionable_errors_and_returns_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[],
        sitemap_locations=[],
    )

    result = module.main(["check_docs_site.py", str(site)])

    captured = capsys.readouterr()
    assert result == 1
    assert f"{site} failed publication artifact checks:" in captured.out
    assert "search/search_index.json is missing required location" in captured.out
