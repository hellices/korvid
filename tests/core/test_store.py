"""`ResourceStore` CRUD is covered through `WatchManager` and UI integration.

This file keeps the one contract with no equivalent boundary elsewhere:
`get()` orders `ALL_NAMESPACES` by the `(namespace, name)` tuple, not by the
internal `"namespace/name"` key string. A namespace such as `team-b` would
sort before `team` by key string but must sort after it.
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


def test_broken_subscriber_does_not_block_later_subscribers() -> None:
    store = ResourceStore()
    seen: list[str] = []

    def broken(_kind: str) -> None:
        raise RuntimeError("subscriber bug")

    store.subscribe(broken)
    store.subscribe(seen.append)
    store.apply_event("pods", "default", "ADDED", _pod("a"))

    assert seen == ["pods"]
