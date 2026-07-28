"""Unit tests for `DrainController` (issue #97 U3d).

The controller owns the post-approval drain execution: fail-closed intent
audit, cordon, plan re-check, pod-by-pod eviction with live progress, the
bounded post-eviction termination wait, and the outcome audit. Keybinding
routing, the cancel-by-repeat-press semantics, dialogs, and the
`@_tracks_cluster_write` wrapper stay on the app; these tests exercise the
controller directly against fakes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.drain import DrainPlan, DrainTarget
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.writes import WriteOps
from korvid.ui.drain import DrainController

NODES_META = ResourceMeta("Node", "nodes", "", "v1", False, ("no",))


def _target(name: str, *, ns: str = "default", pdb: str | None = None) -> DrainTarget:
    return DrainTarget(
        namespace=ns,
        name=name,
        uid=f"uid-{name}",
        local_storage=False,
        pdb_blocked=pdb,
    )


def _plan(*targets: DrainTarget) -> DrainPlan:
    return DrainPlan(targets=targets, skipped_daemonset=(), skipped_mirror=())


class FakeOps(WriteOps):
    """WriteOps fake for the drain surface the controller touches."""

    def __init__(self, plan: DrainPlan) -> None:
        self.plan = plan
        self.cordoned: list[tuple[str, bool]] = []
        self.evicted: list[str] = []
        self.remaining: tuple[str, ...] = ()
        self.evict_errors: dict[str, list[Exception]] = {}
        self.cordon_error: Exception | None = None
        self.plan_error: Exception | None = None

    async def cordon_node(self, name: str, unschedulable: bool, *, uid: str | None = None) -> None:
        if self.cordon_error is not None:
            raise self.cordon_error
        self.cordoned.append((name, unschedulable))

    async def evict_pod(self, namespace: str, name: str, *, uid: str | None = None) -> None:
        errors = self.evict_errors.get(name)
        if errors:
            raise errors.pop(0)
        self.evicted.append(f"{namespace}/{name}")

    async def drain_plan(self, node_name: str) -> DrainPlan:
        if self.plan_error is not None:
            raise self.plan_error
        return self.plan

    async def pods_on_node(self, node_name: str) -> tuple[str, ...]:
        return self.remaining

    async def delete_object(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        raise AssertionError("not part of the drain surface")

    async def scale_object(self, meta, namespace, name, replicas, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        raise AssertionError("not part of the drain surface")

    async def rollout_restart(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        raise AssertionError("not part of the drain surface")

    async def replace_object(self, meta, namespace, name, manifest, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        raise AssertionError("not part of the drain surface")


class Harness:
    def __init__(self, plan: DrainPlan) -> None:
        self.ops = FakeOps(plan)
        self.notifications: list[tuple[str, str]] = []
        self.audits: list[tuple[str, str]] = []
        self.progress: list[str] = []
        self.audit_error: Exception | None = None
        self.controller = DrainController(
            notify=self._notify,
            audit_write=self._audit_write,
            set_progress=self.progress.append,
        )
        # Fast test knobs, mirroring what the app-level tests always shrink.
        self.controller.wait_timeout = 0.2
        self.controller.wait_poll = 0.01
        self.controller.throttle_backoff = 0.01
        self.controller.settle_timeout = 0.1

    def _notify(self, message: str, *, severity: str = "information") -> None:
        self.notifications.append((message, severity))

    async def _audit_write(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None:
        if self.audit_error is not None and outcome == "intent":
            raise self.audit_error
        self.audits.append((detail, outcome))

    async def run(self, plan: DrainPlan | None = None) -> None:
        effective = plan if plan is not None else self.ops.plan
        await self.controller.run(self.ops, NODES_META, "node-1", "node-uid", effective)


async def test_intent_audit_failure_blocks_the_drain() -> None:
    """Fail-closed auditing: no intent record means nothing is cordoned."""
    h = Harness(_plan(_target("a")))
    h.audit_error = OSError("disk full")
    await h.run()
    assert h.ops.cordoned == []
    assert h.ops.evicted == []
    assert any("audit log unavailable" in m for m, _ in h.notifications)


async def test_cordon_failure_stops_before_any_eviction() -> None:
    h = Harness(_plan(_target("a")))
    h.ops.cordon_error = RuntimeError("forbidden")
    await h.run()
    assert h.ops.evicted == []
    assert ("cordon step failed", "error: forbidden") in h.audits
    assert any("cordon" in m for m, sev in h.notifications if sev == "error")


async def test_unapproved_pod_after_cordon_aborts_without_uncordon() -> None:
    """Pods that landed while the dialog was open were never approved:
    the drain aborts, audited, and the node stays cordoned."""
    approved = _plan(_target("a"))
    h = Harness(approved)
    h.ops.plan = _plan(_target("a"), _target("landed-later"))
    await h.run(approved)
    assert h.ops.cordoned == [("node-1", True)]
    assert h.ops.evicted == []
    assert any(outcome == "aborted" for _, outcome in h.audits)
    assert all(flag for _, flag in h.ops.cordoned)  # never uncordoned


async def test_happy_path_evicts_all_and_audits_success() -> None:
    h = Harness(_plan(_target("a"), _target("b")))
    await h.run()
    assert h.ops.evicted == ["default/a", "default/b"]
    assert h.audits[0] == ("planned evictions: 2", "intent")
    assert h.audits[-1][1] == "success"
    assert h.progress[-1] == ""  # progress cleared at the end


async def test_pdb_denial_counts_blocked_and_continues() -> None:
    denial = ApiStatusError(429, "Too Many Requests", body='{"causes":"DisruptionBudget"}')
    h = Harness(_plan(_target("a"), _target("b")))
    h.ops.evict_errors["a"] = [denial]
    await h.run()
    assert h.ops.evicted == ["default/b"]
    detail, outcome = h.audits[-1]
    assert "pdb-blocked 1" in detail
    assert outcome.startswith("partial")


async def test_throttled_429_is_retried_not_reported_blocked() -> None:
    throttle = ApiStatusError(429, "Too Many Requests", body="overloaded")
    h = Harness(_plan(_target("a")))
    h.ops.evict_errors["a"] = [throttle]
    await h.run()
    assert h.ops.evicted == ["default/a"]
    assert h.audits[-1][1] == "success"


async def test_lingering_pods_reported_as_partial() -> None:
    """A 201 only starts graceful deletion: pods still on the node at the
    wait deadline surface in the audit outcome."""
    h = Harness(_plan(_target("a")))
    h.ops.remaining = ("uid-a",)
    await h.run()
    detail, outcome = h.audits[-1]
    assert "not yet terminated" in detail
    assert outcome.startswith("partial")


async def test_cancellation_audits_and_reraises_without_uncordon() -> None:
    h = Harness(_plan(_target("a"), _target("b")))
    started = asyncio.Event()
    release = asyncio.Event()
    original = h.ops.evict_pod

    async def _hanging_evict(namespace: str, name: str, **kwargs: Any) -> None:
        if name == "b":
            started.set()
            await release.wait()
        await original(namespace, name, **kwargs)

    h.ops.evict_pod = _hanging_evict  # type: ignore[method-assign]  # test stub
    task = asyncio.ensure_future(h.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(outcome == "cancelled" for _, outcome in h.audits)
    assert h.ops.cordoned == [("node-1", True)]  # cordoned once, never undone
