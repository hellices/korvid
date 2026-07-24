"""In-memory resource cache fed by watch events; the UI's single read model.

Subscriber callbacks are isolated: a buggy subscriber must never propagate
into the watch loop that calls apply_event (it would kill the whole watch).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

logger = logging.getLogger(__name__)

ALL_NAMESPACES = "*"


class Summary(Protocol):
    """Structural interface satisfied by PodSummary, GenericSummary, and any other summary.

    Using @property makes this compatible with frozen dataclasses whose fields
    are read-only — we only need to read name and namespace, never set them.
    """

    @property
    def name(self) -> str: ...

    @property
    def namespace(self) -> str: ...


class ResourceStore:
    def __init__(self) -> None:
        # {(kind, scope): {"namespace/name": obj}}  — composite key avoids collisions
        # in ALL_NAMESPACES scope when two namespaces have same-named objects.
        self._data: dict[tuple[str, str], dict[str, Summary]] = {}
        self._subscribers: list[Callable[[str], None]] = []

    def apply_event(self, kind: str, scope: str, event_type: str, obj: Summary) -> None:
        bucket = self._data.setdefault((kind, scope), {})
        key = f"{obj.namespace}/{obj.name}"
        if event_type == "DELETED":
            bucket.pop(key, None)
        else:  # ADDED / MODIFIED
            bucket[key] = obj
        self._notify(kind)

    def get(self, kind: str, scope: str) -> list[Summary]:
        bucket = self._data.get((kind, scope), {})
        return sorted(bucket.values(), key=lambda o: (o.namespace, o.name))

    def clear(self, kind: str, scope: str) -> None:
        """Remove all objects for (kind, scope) and notify subscribers."""
        self._data.pop((kind, scope), None)
        self._notify(kind)

    def subscribe(self, callback: Callable[[str], None]) -> None:
        self._subscribers.append(callback)

    def _notify(self, kind: str) -> None:
        for callback in self._subscribers:
            try:
                callback(kind)
            except Exception:  # subscriber bugs must not kill the watch loop
                logger.exception("resource store subscriber failed")
