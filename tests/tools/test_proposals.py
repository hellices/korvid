"""WriteProposal / ProposalStore (issue #110).

External MCP callers submit immutable write proposals; only a fresh TUI
keystroke may approve one. The store owns the state machine (pending →
approved → executed/failed, or pending → denied/expired/cancelled), the
per-session and global pending caps, the size bounds, and the TTL — all
enforced here so no transport can bypass them.
"""

from __future__ import annotations

import dataclasses
import threading

import pytest

from korvid.tools.proposals import (
    ProposalLimitError,
    ProposalStore,
    ProposalTooLargeError,
    WriteProposal,
)


class Clock:
    """Deterministic monotonic clock for TTL tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_store(**kwargs: object) -> tuple[ProposalStore, Clock]:
    clock = Clock()
    store = ProposalStore(clock=clock, **kwargs)  # type: ignore[arg-type]  # test kwargs
    return store, clock


def submit(store: ProposalStore, *, session: str = "s1", name: str = "web-1") -> WriteProposal:
    return store.submit(
        action="delete",
        group="",
        version="v1",
        kind="pods",
        namespace="default",
        name=name,
        arguments_json="{}",
        uid="uid-1",
        context="kind-kind",
        context_epoch=3,
        summary="DELETE v1.pods/web-1 in namespace default",
        preview=("- pod will be removed",),
        session_id=session,
        client_name="claude-code",
        client_version="1.2",
    )


def test_submit_returns_a_pending_proposal_with_an_unguessable_id() -> None:
    store, _ = make_store()
    first = submit(store)
    second = submit(store, name="web-2")
    assert len(first.id) >= 32
    assert first.id != second.id
    record = store.get(first.id)
    assert record is not None
    proposal, state, reason = record
    assert state == "pending"
    assert reason == ""
    assert proposal.action == "delete"
    assert proposal.kind == "pods"
    assert proposal.namespace == "default"
    assert proposal.uid == "uid-1"
    assert proposal.context == "kind-kind"
    assert proposal.context_epoch == 3
    assert proposal.session_id == "s1"
    assert proposal.client_name == "claude-code"
    assert proposal.expires_at > proposal.created_at


def test_proposal_records_are_immutable() -> None:
    store, _ = make_store()
    proposal = submit(store)
    with pytest.raises(dataclasses.FrozenInstanceError, match="cannot assign"):
        proposal.name = "other"  # type: ignore[misc]  # the invariant under test


def test_get_unknown_id_returns_none() -> None:
    store, _ = make_store()
    assert store.get("nope") is None


def test_pending_proposals_expire_after_the_ttl() -> None:
    store, clock = make_store(ttl=600.0)
    proposal = submit(store)
    clock.now += 601.0
    record = store.get(proposal.id)
    assert record is not None
    _, state, reason = record
    assert state == "expired"
    assert "expired" in reason
    assert store.pending() == []


def test_per_session_pending_cap_is_enforced() -> None:
    store, _ = make_store(max_pending_per_session=2)
    submit(store, name="a")
    submit(store, name="b")
    with pytest.raises(ProposalLimitError, match="session"):
        submit(store, name="c")
    # Another session is unaffected by the first session's backlog.
    other = submit(store, session="s2", name="d")
    assert other.session_id == "s2"


def test_global_pending_cap_is_enforced() -> None:
    store, _ = make_store(max_pending_per_session=10, max_pending_total=2)
    submit(store, session="s1", name="a")
    submit(store, session="s2", name="b")
    with pytest.raises(ProposalLimitError, match="global"):
        submit(store, session="s3", name="c")


def test_resolving_a_proposal_frees_its_cap_slot() -> None:
    store, _ = make_store(max_pending_per_session=1)
    first = submit(store, name="a")
    assert store.resolve(first.id, "denied", reason="user denied")
    second = submit(store, name="b")
    assert second.id != first.id


def test_oversized_arguments_are_rejected() -> None:
    store, _ = make_store(max_argument_chars=16)
    with pytest.raises(ProposalTooLargeError, match="arguments"):
        store.submit(
            action="scale",
            group="apps",
            version="v1",
            kind="deployments",
            namespace="default",
            name="web",
            arguments_json='{"replicas": 500, "padding": "xxxxxxxxxxxx"}',
            uid=None,
            context=None,
            context_epoch=0,
            summary="SCALE",
            preview=(),
            session_id="s1",
            client_name="",
            client_version="",
        )


def test_preview_is_truncated_to_the_line_bound() -> None:
    store, _ = make_store(max_preview_lines=3)
    proposal = store.submit(
        action="delete",
        group="",
        version="v1",
        kind="pods",
        namespace="default",
        name="web",
        arguments_json="{}",
        uid=None,
        context=None,
        context_epoch=0,
        summary="DELETE",
        preview=tuple(f"line {i}" for i in range(10)),
        session_id="s1",
        client_name="",
        client_version="",
    )
    assert len(proposal.preview) == 4  # 3 lines + truncation marker
    assert "truncated" in proposal.preview[-1]


def test_resolve_is_exactly_once() -> None:
    store, _ = make_store()
    proposal = submit(store)
    assert store.resolve(proposal.id, "denied", reason="user denied")
    assert not store.resolve(proposal.id, "cancelled", reason="too late")
    record = store.get(proposal.id)
    assert record is not None
    _, state, reason = record
    assert state == "denied"
    assert reason == "user denied"


def test_begin_execution_claims_a_pending_proposal_exactly_once() -> None:
    store, _ = make_store()
    proposal = submit(store)
    assert store.begin_execution(proposal.id)
    assert not store.begin_execution(proposal.id)
    record = store.get(proposal.id)
    assert record is not None
    assert record[1] == "approved"


def test_begin_execution_refuses_terminal_and_unknown_proposals() -> None:
    store, _ = make_store()
    proposal = submit(store)
    store.resolve(proposal.id, "denied", reason="no")
    assert not store.begin_execution(proposal.id)
    assert not store.begin_execution("unknown")


def test_finish_execution_records_the_terminal_outcome() -> None:
    store, _ = make_store()
    ok = submit(store, name="a")
    store.begin_execution(ok.id)
    store.finish_execution(ok.id, executed=True, reason="done")
    record = store.get(ok.id)
    assert record is not None
    assert record[1] == "executed"

    bad = submit(store, name="b")
    store.begin_execution(bad.id)
    store.finish_execution(bad.id, executed=False, reason="409 conflict")
    record = store.get(bad.id)
    assert record is not None
    assert record[1] == "failed"
    assert record[2] == "409 conflict"


def test_an_approved_proposal_does_not_expire_mid_execution() -> None:
    store, clock = make_store(ttl=600.0)
    proposal = submit(store)
    store.begin_execution(proposal.id)
    clock.now += 3600.0
    record = store.get(proposal.id)
    assert record is not None
    assert record[1] == "approved"


def test_cancel_requires_the_submitting_session() -> None:
    store, _ = make_store()
    proposal = submit(store, session="s1")
    assert not store.cancel(proposal.id, session_id="s2")
    assert store.cancel(proposal.id, session_id="s1")
    record = store.get(proposal.id)
    assert record is not None
    assert record[1] == "cancelled"
    # Terminal proposals cannot be re-cancelled.
    assert not store.cancel(proposal.id, session_id="s1")


def test_expire_all_expires_only_pending_proposals() -> None:
    store, _ = make_store()
    pending = submit(store, name="a")
    denied = submit(store, name="b")
    store.resolve(denied.id, "denied", reason="no")
    expired = store.expire_all(reason="context switched")
    assert [p.id for p in expired] == [pending.id]
    record = store.get(pending.id)
    assert record is not None
    assert record[1] == "expired"
    assert record[2] == "context switched"
    record = store.get(denied.id)
    assert record is not None
    assert record[1] == "denied"


def test_subscribers_are_notified_on_submit_and_state_changes() -> None:
    store, _ = make_store()
    events: list[int] = []
    store.subscribe(lambda: events.append(1))
    proposal = submit(store)
    assert len(events) == 1
    store.resolve(proposal.id, "denied", reason="no")
    assert len(events) == 2


def test_store_is_thread_safe_under_concurrent_claims() -> None:
    # The MCP server runs on its own thread; approval runs on the app
    # thread. Exactly one concurrent begin_execution may win.
    store, _ = make_store()
    proposal = submit(store)
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def claim() -> None:
        barrier.wait()
        wins.append(store.begin_execution(proposal.id))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(wins) == 1


# --- terminal retention (review round 1) -----------------------------------


def test_terminal_proposals_are_evicted_after_the_retention_window() -> None:
    store, clock = make_store(terminal_retention=600.0)
    proposal = submit(store)
    store.resolve(proposal.id, "denied", reason="no")
    clock.now += 599.0
    record = store.get(proposal.id)
    assert record is not None
    assert record[1] == "denied"
    clock.now += 2.0
    assert store.get(proposal.id) is None


def test_terminal_proposals_beyond_the_cap_evict_oldest_first() -> None:
    store, _ = make_store(max_pending_per_session=100, max_pending_total=100, max_terminal=2)
    resolved = [submit(store, name=f"web-{i}") for i in range(3)]
    for proposal in resolved:
        store.resolve(proposal.id, "denied", reason="no")
    assert store.get(resolved[0].id) is None
    newest = store.get(resolved[2].id)
    assert newest is not None
    assert newest[1] == "denied"


def test_terminal_eviction_never_touches_pending_or_approved_proposals() -> None:
    store, clock = make_store(terminal_retention=1.0)
    pending = submit(store, name="a")
    approved = submit(store, name="b")
    assert store.begin_execution(approved.id)
    denied = submit(store, name="c")
    store.resolve(denied.id, "denied", reason="no")
    clock.now += 2.0
    assert store.get(denied.id) is None
    assert store.get(pending.id) is not None
    assert store.get(approved.id) is not None


def test_expire_all_returns_the_expired_proposals() -> None:
    store, _ = make_store()
    pending = submit(store, name="a")
    denied = submit(store, name="b")
    store.resolve(denied.id, "denied", reason="no")
    expired = store.expire_all(reason="context switched")
    assert [p.id for p in expired] == [pending.id]
