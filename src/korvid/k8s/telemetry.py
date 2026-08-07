from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

ReadOperation = Literal["list", "watch_open", "watch_event", "get", "error"]


@dataclass(frozen=True)
class ReadTelemetryEvent:
    operation: ReadOperation
    path: str
    decoded_bytes: int = 0
    object_count: int = 0
    status: int | None = None


ReadTelemetry = Callable[[ReadTelemetryEvent], None]
