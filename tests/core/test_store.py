"""`ResourceStore` — the bulk of its CRUD/subscriber mechanics is exercised
through the real `WatchManager` integration boundary in
`tests/core/test_watch.py` (ADDED/DELETED) and through numerous UI-level
tests exercising a real store for MODIFIED. This file keeps only one
contract with no equivalent boundary elsewhere: `get()`'s published order for
`ALL_NAMESPACES` is by the `(namespace, name)` tuple, not by the internal
`"namespace/name"` key string — `-` sorts before `/`, so a namespace like
`team-b` would come before `team` under key-string order but must sort after
it. No retained watch/UI test exercises more than one namespace at once.
"""

from __future__ import annotations

from korvid.core.store import ALL_NAMESPACES, ResourceStore
from korvid.k8s.models import PodSummary


def _pod(name: str, ns: str = "default") -> PodSummary:
    return PodSummary(name=name, namespace=ns, phase="Running", ready="1/1", restarts=0, node=None)


def test_all_namespaces_orders_by_namespace_then_name_not_by_key_string() -> None:
    store = ResourceStore()
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("x", ns="team"))
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("y", ns="team-b"))
    assert [(p.namespace, p.name) for p in store.get("pods", ALL_NAMESPACES)] == [
        ("team", "x"),
        ("team-b", "y"),
    ]
