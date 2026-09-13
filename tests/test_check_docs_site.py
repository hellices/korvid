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
PUBLIC_CONTRIBUTOR_PATH = "dev/"
PUBLIC_ARCHITECTURE_PATH = "dev/specs/2026-08-12-korvid-architecture/"
PUBLIC_EVAL_PATH = "evals/methodology/"
INTERNAL_CONTRACT_TESTS_PATH = "dev/contract-tests/"
INTERNAL_RELEASE_PATH = "release/"


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
) -> Path:
    site = root / "site"
    search = site / "search"
    search.mkdir(parents=True)
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


def test_check_site_accepts_expected_publication_artifacts(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            PUBLIC_CONTRIBUTOR_PATH,
            PUBLIC_ARCHITECTURE_PATH,
            PUBLIC_EVAL_PATH,
        ],
        sitemap_locations=[
            f"{SITE_URL}{PUBLIC_CONTRIBUTOR_PATH}",
            f"{SITE_URL}{PUBLIC_ARCHITECTURE_PATH}",
            f"{SITE_URL}{PUBLIC_EVAL_PATH}",
        ],
    )

    assert module.check_site(site) == []


def test_check_site_requires_the_public_dev_index_in_search_and_sitemap(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            PUBLIC_ARCHITECTURE_PATH,
            PUBLIC_EVAL_PATH,
        ],
        sitemap_locations=[
            f"{SITE_URL}{PUBLIC_ARCHITECTURE_PATH}",
            f"{SITE_URL}{PUBLIC_EVAL_PATH}",
        ],
    )

    errors = module.check_site(site)
    assert "search/search_index.json is missing required dev location 'dev/'" in errors
    assert (
        "sitemap.xml is missing required dev URL 'https://hellices.github.io/korvid/dev/'" in errors
    )


def test_check_site_rejects_extra_internal_dev_pages_in_search_and_sitemap(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            PUBLIC_CONTRIBUTOR_PATH,
            PUBLIC_ARCHITECTURE_PATH,
            PUBLIC_EVAL_PATH,
            INTERNAL_CONTRACT_TESTS_PATH,
        ],
        sitemap_locations=[
            f"{SITE_URL}{PUBLIC_CONTRIBUTOR_PATH}",
            f"{SITE_URL}{PUBLIC_ARCHITECTURE_PATH}",
            f"{SITE_URL}{PUBLIC_EVAL_PATH}",
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
            PUBLIC_CONTRIBUTOR_PATH,
            PUBLIC_ARCHITECTURE_PATH,
            PUBLIC_EVAL_PATH,
            INTERNAL_RELEASE_PATH,
        ],
        sitemap_locations=[
            f"{SITE_URL}{PUBLIC_CONTRIBUTOR_PATH}",
            f"{SITE_URL}{PUBLIC_ARCHITECTURE_PATH}",
            f"{SITE_URL}{PUBLIC_EVAL_PATH}",
            f"{SITE_URL}{INTERNAL_RELEASE_PATH}",
        ],
    )

    errors = module.check_site(site)
    assert "search/search_index.json unexpectedly includes forbidden location 'release/'" in errors
    assert (
        "sitemap.xml unexpectedly includes forbidden URL "
        "'https://hellices.github.io/korvid/release/'" in errors
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
