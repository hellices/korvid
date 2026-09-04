"""Shared provider stream limits and helpers."""

from __future__ import annotations

from typing import Final

from korvid.providers.errors import ProviderError

MAX_TOOL_CALLS: Final = 64
MAX_TOOL_ARGUMENTS_BYTES: Final = 65_536
MAX_REASONING_BYTES: Final = 262_144
MAX_PROBE_TEXT_BYTES: Final = 16_384


def append_bounded(current: str, fragment: str, *, max_bytes: int, label: str) -> str:
    """Append text while enforcing a UTF-8 byte budget."""
    combined = current + fragment
    if len(combined.encode("utf-8")) > max_bytes:
        raise ProviderError(f"{label} exceeds {max_bytes} UTF-8 bytes")
    return combined


def require_count(current: int, *, max_count: int, label: str) -> None:
    """Raise when the next item would exceed the allowed count."""
    if current >= max_count:
        raise ProviderError(f"{label} exceeds {max_count}")
