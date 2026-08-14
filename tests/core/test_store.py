from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core import store as store_module
from korvid.core.store import ALL_NAMESPACES, ResourceStore
from korvid.k8s import models
from korvid.k8s.models import PodSummary, format_age


def _pod(name: str, ns: str = "default") -> PodSummary:
    return PodSummary(name=name, namespace=ns, phase="Running", ready="1/1", restarts=0, node=None)


def test_apply_added_and_get_sorted() -> None:
    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("b"))
    store.apply_event("pods", "default", "ADDED", _pod("a"))
    assert [p.name for p in store.get("pods", "default")] == ["a", "b"]


def test_modified_replaces() -> None:
    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("a"))
    updated = PodSummary(
        name="a", namespace="default", phase="Failed", ready="0/1", restarts=3, node=None
    )
    store.apply_event("pods", "default", "MODIFIED", updated)
    summaries = store.get("pods", "default")
    assert len(summaries) == 1
    assert isinstance(summaries[0], PodSummary)
    pod = summaries[0]
    assert pod.phase == "Failed"


def test_deleted_removes() -> None:
    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("a"))
    store.apply_event("pods", "default", "DELETED", _pod("a"))
    assert store.get("pods", "default") == []


def test_subscriber_notified_with_kind() -> None:
    store = ResourceStore()
    seen: list[str] = []
    store.subscribe(seen.append)
    store.apply_event("pods", "default", "ADDED", _pod("a"))
    assert seen == ["pods"]


def test_broken_subscriber_does_not_block_others() -> None:
    store = ResourceStore()
    seen: list[str] = []

    def broken(kind: str) -> None:
        raise RuntimeError("subscriber bug")

    store.subscribe(broken)
    store.subscribe(seen.append)
    store.apply_event("pods", "default", "ADDED", _pod("a"))  # must not raise
    assert seen == ["pods"]


def test_namespaces_isolated() -> None:
    store = ResourceStore()
    store.apply_event("pods", "prod", "ADDED", _pod("a", ns="prod"))
    assert store.get("pods", "default") == []
    assert [p.name for p in store.get("pods", "prod")] == ["a"]


def test_clear_empties_bucket() -> None:
    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("a"))
    store.apply_event("pods", "default", "ADDED", _pod("b"))
    store.clear("pods", "default")
    assert store.get("pods", "default") == []


def test_clear_notifies_subscribers() -> None:
    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("a"))
    seen: list[str] = []
    store.subscribe(seen.append)
    store.clear("pods", "default")
    assert seen == ["pods"]


def test_clear_other_namespace_unaffected() -> None:
    store = ResourceStore()
    store.apply_event("pods", "prod", "ADDED", _pod("a", ns="prod"))
    store.clear("pods", "default")  # clear a bucket that doesn't exist
    assert [p.name for p in store.get("pods", "prod")] == ["a"]


# ---------------------------------------------------------------------------
# ALL_NAMESPACES scope
# ---------------------------------------------------------------------------


def test_all_namespaces_holds_pods_from_multiple_namespaces() -> None:
    """ALL_NAMESPACES scope stores objects from different namespaces under one bucket."""
    store = ResourceStore()
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("a", ns="default"))
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("a", ns="prod"))
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("b", ns="default"))
    result = store.get("pods", ALL_NAMESPACES)
    assert [(p.namespace, p.name) for p in result] == [
        ("default", "a"),
        ("default", "b"),
        ("prod", "a"),
    ]


def test_all_namespaces_deleted_removes_exact_match_only() -> None:
    """DELETED in ALL_NAMESPACES removes the (namespace, name) pair, not same-name in other ns."""
    store = ResourceStore()
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("a", ns="default"))
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("a", ns="prod"))
    store.apply_event("pods", ALL_NAMESPACES, "DELETED", _pod("a", ns="prod"))
    result = store.get("pods", ALL_NAMESPACES)
    assert len(result) == 1
    assert result[0].namespace == "default"
    assert result[0].name == "a"


def test_all_namespaces_get_sorted_by_namespace_then_name() -> None:
    """get returns items sorted by (namespace, name) for ALL_NAMESPACES scope."""
    store = ResourceStore()
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("z", ns="alpha"))
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("a", ns="beta"))
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("a", ns="alpha"))
    result = store.get("pods", ALL_NAMESPACES)
    assert [(p.namespace, p.name) for p in result] == [
        ("alpha", "a"),
        ("alpha", "z"),
        ("beta", "a"),
    ]


def test_clear_all_drops_every_bucket_and_notifies() -> None:
    """`:ctx` teardown (issue #36): no rows from the old cluster may survive."""
    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("a"))
    store.apply_event("deployments", "other", "ADDED", _pod("b", ns="other"))
    notified: list[str] = []
    store.subscribe(notified.append)

    store.clear_all()

    assert store.get("pods", "default") == []
    assert store.get("deployments", "other") == []
    assert set(notified) == {"pods", "deployments"}


def test_repeated_reads_do_not_resort_an_unchanged_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """A watch tick re-reads the whole bucket for every repaint, so ordering a
    1,000-object bucket again on every read is work the key set already
    settled. Sorting must happen once per key-set change, not once per read."""
    store = ResourceStore()
    for name in ("b", "a", "c"):
        store.apply_event("pods", "default", "ADDED", _pod(name))

    sorts = 0
    real_sorted = sorted

    def counting_sorted(*args: Any, **kwargs: Any) -> list[Any]:
        nonlocal sorts
        sorts += 1
        return real_sorted(*args, **kwargs)

    monkeypatch.setattr(store_module, "sorted", counting_sorted, raising=False)

    first = [p.name for p in store.get("pods", "default")]
    second = [p.name for p in store.get("pods", "default")]

    assert first == ["a", "b", "c"]
    assert second == first
    assert sorts == 1


def test_a_modified_object_is_returned_after_an_earlier_read() -> None:
    """Reusing a settled order must never reuse a settled object: MODIFIED
    replaces the value under an unchanged key."""

    def phase_of(store: ResourceStore) -> str:
        summary = store.get("pods", "default")[0]
        assert isinstance(summary, PodSummary)
        return summary.phase

    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("a"))
    assert phase_of(store) == "Running"

    store.apply_event(
        "pods",
        "default",
        "MODIFIED",
        PodSummary(
            name="a", namespace="default", phase="Failed", ready="0/1", restarts=3, node=None
        ),
    )

    assert phase_of(store) == "Failed"


def test_an_object_added_after_a_read_sorts_into_place() -> None:
    """A cached order must be discarded when the key set changes, or a new
    object would append instead of sorting into position."""
    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("b"))
    assert [p.name for p in store.get("pods", "default")] == ["b"]

    store.apply_event("pods", "default", "ADDED", _pod("a"))

    assert [p.name for p in store.get("pods", "default")] == ["a", "b"]


def test_a_deleted_object_leaves_the_remaining_order_intact() -> None:
    store = ResourceStore()
    for name in ("a", "b", "c"):
        store.apply_event("pods", "default", "ADDED", _pod(name))
    assert len(store.get("pods", "default")) == 3

    store.apply_event("pods", "default", "DELETED", _pod("b"))

    assert [p.name for p in store.get("pods", "default")] == ["a", "c"]


def test_namespace_ordering_survives_a_reused_order() -> None:
    """Keys are `namespace/name`, but the published order is by
    `(namespace, name)`; `-` sorts before `/`, so a key-string order would
    disagree with the tuple order for namespaces that prefix one another."""
    store = ResourceStore()
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("x", ns="team"))
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("y", ns="team-b"))
    assert [(p.namespace, p.name) for p in store.get("pods", ALL_NAMESPACES)] == [
        ("team", "x"),
        ("team-b", "y"),
    ]

    # Settle the order first, then read it again through the reuse path.
    store.apply_event("pods", ALL_NAMESPACES, "MODIFIED", _pod("x", ns="team"))

    ordered = [(p.namespace, p.name) for p in store.get("pods", ALL_NAMESPACES)]

    assert ordered == [("team", "x"), ("team-b", "y")]


def test_clearing_a_kind_discards_its_reused_order() -> None:
    """`clear` empties the bucket, so a leftover order cannot produce a wrong
    read — any repopulation re-invalidates it. What it would do is retain a
    key list for a kind nobody is showing, which is what this pins."""
    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("b"))
    assert [p.name for p in store.get("pods", "default")] == ["b"]

    store.clear("pods", "default")

    assert store.get("pods", "default") == []
    assert ("pods", "default") not in store._order
    store.apply_event("pods", "default", "ADDED", _pod("a"))
    assert [p.name for p in store.get("pods", "default")] == ["a"]


def test_clearing_every_kind_discards_each_reused_order() -> None:
    """A context switch purges every kind at once; the orders it settled for
    the previous cluster must not be retained until something repopulates
    them, which for a large view is a list per object."""
    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("b"))
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("c", ns="other"))
    assert store.get("pods", "default")
    assert store.get("pods", ALL_NAMESPACES)

    store.clear_all()

    assert store._order == {}
    store.apply_event("pods", "default", "ADDED", _pod("a"))
    assert [p.name for p in store.get("pods", "default")] == ["a"]


def test_a_bucket_grown_behind_the_cache_is_still_ordered_correctly() -> None:
    """Defence in depth: the settled order is only sound while every mutation
    goes through `apply_event`. A future contributor adding a path that
    forgets to invalidate should get a stale-but-repaired order, not a
    silently truncated table."""
    store = ResourceStore()
    store.apply_event("pods", "default", "ADDED", _pod("b"))
    assert [p.name for p in store.get("pods", "default")] == ["b"]

    store._data[("pods", "default")]["default/a"] = _pod("a")

    assert [p.name for p in store.get("pods", "default")] == ["a", "b"]


def test_a_bucket_shrunk_behind_the_cache_does_not_raise() -> None:
    """The same slip in the other direction must not crash a repaint."""
    store = ResourceStore()
    for name in ("a", "b"):
        store.apply_event("pods", "default", "ADDED", _pod(name))
    assert len(store.get("pods", "default")) == 2

    del store._data[("pods", "default")]["default/a"]

    assert [p.name for p in store.get("pods", "default")] == ["b"]


def test_a_delete_and_an_add_between_two_reads_do_not_reuse_a_dead_key() -> None:
    """The length tripwire cannot see a net-zero membership swap — one key
    leaves and another arrives between two reads — so this pins the only
    invariant that rests entirely on `apply_event` invalidating for itself."""
    store = ResourceStore()
    for name in ("a", "b"):
        store.apply_event("pods", "default", "ADDED", _pod(name))
    assert [p.name for p in store.get("pods", "default")] == ["a", "b"]

    store.apply_event("pods", "default", "DELETED", _pod("a"))
    store.apply_event("pods", "default", "ADDED", _pod("c"))

    assert [p.name for p in store.get("pods", "default")] == ["b", "c"]


def test_a_net_zero_swap_behind_the_cache_recovers_instead_of_raising() -> None:
    """The length tripwire cannot see one key leaving as another arrives, so a
    mutation path that forgot to invalidate would reach a dead key. That must
    re-order the read, not raise `KeyError` in the middle of a repaint."""
    store = ResourceStore()
    for name in ("a", "b"):
        store.apply_event("pods", "default", "ADDED", _pod(name))
    assert [p.name for p in store.get("pods", "default")] == ["a", "b"]

    bucket = store._data[("pods", "default")]
    del bucket["default/a"]
    bucket["default/c"] = _pod("c")

    assert [p.name for p in store.get("pods", "default")] == ["b", "c"]


def test_clearing_every_kind_also_drops_the_age_memo() -> None:
    """A context switch purges every object, so the age strings keyed by their
    creation timestamps have no reuse value against the next cluster."""
    now = datetime(2031, 9, 1, 0, 5, 0, tzinfo=UTC)
    assert format_age("2031-09-01T00:00:00Z", now) == "5m"
    assert models._AGE_WINDOWS

    ResourceStore().clear_all()

    assert models._AGE_WINDOWS == {}
