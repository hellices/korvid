"""Shared provider stream limits and helpers."""

from __future__ import annotations

from typing import Final

from korvid.providers.errors import ProviderError

MAX_TOOL_CALLS: Final = 64
MAX_TOOL_ARGUMENTS_BYTES: Final = 65_536
MAX_REASONING_BYTES: Final = 262_144
MAX_PROBE_TEXT_BYTES: Final = 16_384


def _utf8_len(text: str) -> int:
    """Return the UTF-8 byte length for one text fragment."""
    return len(text.encode("utf-8"))


class BoundedTextAccumulator:
    """Incrementally accumulate text while enforcing a UTF-8 byte budget."""

    def __init__(self, *, max_bytes: int, label: str) -> None:
        self._max_bytes = max_bytes
        self._label = label
        self._fragments: list[str] = []
        self._byte_count = 0

    def append(self, fragment: str) -> None:
        """Append one fragment after checking only that fragment's UTF-8 bytes."""
        next_total = self._byte_count + _utf8_len(fragment)
        if next_total > self._max_bytes:
            raise ProviderError(f"{self._label} exceeds {self._max_bytes} UTF-8 bytes")
        if fragment:
            self._fragments.append(fragment)
        self._byte_count = next_total

    @property
    def value(self) -> str:
        """Return the accumulated text."""
        return "".join(self._fragments)


def append_bounded(current: str, fragment: str, *, max_bytes: int, label: str) -> str:
    """Append text while enforcing a UTF-8 byte budget."""
    acc = BoundedTextAccumulator(max_bytes=max_bytes, label=label)
    acc.append(current)
    acc.append(fragment)
    return acc.value


def require_count(current: int, *, max_count: int, label: str) -> None:
    """Raise when the next item would exceed the allowed count."""
    if current >= max_count:
        raise ProviderError(f"{label} exceeds {max_count}")
