"""Shared Kubernetes Event timestamp selection.

Event objects — both core v1 `Event` and `events.k8s.io/v1 Event` — encode
the "latest occurrence" instant across several optional fields with the
same precedence everywhere the field is read: repeating events roll their
latest occurrence into `series.lastObservedTime`; non-repeating core v1
events set `lastTimestamp`; events.k8s.io events with no series set
`eventTime` (the initial observation); the deprecated `firstTimestamp` and
finally `metadata.creationTimestamp` are the last resorts — a valid event
may carry only those, and treating it as undated would misorder or
suppress it in every caller that ranks or ages events.
"""

from __future__ import annotations

from typing import Any


def select_event_timestamp(event: dict[str, Any]) -> str:
    """Raw timestamp string of an Event's latest occurrence, or `""`.

    Precedence: `series.lastObservedTime`, `lastTimestamp`, `eventTime`,
    `firstTimestamp`, `metadata.creationTimestamp`. `series` and `metadata`
    are guarded against being absent or not a mapping — some client
    payloads carry `None` or a malformed nested value there, and this
    helper must fall through rather than raise. Callers own parsing
    (RFC 3339 → `datetime`) and display formatting; this helper only
    selects which raw field wins.
    """
    series = event.get("series")
    series = series if isinstance(series, dict) else {}
    metadata = event.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    raw = (
        series.get("lastObservedTime")
        or event.get("lastTimestamp")
        or event.get("eventTime")
        or event.get("firstTimestamp")
        or metadata.get("creationTimestamp")
        or ""
    )
    return str(raw)
