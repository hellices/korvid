"""Unit tests for the generated-docs publication artifact checker."""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "check_docs_site.py"


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
            "dev/specs/2026-08-12-korvid-architecture/",
            "evals/methodology/",
        ],
        sitemap_locations=[
            "https://hellices.github.io/korvid/dev/specs/2026-08-12-korvid-architecture/",
            "https://hellices.github.io/korvid/evals/methodology/",
        ],
    )

    assert module.check_site(site) == []


def test_check_site_reports_missing_required_generated_entries(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=["evals/methodology/"],
        sitemap_locations=["https://hellices.github.io/korvid/evals/methodology/"],
    )

    errors = module.check_site(site)
    assert (
        "search/search_index.json is missing required location "
        "'dev/specs/2026-08-12-korvid-architecture/'" in errors
    )
    assert (
        "sitemap.xml is missing required URL "
        "'https://hellices.github.io/korvid/dev/specs/2026-08-12-korvid-architecture/'" in errors
    )


def test_check_site_reports_forbidden_generated_entries(tmp_path: Path) -> None:
    module = _module()
    site = _write_site(
        tmp_path,
        search_locations=[
            "dev/specs/2026-08-12-korvid-architecture/",
            "evals/methodology/",
            "dev/ui-controllers/",
            "release/",
        ],
        sitemap_locations=[
            "https://hellices.github.io/korvid/dev/specs/2026-08-12-korvid-architecture/",
            "https://hellices.github.io/korvid/evals/methodology/",
            "https://hellices.github.io/korvid/dev/ui-controllers/",
            "https://hellices.github.io/korvid/release/",
        ],
    )

    errors = module.check_site(site)
    assert (
        "search/search_index.json unexpectedly includes forbidden location "
        "'dev/ui-controllers/'" in errors
    )
    assert "search/search_index.json unexpectedly includes forbidden location 'release/'" in errors
    assert (
        "sitemap.xml unexpectedly includes forbidden URL "
        "'https://hellices.github.io/korvid/dev/ui-controllers/'" in errors
    )
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
