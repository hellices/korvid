"""Typed events crossing the Kubernetes watch boundary."""

from __future__ import annotations

from enum import Enum
from typing import TypeAlias, TypeVar

_T = TypeVar("_T")


class WatchProgress(Enum):
    """Transport activity that may not project to a resource row."""

    LIVE_EVENT = "live_event"
    POLL = "poll"


WatchEvent: TypeAlias = WatchProgress | tuple[str, _T]
