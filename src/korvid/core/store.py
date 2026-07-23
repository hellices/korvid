"""In-memory resource cache fed by watch events; the UI's single read model.

Subscriber callbacks are isolated: a buggy subscriber must never propagate
into the watch loop that calls apply_event (it would kill the whole watch).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from korvid.k8s.models import PodSummary

logger = logging.getLogger(__name__)


class ResourceStore:
    def __init__(self) -> None:
        # {(kind, namespace): {name: obj}}
        self._data: dict[tuple[str, str], dict[str, PodSummary]] = {}
        self._subscribers: list[Callable[[str], None]] = []

    def apply_event(self, kind: str, event_type: str, obj: PodSummary) -> None:
        bucket = self._data.setdefault((kind, obj.namespace), {})
        if event_type == "DELETED":
            bucket.pop(obj.name, None)
        else:  # ADDED / MODIFIED
            bucket[obj.name] = obj
        for callback in self._subscribers:
            try:
                callback(kind)
            except Exception:  # subscriber bugs must not kill the watch loop
                logger.exception("resource store subscriber failed")

    def get(self, kind: str, namespace: str) -> list[PodSummary]:
        bucket = self._data.get((kind, namespace), {})
        return sorted(bucket.values(), key=lambda o: o.name)

    def subscribe(self, callback: Callable[[str], None]) -> None:
        self._subscribers.append(callback)
