"""Decode Ollama's native terminal metrics into a provider-neutral event.

Ollama's `/api/chat` and `/api/generate` responses carry nanosecond duration
counters and token counts on their terminal frame — `total_duration`,
`load_duration`, `prompt_eval_duration`, `prompt_eval_count`, `eval_duration`,
`eval_count`. This module is the single place those raw, Ollama-specific
numbers are converted into korvid's normalized `provider_metrics` event
(issue #319): durations in seconds, counts as plain integers. It reads only
numbers, so nothing here can carry a prompt, a tool argument, a tool result,
a model name, or any other free-form content across the boundary.

The decode is defensive: an absent, negative, boolean, or non-numeric value
becomes nothing rather than a fabricated measurement, and a frame with none
of the fields present decodes to `None` so the adapter attaches no empty
round metrics.

LiteLLM's Ollama transformations surface only token counts and discard the
duration counters. korvid therefore captures the terminal frame at the
request-local HTTP response boundary before transformation, then hands only
those six numeric fields to this decoder.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Final

from korvid.agent.diagnostics import PROVIDER_METRICS_EVENT

#: Ollama reports every duration in nanoseconds.
NANOSECONDS_PER_SECOND: Final = 1_000_000_000


def _seconds(value: Any) -> float | None:
    """Convert a non-negative nanosecond count to seconds, else `None`."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number / NANOSECONDS_PER_SECOND


def _count(value: Any) -> int | None:
    """A non-negative integer token count, else `None`."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def decode_ollama_metrics(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Build a `provider_metrics` event from a raw Ollama terminal frame.

    Args:
        raw: The Ollama response frame (or any mapping that may carry its
            native duration/count fields), or `None`.

    Returns:
        A `provider_metrics` event mapping carrying only the fields that
        were present and usable, or `None` when `raw` is not a mapping or
        carries no usable metric — so the adapter never yields an empty
        metrics event.
    """
    if not isinstance(raw, Mapping):
        return None
    event: dict[str, Any] = {"type": PROVIDER_METRICS_EVENT}
    for key, value in (
        ("total_seconds", _seconds(raw.get("total_duration"))),
        ("load_seconds", _seconds(raw.get("load_duration"))),
        ("prompt_eval_seconds", _seconds(raw.get("prompt_eval_duration"))),
        ("prompt_tokens", _count(raw.get("prompt_eval_count"))),
        ("generation_seconds", _seconds(raw.get("eval_duration"))),
        ("generation_tokens", _count(raw.get("eval_count"))),
    ):
        if value is not None:
            event[key] = value
    if len(event) == 1:  # only the "type" key: nothing worth attaching
        return None
    return event
