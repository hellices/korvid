"""`decode_ollama_metrics`: Ollama's native counters, normalized and safe.

The decoder is the only place Ollama's provider-specific nanosecond
durations and token counts become korvid's provider-neutral
`provider_metrics` event, and the only place that decides what a raw frame
is *not* allowed to smuggle across the boundary. These cases pin the ns→s
conversion, the count validation, and the refusal of anything unusable —
so a partly filled, malformed, or content-bearing frame can never become a
fabricated measurement or a leak.
"""

from __future__ import annotations

import math

from korvid.agent.diagnostics import (
    PROVIDER_METRICS_EVENT,
    provider_metrics_are_empty,
    provider_metrics_from_event,
)
from korvid.providers.ollama_metrics import decode_ollama_metrics


def _ollama_frame() -> dict[str, object]:
    """A full Ollama `/api/chat` terminal frame, durations in nanoseconds."""
    return {
        "model": "qwen3:8b",
        "message": {"role": "assistant", "content": "the pod is healthy"},
        "done": True,
        "total_duration": 6_000_000_000,
        "load_duration": 1_000_000_000,
        "prompt_eval_count": 1800,
        "prompt_eval_duration": 4_000_000_000,
        "eval_count": 20,
        "eval_duration": 1_000_000_000,
    }


def test_a_full_frame_decodes_every_field_to_seconds_and_counts() -> None:
    event = decode_ollama_metrics(_ollama_frame())

    assert event is not None
    assert event["type"] == PROVIDER_METRICS_EVENT
    assert event["total_seconds"] == 6.0
    assert event["load_seconds"] == 1.0
    assert event["prompt_eval_seconds"] == 4.0
    assert event["prompt_tokens"] == 1800
    assert event["generation_seconds"] == 1.0
    assert event["generation_tokens"] == 20


def test_the_decoded_event_round_trips_through_the_neutral_reader() -> None:
    event = decode_ollama_metrics(_ollama_frame())

    assert event is not None
    metrics = provider_metrics_from_event(event)
    assert not provider_metrics_are_empty(metrics)
    assert metrics.prompt_tokens == 1800
    assert metrics.generation_seconds == 1.0


def test_the_frame_content_never_reaches_the_event() -> None:
    event = decode_ollama_metrics(_ollama_frame())

    assert event is not None
    # Only numbers and the event type — never the model name or the message.
    assert set(event) <= {
        "type",
        "total_seconds",
        "load_seconds",
        "prompt_eval_seconds",
        "prompt_tokens",
        "generation_seconds",
        "generation_tokens",
    }
    assert "qwen3:8b" not in event.values()


def test_a_partial_frame_decodes_only_the_fields_present() -> None:
    event = decode_ollama_metrics({"eval_count": 42, "eval_duration": 2_000_000_000})

    assert event == {
        "type": PROVIDER_METRICS_EVENT,
        "generation_seconds": 2.0,
        "generation_tokens": 42,
    }


def test_none_decodes_to_none() -> None:
    assert decode_ollama_metrics(None) is None


def test_a_frame_without_any_metric_decodes_to_none() -> None:
    assert decode_ollama_metrics({"model": "qwen3:8b", "done": True}) is None


def test_negative_and_non_numeric_values_are_refused() -> None:
    event = decode_ollama_metrics(
        {
            "total_duration": -5,
            "load_duration": "soon",
            "prompt_eval_count": -1,
            "eval_count": 7,
        }
    )

    # Only the one usable count survives; the junk becomes nothing.
    assert event == {"type": PROVIDER_METRICS_EVENT, "generation_tokens": 7}


def test_a_boolean_is_not_mistaken_for_a_count() -> None:
    # `True` is an `int` in Python; a metrics decoder must not treat it as 1.
    assert decode_ollama_metrics({"eval_count": True}) is None


def test_a_zero_duration_is_kept_as_a_real_measurement() -> None:
    event = decode_ollama_metrics({"eval_duration": 0, "eval_count": 0})

    assert event == {
        "type": PROVIDER_METRICS_EVENT,
        "generation_seconds": 0.0,
        "generation_tokens": 0,
    }


def test_non_finite_durations_are_refused() -> None:
    event = decode_ollama_metrics(
        {
            "total_duration": math.inf,
            "load_duration": math.nan,
            "eval_duration": 1_000_000_000,
        }
    )

    assert event == {
        "type": PROVIDER_METRICS_EVENT,
        "generation_seconds": 1.0,
    }


def test_oversized_duration_integers_do_not_discard_valid_metrics() -> None:
    event = decode_ollama_metrics(
        {
            "total_duration": 10**400,
            "load_duration": -(10**400),
            "prompt_eval_duration": 10**400,
            "eval_duration": 1_000_000_000,
            "eval_count": 7,
        }
    )
    assert event == {
        "type": PROVIDER_METRICS_EVENT,
        "generation_seconds": 1.0,
        "generation_tokens": 7,
    }
