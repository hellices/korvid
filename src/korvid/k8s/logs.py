"""Log-streaming data types for the k8s layer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LogLine:
    """A single decoded log line from a pod container."""

    pod: str
    container: str
    text: str
