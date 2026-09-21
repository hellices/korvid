"""Small configuration-section parsers shared by the main config loader."""

from __future__ import annotations

from typing import Any


def mapping_section(value: object) -> dict[str, Any]:
    """Treat a non-mapping optional section as absent, without coercing it."""
    return value if isinstance(value, dict) else {}


def parse_keybindings(value: object, warnings: list[str]) -> tuple[dict[str, str], bool]:
    """Parse the section shape, leaving entry validation to the keymap planner.

    Returns:
        Overrides and whether a malformed persisted section needs explicit cleanup.
    """
    if value is None:
        return {}, False
    if isinstance(value, dict):
        return dict(value), False
    warnings.append(
        "keybindings: expected an action-to-key mapping; using defaults. "
        "Open :keys and press F8, F9, then F10 to remove the invalid section."
    )
    return {}, True
