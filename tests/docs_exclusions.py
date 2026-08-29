"""Fail-closed helpers for interpreting this repository's MkDocs exclusions."""

from __future__ import annotations


def parse_exclude_docs(block: str) -> tuple[str, ...]:
    """Normalize the plain gitignore-style entries this repository supports."""
    entries: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert not line.startswith("/"), (
            f"exclude_docs entry {line!r} is root-anchored, which this walk "
            "does not model; preserve anchoring explicitly before using it"
        )
        is_pattern = line.startswith("!") or any(char in line for char in "*?[")
        assert not is_pattern, (
            f"exclude_docs entry {line!r} uses gitignore pattern syntax this walk "
            "cannot resolve to a concrete docs path; teach `parse_exclude_docs` "
            "the pattern instead of letting the walk assert on an unpublished page"
        )
        entries.append(line.strip("/"))
    return tuple(entries)


def is_published(relative: str, excluded: tuple[str, ...]) -> bool:
    """Return whether a docs-relative page survives all exclusion entries."""

    def matches(entry: str) -> bool:
        if "/" in entry:
            return relative == entry or relative.startswith(f"{entry}/")
        return entry in relative.split("/")

    return not any(matches(entry) for entry in excluded)
