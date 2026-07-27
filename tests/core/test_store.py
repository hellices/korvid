from korvid.core.store import ALL_NAMESPACES, ResourceStore
from korvid.k8s.models import PodSummary


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


def test_clear_namespace_removes_only_that_namespace() -> None:
    """Per-namespace watch fallback re-LISTs one namespace at a time (issue #49):
    purging its slice must not wipe other namespaces from the shared bucket."""
    store = ResourceStore()
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("a", ns="default"))
    store.apply_event("pods", ALL_NAMESPACES, "ADDED", _pod("b", ns="prod"))
    seen: list[str] = []
    store.subscribe(seen.append)
    store.clear_namespace("pods", ALL_NAMESPACES, "prod")
    result = store.get("pods", ALL_NAMESPACES)
    assert [(p.namespace, p.name) for p in result] == [("default", "a")]
    assert seen == ["pods"]


def test_clear_namespace_on_missing_bucket_is_noop() -> None:
    store = ResourceStore()
    store.clear_namespace("pods", ALL_NAMESPACES, "prod")
    assert store.get("pods", ALL_NAMESPACES) == []


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
