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
    def __init__(self, on_purge: Callable[[], None] | None = None) -> None:
        """Args:
        on_purge: Called by `clear_all` after every bucket is dropped, for
            caches keyed by data this store just retired. Injected rather
            than imported so `core` keeps knowing only the `Summary`
            protocol, and so one store's context switch cannot reach into
            state another store is still rendering from.
        """
        self._on_purge = on_purge
        # {(kind, scope): {"namespace/name": obj}}  — composite key avoids collisions
        # in ALL_NAMESPACES scope when two namespaces have same-named objects.
        self._data: dict[tuple[str, str], dict[str, Summary]] = {}
        self._subscribers: list[Callable[[str], None]] = []
        #: Settled `get()` order per bucket, as keys into `_data`. Dropped
        #: whenever a key enters or leaves that bucket; a MODIFIED event
        #: replaces a value under an unchanged key, which cannot move it.
        self._order: dict[tuple[str, str], list[str]] = {}

    def apply_event(self, kind: str, scope: str, event_type: str, obj: Summary) -> None:
        bucket = self._data.setdefault((kind, scope), {})
        key = f"{obj.namespace}/{obj.name}"
        if event_type == "DELETED":
            if bucket.pop(key, None) is not None:
                self._order.pop((kind, scope), None)
        else:  # ADDED / MODIFIED
            if key not in bucket:
                self._order.pop((kind, scope), None)
            bucket[key] = obj
        self._notify(kind)

    def get(self, kind: str, scope: str) -> list[Summary]:
        """Objects for (kind, scope), ordered by `(namespace, name)`.

        A repaint re-reads the whole bucket, so the order is settled once per
        key-set change instead of once per read: at 1,000 objects the ordering
        pass dominated the read, and watch churn is overwhelmingly MODIFIED
        events, which cannot reorder anything. The objects themselves are
        always re-read from the bucket, so a replaced value is never stale.
        """
        bucket = self._data.get((kind, scope))
        if bucket is None:
            return []
        order = self._order.get((kind, scope))
        # The length check is a tripwire, not the invalidation rule: it costs
        # O(1) and catches any mutation path that changes the bucket's size
        # without invalidating. A net-zero swap — one key leaving as another
        # arrives between two reads — is invisible to it, so the reuse below
        # also recovers from a dead key instead of raising mid-repaint.
        if order is None or len(order) != len(bucket):
            order = self._settle_order(kind, scope, bucket)
        try:
            return [bucket[key] for key in order]
        except KeyError:
            return [bucket[key] for key in self._settle_order(kind, scope, bucket)]

    def _settle_order(self, kind: str, scope: str, bucket: dict[str, Summary]) -> list[str]:
        """Order *bucket*'s keys by `(namespace, name)` and remember it."""
        order = [
            key for key, _ in sorted(bucket.items(), key=lambda kv: (kv[1].namespace, kv[1].name))
        ]
        self._order[(kind, scope)] = order
        return order

    def clear(self, kind: str, scope: str) -> None:
        """Remove all objects for (kind, scope) and notify subscribers."""
        self._data.pop((kind, scope), None)
        self._order.pop((kind, scope), None)
        self._notify(kind)

    def clear_all(self) -> None:
        """Drop every bucket and notify each affected kind once.

        Context switching (issue #36) purges the whole store: rows from the
        previous cluster must never render against the new one. The age memo
        Anything keyed by those objects — the injected `on_purge` hook, e.g.
        the age memo, whose keys are their creation timestamps — is retired
        with them rather than left to age out one entry at a time.
        """
        kinds = {kind for kind, _ in self._data}
        self._data.clear()
        self._order.clear()
        if self._on_purge is not None:
            self._on_purge()
        for kind in kinds:
            self._notify(kind)

    def subscribe(self, callback: Callable[[str], None]) -> None:
        self._subscribers.append(callback)

    def _notify(self, kind: str) -> None:
        for callback in self._subscribers:
            try:
                callback(kind)
            except Exception:  # subscriber bugs must not kill the watch loop
                logger.exception("resource store subscriber failed")
