"""Validate generated documentation publication artifacts.

This checker reads the built `site/` outputs rather than source literals so
the same assertions can run before upload and after a local strict build.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SEARCH_INDEX = "search/search_index.json"
SITEMAP = "sitemap.xml"
SITE_URL = "https://hellices.github.io/korvid/"
CANONICAL_DEV_PREFIX = f"{SITE_URL}dev/"
PUBLIC_DEV_SEARCH_LOCATIONS = frozenset(
    {
        "dev/",
        "dev/specs/2026-08-12-korvid-architecture/",
    }
)
PUBLIC_DEV_SITEMAP_URLS = frozenset(
    {
        f"{SITE_URL}dev/",
        f"{SITE_URL}dev/specs/2026-08-12-korvid-architecture/",
    }
)

REQUIRED_SEARCH_LOCATIONS = frozenset(
    {
        "evals/methodology/",
    }
)
FORBIDDEN_SEARCH_LOCATIONS = frozenset(
    {
        "release/",
    }
)
REQUIRED_SITEMAP_URLS = frozenset(
    {
        f"{SITE_URL}evals/methodology/",
    }
)
FORBIDDEN_SITEMAP_URLS = frozenset(
    {
        f"{SITE_URL}release/",
    }
)


def _read_text(path: Path, label: str) -> tuple[str | None, list[str]]:
    try:
        return path.read_text(encoding="utf-8"), []
    except OSError as exc:
        return None, [f"{label} is not readable: {exc}"]


def _search_locations(site_dir: Path) -> tuple[set[str], list[str]]:
    path = site_dir / SEARCH_INDEX
    text, errors = _read_text(path, SEARCH_INDEX)
    if text is None:
        return set(), errors
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return set(), [f"{SEARCH_INDEX} is not valid JSON: {exc}"]
    if not isinstance(payload, dict):
        return set(), [f"{SEARCH_INDEX} must contain a top-level object"]
    docs = payload.get("docs")
    if not isinstance(docs, list):
        return set(), [f"{SEARCH_INDEX} must contain a 'docs' list"]

    locations: set[str] = set()
    malformed: list[str] = []
    for index, entry in enumerate(docs):
        if not isinstance(entry, dict):
            malformed.append(f"{SEARCH_INDEX} docs[{index}] is not an object")
            continue
        location = entry.get("location")
        if not isinstance(location, str):
            malformed.append(f"{SEARCH_INDEX} docs[{index}] is missing a string 'location'")
            continue
        locations.add(location.split("#", 1)[0])
    return locations, malformed


def _sitemap_locations(site_dir: Path) -> tuple[set[str], list[str]]:
    path = site_dir / SITEMAP
    text, errors = _read_text(path, SITEMAP)
    if text is None:
        return set(), errors
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return set(), [f"{SITEMAP} is not valid XML: {exc}"]
    locations = {
        element.text.strip()
        for element in root.findall(".//{*}loc")
        if isinstance(element.text, str) and element.text.strip()
    }
    return locations, []


def _membership_errors(
    actual: set[str],
    *,
    label: str,
    required: frozenset[str],
    forbidden: frozenset[str],
) -> list[str]:
    errors: list[str] = []
    for entry in sorted(required - actual):
        errors.append(
            f"{label} is missing required {'URL' if 'http' in entry else 'location'} {entry!r}"
        )
    for entry in sorted(forbidden & actual):
        errors.append(
            f"{label} unexpectedly includes forbidden {'URL' if 'http' in entry else 'location'} {entry!r}"
        )
    return errors


def _dev_scope_errors(
    actual: set[str],
    *,
    label: str,
    required: frozenset[str],
    item_name: str,
) -> list[str]:
    errors: list[str] = []
    for entry in sorted(required - actual):
        errors.append(f"{label} is missing required dev {item_name} {entry!r}")
    for entry in sorted(actual - required):
        errors.append(f"{label} unexpectedly includes non-public dev {item_name} {entry!r}")
    return errors


def check_site(site_dir: Path) -> list[str]:
    """Return actionable publication-artifact errors for a built `site/` tree."""

    search_locations, search_errors = _search_locations(site_dir)
    sitemap_locations, sitemap_errors = _sitemap_locations(site_dir)
    errors = [*search_errors, *sitemap_errors]
    errors.extend(
        _dev_scope_errors(
            {location for location in search_locations if location.startswith("dev/")},
            label=SEARCH_INDEX,
            required=PUBLIC_DEV_SEARCH_LOCATIONS,
            item_name="location",
        )
    )
    errors.extend(
        _membership_errors(
            search_locations,
            label=SEARCH_INDEX,
            required=REQUIRED_SEARCH_LOCATIONS,
            forbidden=FORBIDDEN_SEARCH_LOCATIONS,
        )
    )
    errors.extend(
        _dev_scope_errors(
            {
                location
                for location in sitemap_locations
                if location.startswith(CANONICAL_DEV_PREFIX)
            },
            label=SITEMAP,
            required=PUBLIC_DEV_SITEMAP_URLS,
            item_name="URL",
        )
    )
    errors.extend(
        _membership_errors(
            sitemap_locations,
            label=SITEMAP,
            required=REQUIRED_SITEMAP_URLS,
            forbidden=FORBIDDEN_SITEMAP_URLS,
        )
    )
    return errors


def main(argv: list[str]) -> int:
    """Validate the built docs site.

    Args:
        argv: Command-line arguments. Optionally accepts a `site/` directory path.

    Returns:
        Process exit status.
    """

    site_dir = Path(argv[1] if len(argv) > 1 else "site")
    errors = check_site(site_dir)
    if errors:
        print(f"{site_dir} failed publication artifact checks:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"{site_dir} publication artifacts look correct")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through tests
    raise SystemExit(main(sys.argv))
