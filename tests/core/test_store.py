from korvid.core.store import ResourceStore
from korvid.k8s.models import PodSummary


def _pod(name: str, ns: str = "default") -> PodSummary:
    return PodSummary(name=name, namespace=ns, phase="Running", ready="1/1", restarts=0, node=None)


def test_apply_added_and_get_sorted() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("b"))
    store.apply_event("pods", "ADDED", _pod("a"))
    assert [p.name for p in store.get("pods", "default")] == ["a", "b"]


def test_modified_replaces() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("a"))
    updated = PodSummary(
        name="a", namespace="default", phase="Failed", ready="0/1", restarts=3, node=None
    )
    store.apply_event("pods", "MODIFIED", updated)
    (pod,) = store.get("pods", "default")
    assert pod.phase == "Failed"


def test_deleted_removes() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("a"))
    store.apply_event("pods", "DELETED", _pod("a"))
    assert store.get("pods", "default") == []


def test_subscriber_notified_with_kind() -> None:
    store = ResourceStore()
    seen: list[str] = []
    store.subscribe(seen.append)
    store.apply_event("pods", "ADDED", _pod("a"))
    assert seen == ["pods"]


def test_broken_subscriber_does_not_block_others() -> None:
    store = ResourceStore()
    seen: list[str] = []

    def broken(kind: str) -> None:
        raise RuntimeError("subscriber bug")

    store.subscribe(broken)
    store.subscribe(seen.append)
    store.apply_event("pods", "ADDED", _pod("a"))  # must not raise
    assert seen == ["pods"]


def test_namespaces_isolated() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("a", ns="prod"))
    assert store.get("pods", "default") == []
    assert [p.name for p in store.get("pods", "prod")] == ["a"]


def test_clear_empties_bucket() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("a"))
    store.apply_event("pods", "ADDED", _pod("b"))
    store.clear("pods", "default")
    assert store.get("pods", "default") == []


def test_clear_notifies_subscribers() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("a"))
    seen: list[str] = []
    store.subscribe(seen.append)
    store.clear("pods", "default")
    assert seen == ["pods"]


def test_clear_other_namespace_unaffected() -> None:
    store = ResourceStore()
    store.apply_event("pods", "ADDED", _pod("a", ns="prod"))
    store.clear("pods", "default")  # clear a bucket that doesn't exist
    assert [p.name for p in store.get("pods", "prod")] == ["a"]
