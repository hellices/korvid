from typing import Any

from korvid.k8s.events import select_event_timestamp


def test_prefers_series_last_observed_time_over_everything_else() -> None:
    event: dict[str, Any] = {
        "series": {"lastObservedTime": "2026-07-26T10:00:00Z"},
        "lastTimestamp": "2026-07-26T09:00:00Z",
        "eventTime": "2026-07-26T08:00:00Z",
        "firstTimestamp": "2026-07-26T07:00:00Z",
        "metadata": {"creationTimestamp": "2026-07-26T06:00:00Z"},
    }
    assert select_event_timestamp(event) == "2026-07-26T10:00:00Z"


def test_last_timestamp_wins_over_event_time_and_older_fallbacks() -> None:
    event: dict[str, Any] = {
        "lastTimestamp": "2026-07-26T09:00:00Z",
        "eventTime": "2026-07-26T08:00:00Z",
        "firstTimestamp": "2026-07-26T07:00:00Z",
        "metadata": {"creationTimestamp": "2026-07-26T06:00:00Z"},
    }
    assert select_event_timestamp(event) == "2026-07-26T09:00:00Z"


def test_event_time_wins_over_first_timestamp_and_creation_timestamp() -> None:
    event: dict[str, Any] = {
        "eventTime": "2026-07-26T08:00:00Z",
        "firstTimestamp": "2026-07-26T07:00:00Z",
        "metadata": {"creationTimestamp": "2026-07-26T06:00:00Z"},
    }
    assert select_event_timestamp(event) == "2026-07-26T08:00:00Z"


def test_first_timestamp_wins_over_creation_timestamp() -> None:
    event: dict[str, Any] = {
        "firstTimestamp": "2026-07-26T07:00:00Z",
        "metadata": {"creationTimestamp": "2026-07-26T06:00:00Z"},
    }
    assert select_event_timestamp(event) == "2026-07-26T07:00:00Z"


def test_falls_back_to_creation_timestamp_when_nothing_else_is_set() -> None:
    event: dict[str, Any] = {"metadata": {"creationTimestamp": "2026-07-26T06:00:00Z"}}
    assert select_event_timestamp(event) == "2026-07-26T06:00:00Z"


def test_empty_event_selects_nothing() -> None:
    assert select_event_timestamp({}) == ""


def test_absent_series_falls_through_to_non_series_fields() -> None:
    """No `series` key at all — the non-series fallbacks still apply."""
    event: dict[str, Any] = {"lastTimestamp": "2026-07-26T09:00:00Z"}
    assert select_event_timestamp(event) == "2026-07-26T09:00:00Z"


def test_non_mapping_series_is_ignored_rather_than_raising() -> None:
    """Malformed payloads may carry a non-mapping `series` (e.g. `None`,
    a string, or a list). Selection must not raise — it should behave as
    if no series data were present and fall through to the next field."""
    for malformed_series in (None, "not-a-mapping", [], 42):
        event: dict[str, Any] = {
            "series": malformed_series,
            "lastTimestamp": "2026-07-26T09:00:00Z",
        }
        assert select_event_timestamp(event) == "2026-07-26T09:00:00Z"


def test_non_mapping_series_with_no_other_fields_selects_nothing() -> None:
    assert select_event_timestamp({"series": "not-a-mapping"}) == ""


def test_non_mapping_metadata_is_ignored_rather_than_raising() -> None:
    """Symmetrical guard for `metadata` — the last fallback in the chain."""
    for malformed_metadata in (None, "not-a-mapping", [], 42):
        event: dict[str, Any] = {"metadata": malformed_metadata}
        assert select_event_timestamp(event) == ""
