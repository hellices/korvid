"""Log-streaming data types for the k8s layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LogLine:
    """A single decoded log line from a pod container.

    ``timestamp`` is the kubelet-attached RFC3339 timestamp (requested with
    ``timestamps=true`` and stripped from ``text``); ``None`` when the prefix
    could not be parsed.
    """

    pod: str
    container: str
    text: str
    timestamp: datetime | None = None
