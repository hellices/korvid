"""Node cordon / uncordon / drain keybindings (issue #40).

`c` cordons and `u` uncordons the selected node behind a plain approval
dialog; `shift+d` drains it behind a typed-name approval showing the
PDB-aware impact plan, then evicts pod by pod with live PDB warnings and
mid-drain cancellation (the node stays cordoned).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from textual.css.query import NoMatches
from textual.widgets import Input, Static

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.drain import DrainPlan, DrainTarget
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    RelationKind,
    RelationshipFacts,
    TargetReference,
)
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar
from korvid.ui.workspace_state import PaneState

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_NODES_META = ResourceMeta("Node", "nodes", "", "v1", False, ("no",))
_ALIASES = {"pods": _PODS_META, "nodes": _NODES_META}


def _target(name: str, *, pdb: str | None = None, local: bool = False) -> DrainTarget:
    return DrainTarget(
        namespace="default", name=name, uid=f"uid-{name}", local_storage=local, pdb_blocked=pdb
    )


class NodeRecorder(WriteOps):
    """WriteOps fake recording node-op calls; evictions can fail per pod
    and block on an event so cancellation is observable."""

    def __init__(
        self,
        plan: DrainPlan | None = None,
        evict_errors: dict[str, ApiStatusError] | None = None,
    ) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.plan = plan or DrainPlan(targets=(), skipped_daemonset=(), skipped_mirror=())
        self.evict_errors = evict_errors or {}
        self.evict_started = asyncio.Event()
        self.release_evictions = asyncio.Event()
        self.release_evictions.set()
        self.evicted_names: set[str] = set()

    async def delete_object(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        pass

    async def scale_object(self, meta, namespace, name, replicas, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        pass

    async def rollout_restart(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        pass

    async def replace_object(self, meta, namespace, name, manifest, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        pass

    async def cordon_node(self, name: str, unschedulable: bool, *, uid: str | None = None) -> None:
        self.calls.append(("cordon", name, unschedulable, uid))

    async def evict_pod(self, namespace: str, name: str, *, uid: str | None = None) -> None:
        self.evict_started.set()
        await self.release_evictions.wait()
        error = self.evict_errors.get(name)
        if error is not None:
            raise error
        self.evicted_names.add(name)
        self.calls.append(("evict", namespace, name, uid))

    async def drain_plan(self, node_name: str) -> DrainPlan:
        self.calls.append(("plan", node_name))
        return DrainPlan(
            targets=tuple(t for t in self.plan.targets if t.name not in self.evicted_names),
            skipped_daemonset=self.plan.skipped_daemonset,
            skipped_mirror=self.plan.skipped_mirror,
        )


def make_app(
    recorder: NodeRecorder,
    audit_path: Path,
    *,
    readonly: bool = False,
    check_calls: list[tuple[str, str, str, str | None, str, str]] | None = None,
    extra_nodes: tuple[str, ...] = (),
    relationship_calls: list[tuple[str, str | None]] | None = None,
) -> KorvidApp:
    store = ResourceStore()
    data: dict[str, list[Summary]] = {
        "pods": [
            PodSummary(
                name="web-1",
                namespace="default",
                phase="Running",
                ready="1/1",
                restarts=0,
                node="worker-1",
                uid="pod-uid-1",
            )
        ],
        "nodes": [
            GenericSummary(
                name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1"
            ),
            *(
                GenericSummary(
                    name=extra, namespace="", kind="Node", created="", uid=f"uid-{extra}"
                )
                for extra in extra_nodes
            ),
        ],
    }

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def check_permission(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        if check_calls is not None:
            check_calls.append((verb, resource, sub, ns, group, name))
        return True

    async def list_relationship_objects(
        meta: ResourceMeta, namespace: str | None
    ) -> list[GenericSummary]:
        assert relationship_calls is not None
        relationship_calls.append((meta.plural, namespace))
        if meta.plural == "nodes":
            return [
                GenericSummary(
                    name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1"
                )
            ]
        if meta.plural == "pods":
            return [
                GenericSummary(
                    name="web-1",
                    namespace="default",
                    kind="Pod",
                    created="",
                    uid="pod-uid-1",
                    relationships=RelationshipFacts(
                        references=(
                            ReferenceFact(
                                relation=RelationKind.SCHEDULED_ON,
                                target=TargetReference("", "Node", "", "worker-1", "node-uid-1"),
                                confidence=FactConfidence.OBSERVED,
                                field="spec.nodeName",
                            ),
                        )
                    ),
                )
            ]
        return []

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=readonly),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        write_ops=recorder,
        audit=AuditLog(audit_path),
        check_permission=None if check_calls is None else check_permission,
        list_relationship_objects=list_relationship_objects
        if relationship_calls is not None
        else None,
    )


def _resource_row_count(app: KorvidApp) -> int:
    try:
        return app.query_one(ResourceTable).row_count
    except NoMatches:
        return -1


async def _to_nodes(pilot) -> None:  # type: ignore[no-untyped-def]  # Pilot's app type isn't exposed
    await pilot.press("colon")
    for ch in "nodes":
        await pilot.press(ch)
    await pilot.press("enter")

    def _node_row_rendered() -> bool:
        try:
            table = pilot.app.query_one(ResourceTable)
        except NoMatches:
            return False
        return table.row_count > 0 and str(table.get_row_at(0)[0]) == "worker-1"

    await until(pilot, _node_row_rendered, label="nodes view rendered")


async def _confirm_typed(pilot, name: str) -> None:  # type: ignore[no-untyped-def]  # Pilot's app type isn't exposed
    for ch in name:
        await pilot.press(ch)
    await pilot.press("enter")


async def test_c_cordons_node_after_approval(tmp_path: Path) -> None:
    rec = NodeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="cordon dialog")
        await pilot.press("y")
        await until(pilot, lambda: rec.calls, label="cordon executed")
        assert rec.calls == [("cordon", "worker-1", True, "node-uid-1")]

        def _success_audited() -> bool:
            if not audit_path.exists():
                return False
            lines = audit_path.read_text().splitlines()
            return any('"success"' in ln for ln in lines)

        await until(pilot, _success_audited, label="success audit record")
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert entries[0]["action"] == "cordon"
        assert entries[0]["outcome"] == "intent"
        assert entries[-1]["outcome"] == "success"


async def test_u_uncordons_node_after_approval(tmp_path: Path) -> None:
    rec = NodeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("u")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="uncordon dialog")
        await pilot.press("y")
        await until(pilot, lambda: rec.calls, label="uncordon executed")
        assert rec.calls == [("cordon", "worker-1", False, "node-uid-1")]
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert entries[0]["action"] == "uncordon"


async def test_cordon_does_not_apply_to_pods(tmp_path: Path) -> None:
    rec = NodeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await pilot.press("c")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_readonly_blocks_node_ops(tmp_path: Path) -> None:
    rec = NodeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", readonly=True)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        for key in ("c", "u", "D"):
            notice_count = len(app._notifications)
            await pilot.press(key)

            def _notification_added(start: int = notice_count) -> bool:
                return len(app._notifications) > start

            await until(
                pilot,
                _notification_added,
                label=f"{key} readonly warning shown",
            )
            assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_cordon_permission_precheck_patches_nodes(tmp_path: Path) -> None:
    checks: list[tuple[str, str, str, str | None, str, str]] = []
    rec = NodeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", check_calls=checks)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("c")
        await until(pilot, lambda: checks, label="SSAR pre-check ran")
        assert checks[0] == ("patch", "nodes", "", None, "", "worker-1")


async def test_drain_requires_typed_name_and_shows_impact_plan(tmp_path: Path) -> None:
    plan = DrainPlan(
        targets=(_target("web-1"), _target("cache-1", local=True)),
        skipped_daemonset=("default/ds-1",),
        skipped_mirror=(),
    )
    rec = NodeRecorder(plan=plan)
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        preview = str(app.screen.query_one(".confirm-preview", Static).render())
        assert "Pods to evict (2)" in preview
        assert "default/web-1" in preview
        assert "local storage" in preview
        assert "DaemonSet pods skipped (1)" in preview
        assert "drain impact" in preview
        # y alone must not confirm: the node name has to be typed.
        await pilot.press("y")
        await until(
            pilot,
            lambda: app.screen.query_one("#confirm-name", Input).value == "y",
            label="typed drain input updated",
        )
        assert not any(call[0] == "cordon" for call in rec.calls)
        await pilot.press("backspace")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="drain success audited",
        )
        assert ("cordon", "worker-1", True, "node-uid-1") in rec.calls
        evictions = [call for call in rec.calls if call[0] == "evict"]
        assert evictions == [
            ("evict", "default", "web-1", "uid-web-1"),
            ("evict", "default", "cache-1", "uid-cache-1"),
        ]
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert entries[0]["action"] == "drain"
        assert entries[0]["outcome"] == "intent"
        assert "evicted 2" in entries[-1]["detail"]


async def test_drain_pdb_blocked_eviction_warns_and_continues(tmp_path: Path) -> None:
    plan = DrainPlan(
        targets=(_target("db-1", pdb="db-pdb"), _target("web-1")),
        skipped_daemonset=(),
        skipped_mirror=(),
    )
    rec = NodeRecorder(
        plan=plan,
        evict_errors={
            "db-1": ApiStatusError(
                429, "Cannot evict pod as it would violate the pod's disruption budget."
            )
        },
    )
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "evicted 1" in audit_path.read_text(),
            label="single-eviction drain audited",
        )
        # The blocked eviction did not stop the drain: web-1 was still evicted.
        assert ("evict", "default", "web-1", "uid-web-1") in rec.calls
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert "pdb-blocked 1" in entries[-1]["detail"]


async def test_drain_cancel_mid_drain_leaves_node_cordoned(tmp_path: Path) -> None:
    plan = DrainPlan(
        targets=(_target("web-1"), _target("cache-1")),
        skipped_daemonset=(),
        skipped_mirror=(),
    )
    rec = NodeRecorder(plan=plan)
    rec.release_evictions.clear()  # first eviction blocks until released
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    app._drain.settle_timeout = 0.1  # the held eviction never settles
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(pilot, lambda: rec.evict_started.is_set(), label="first eviction in flight")
        await pilot.press("D")  # pressing drain again cancels the running drain
        await until(
            pilot,
            lambda: audit_path.exists() and "cancelled" in audit_path.read_text(),
            label="drain cancellation audited",
        )
        # The node was cordoned and stays cordoned; no eviction completed.
        assert ("cordon", "worker-1", True, "node-uid-1") in rec.calls
        assert not any(call[0] == "evict" for call in rec.calls)


async def test_drain_with_nothing_to_evict_still_cordons(tmp_path: Path) -> None:
    rec = NodeRecorder()  # empty plan
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        preview = str(app.screen.query_one(".confirm-preview", Static).render())
        assert "No pods to evict" in preview
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="drain success audited",
        )
        assert ("cordon", "worker-1", True, "node-uid-1") in rec.calls


async def test_drain_plan_failure_aborts_before_dialog(tmp_path: Path) -> None:
    class FailingRecorder(NodeRecorder):
        async def drain_plan(self, node_name: str) -> DrainPlan:
            raise ApiStatusError(403, "Forbidden")

    rec = FailingRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(
            pilot,
            lambda: any(
                "could not compute the impact plan" in n.message for n in app._notifications
            ),
            label="drain plan failure shown",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert not any(call[0] == "cordon" for call in rec.calls)


async def test_unsupported_transport_reports_unavailable(tmp_path: Path) -> None:
    class Bare(NodeRecorder):
        async def cordon_node(
            self, name: str, unschedulable: bool, *, uid: str | None = None
        ) -> None:
            raise NotImplementedError("this transport does not support cordon/uncordon")

    rec = Bare()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="cordon dialog")
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "error" in audit_path.read_text(),
            label="cordon failure audited",
        )
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert "error" in entries[-1]["outcome"]


async def test_drain_while_drain_in_progress_cancels_instead_of_stacking(tmp_path: Path) -> None:
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = NodeRecorder(plan=plan)
    rec.release_evictions.clear()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    app._drain.settle_timeout = 0.1  # the held eviction never settles
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(pilot, lambda: rec.evict_started.is_set(), label="first eviction in flight")
        plans_before = len([call for call in rec.calls if call[0] == "plan"])
        await pilot.press("D")  # cancels; must NOT open a second drain dialog
        await until(
            pilot,
            lambda: audit_path.exists() and "cancelled" in audit_path.read_text(),
            label="drain cancellation audited",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert len([call for call in rec.calls if call[0] == "plan"]) == plans_before


async def test_drain_aborts_when_plan_gains_unapproved_pods_after_cordon(tmp_path: Path) -> None:
    """The node is still schedulable while the approval dialog is open; a
    pod that lands in that window was never reviewed. The post-cordon
    re-check must abort (node stays cordoned) instead of silently skipping
    or evicting it."""
    approved = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    grown = DrainPlan(
        targets=(_target("web-1"), _target("sneaky-1")),
        skipped_daemonset=(),
        skipped_mirror=(),
    )

    class GrowingRecorder(NodeRecorder):
        async def drain_plan(self, node_name: str) -> DrainPlan:
            self.calls.append(("plan", node_name))
            plan_calls = sum(1 for call in self.calls if call[0] == "plan")
            return approved if plan_calls == 1 else grown

    rec = GrowingRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "plan changed after cordon" in audit_path.read_text(),
            label="plan-change drain audited",
        )
        # Cordoned, then aborted: nothing was evicted.
        assert ("cordon", "worker-1", True, "node-uid-1") in rec.calls
        assert not any(call[0] == "evict" for call in rec.calls)
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert entries[-1]["outcome"] == "aborted"
        assert "sneaky-1" in entries[-1]["detail"]


async def test_drain_key_on_other_node_does_not_cancel_running_drain(tmp_path: Path) -> None:
    """Cancelling is targeted: pressing the drain key while a *different*
    node is selected must warn instead of silently killing the running
    drain."""
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = NodeRecorder(plan=plan)
    rec.release_evictions.clear()  # keep the drain in flight
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path, extra_nodes=("worker-2",))
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(pilot, lambda: rec.evict_started.is_set(), label="first eviction in flight")
        await pilot.press("down")  # select worker-2
        await pilot.press("D")  # must NOT cancel worker-1's drain
        await until(
            pilot,
            lambda: any(
                "press the drain key on it to cancel" in n.message for n in app._notifications
            ),
            label="wrong-node cancel warning shown",
        )
        assert "cancelled" not in (audit_path.read_text() if audit_path.exists() else "")
        assert app._resource_writes.drain_worker is not None
        assert app._resource_writes.drain_worker.is_running
        rec.release_evictions.set()
        await until(
            pilot,
            lambda: audit_path.exists() and "evicted 1" in audit_path.read_text(),
            label="single-eviction drain audited",
        )


async def test_drain_publishes_progress_on_status_bar(tmp_path: Path) -> None:
    """Issue #40: eviction progress is live, not just start/end toasts."""
    plan = DrainPlan(
        targets=(_target("web-1"), _target("cache-1")),
        skipped_daemonset=(),
        skipped_mirror=(),
    )
    rec = NodeRecorder(plan=plan)
    rec.release_evictions.clear()  # hold the first eviction in flight
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(pilot, lambda: rec.evict_started.is_set(), label="first eviction in flight")

        def _status_text() -> str:
            return str(app.query_one(StatusBar).render())

        await until(
            pilot,
            lambda: "drain worker-1: 0/2" in _status_text(),
            label="drain progress shown",
        )
        rec.release_evictions.set()
        await until(
            pilot,
            lambda: audit_path.exists() and "evicted 2" in audit_path.read_text(),
            label="two-eviction drain audited",
        )
        # Progress indicator is cleared once the drain completes.
        assert "drain worker-1" not in _status_text()


async def test_drain_reports_pods_that_never_finish_terminating(tmp_path: Path) -> None:
    """An accepted eviction only starts graceful deletion; if the pod
    lingers past the bounded wait the drain is audited as partial."""

    class LingeringRecorder(NodeRecorder):
        async def drain_plan(self, node_name: str) -> DrainPlan:
            self.calls.append(("plan", node_name))
            return self.plan  # the evicted pod never leaves the node

    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = LingeringRecorder(plan=plan)
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    app._drain.wait_timeout = 0.3
    app._drain.wait_poll = 0.05
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "not yet terminated" in audit_path.read_text(),
            label="lingering-pod drain audited",
        )
        assert ("evict", "default", "web-1", "uid-web-1") in rec.calls
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert entries[-1]["outcome"].startswith("partial")
        assert "1 evicted pods not yet terminated" in entries[-1]["detail"]


async def test_drain_waits_for_evicted_pods_to_disappear(tmp_path: Path) -> None:
    """The success audit is only written after the accepted evictions'
    pods are gone from the node's pod list."""
    plan = DrainPlan(
        targets=(_target("web-1"), _target("cache-1")),
        skipped_daemonset=(),
        skipped_mirror=(),
    )
    rec = NodeRecorder(plan=plan)
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="drain success audited",
        )
        # The verification poll ran after the evictions: initial preview,
        # post-cordon recheck, then at least one termination-wait poll.
        plan_calls = [call for call in rec.calls if call[0] == "plan"]
        assert len(plan_calls) >= 3
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert entries[-1]["outcome"] == "success"
        assert "not yet terminated" not in entries[-1]["detail"]


async def test_uncordon_is_refused_while_node_is_being_drained(tmp_path: Path) -> None:
    """Uncordoning mid-drain would let new pods schedule behind the
    drain's back - the key is rejected until the drain ends."""
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = NodeRecorder(plan=plan)
    rec.release_evictions.clear()  # hold the eviction so the drain stays active
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(pilot, lambda: rec.evict_started.is_set(), label="eviction in flight")
        await pilot.press("u")
        await until(
            pilot,
            lambda: any(
                "is being drained - cancel the drain first" in n.message for n in app._notifications
            ),
            label="uncordon refusal shown",
        )
        # No uncordon dialog opened and no schedulable write was issued.
        assert not isinstance(app.screen, ConfirmScreen)
        assert not any(call[:3] == ("cordon", "worker-1", False) for call in rec.calls)
        rec.release_evictions.set()
        await until(
            pilot,
            lambda: audit_path.exists() and "evicted 1" in audit_path.read_text(),
            label="single-eviction drain audited",
        )


async def test_throttled_429_eviction_is_retried_not_reported_pdb_blocked(
    tmp_path: Path,
) -> None:
    """A 429 without the PDB denial markers is apiserver throttling
    (API Priority and Fairness): retried with backoff, and never
    misreported as pdb-blocked."""

    class ThrottlingRecorder(NodeRecorder):
        def __init__(self, plan: DrainPlan) -> None:
            super().__init__(plan=plan)
            self.throttles_left = 1

        async def evict_pod(self, namespace: str, name: str, *, uid: str | None = None) -> None:
            if self.throttles_left:
                self.throttles_left -= 1
                raise ApiStatusError(429, "Too Many Requests")
            await super().evict_pod(namespace, name, uid=uid)

    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = ThrottlingRecorder(plan)
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    app._drain.throttle_backoff = 0.01
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "evicted 1" in audit_path.read_text(),
            label="single-eviction drain audited",
        )
        assert ("evict", "default", "web-1", "uid-web-1") in rec.calls
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert entries[-1]["outcome"] == "success"
        assert "pdb-blocked 0" in entries[-1]["detail"]


async def test_persistently_throttled_eviction_counts_as_failed(tmp_path: Path) -> None:
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = NodeRecorder(
        plan=plan,
        evict_errors={"web-1": ApiStatusError(429, "Too Many Requests")},
    )
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    app._drain.throttle_retries = 1
    app._drain.throttle_backoff = 0.01
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "failed 1" in audit_path.read_text(),
            label="failed-eviction drain audited",
        )
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert entries[-1]["outcome"].startswith("partial")
        assert "pdb-blocked 0" in entries[-1]["detail"]


async def test_cancelled_in_flight_eviction_that_lands_is_counted(tmp_path: Path) -> None:
    """Cancellation can arrive after the eviction POST reached the
    apiserver: the drain lets the in-flight request settle (bounded) so
    the cancellation audit reports the eviction that actually landed."""
    plan = DrainPlan(
        targets=(_target("web-1"), _target("cache-1")),
        skipped_daemonset=(),
        skipped_mirror=(),
    )
    rec = NodeRecorder(plan=plan)
    rec.release_evictions.clear()  # hold the first eviction in flight
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(pilot, lambda: rec.evict_started.is_set(), label="first eviction in flight")
        await pilot.press("D")  # cancel while the POST is in flight
        await pilot.pause(0.05)
        rec.release_evictions.set()  # ... and the in-flight eviction lands
        await until(
            pilot,
            lambda: audit_path.exists() and "cancelled" in audit_path.read_text(),
            label="drain cancellation audited",
        )
        # The eviction that was in flight when cancel arrived is counted.
        assert ("evict", "default", "web-1", "uid-web-1") in rec.calls
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert "evicted 1 of 2" in entries[-1]["detail"]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("c", "the Node is marked unschedulable for ordinary workload placement"),
        ("u", "future scheduling to the Node is permitted"),
    ],
)
async def test_cordon_toggle_shows_local_impact_without_graph_load(
    tmp_path: Path, key: str, expected: str
) -> None:
    calls: list[tuple[str, str | None]] = []
    rec = NodeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        rec,
        audit_path,
        relationship_calls=calls,
    )
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press(key)
        await until(
            pilot,
            lambda: (
                isinstance(app.screen, ConfirmScreen) and bool(app.screen.query(".confirm-impact"))
            ),
            label="node scheduling impact rendered",
        )
        text = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "Node maintenance impact (advisory):" in text
        assert expected in text
        assert "graph-derived impact" not in text
        assert calls == []
        await pilot.press("n")
        await until(
            pilot,
            lambda: not isinstance(app.screen, ConfirmScreen),
            label="node scheduling confirmation dismissed",
        )
        assert rec.calls == []
        assert not audit_path.exists()


class _StoreReplacingRecorder(NodeRecorder):
    """NodeRecorder that mutates the node UID from within preview_cordon.

    Simulates a replacement arriving during the dry-run gap without relying
    on asyncio.Event blocking, which Textual's accelerated test clock can
    cancel prematurely via asyncio.wait_for's internal timeout.
    """

    def __init__(self) -> None:
        super().__init__()
        self._store: ResourceStore | None = None
        self._scope: str = "default"

    def attach_store(self, store: "ResourceStore", scope: str) -> None:
        self._store = store
        self._scope = scope

    async def preview_cordon(
        self, name: str, unschedulable: bool, *, uid: str | None = None
    ) -> list[str] | None:
        if self._store is not None:
            replacement = GenericSummary(
                name=name, namespace="", kind="Node", created="", uid="node-uid-REPLACED"
            )
            self._store.apply_event("nodes", self._scope, "MODIFIED", replacement)
        return None


async def test_cordon_uid_drift_during_dry_run_blocks_write(tmp_path: Path) -> None:
    rec = _StoreReplacingRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    rec.attach_store(app.store, app.config.namespace or "default")
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        # Attach the live scope once the nodes view is active.
        rec.attach_store(app.store, app.current_scope)
        await pilot.press("c")
        await until(
            pilot,
            lambda: any(
                "selection changed during the dry-run preview" in n.message
                for n in app._notifications
            ),
            label="cordon dry-run UID drift cancellation",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []
        assert not audit_path.exists()


async def test_cordon_uid_drift_during_confirmation_blocks_write(tmp_path: Path) -> None:
    rec = NodeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press("c")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="cordon dialog open",
        )
        replacement = GenericSummary(
            name="worker-1", namespace="", kind="Node", created="", uid="node-uid-REPLACED"
        )
        app.store.apply_event("nodes", app.current_scope, "MODIFIED", replacement)
        await pilot.press("y")
        await until(
            pilot,
            lambda: any(
                "selection changed during the confirmation dialog" in n.message
                for n in app._notifications
            ),
            label="cordon confirmation UID drift cancellation",
        )
        assert rec.calls == []
        assert not audit_path.exists()


async def test_drain_shows_plan_graph_and_local_sections(tmp_path: Path) -> None:
    plan = DrainPlan(
        targets=(_target("web-1"),),
        skipped_daemonset=("default/agent",),
        skipped_mirror=(),
    )
    calls: list[tuple[str, str | None]] = []
    app = make_app(
        NodeRecorder(plan),
        tmp_path / "audit.jsonl",
        relationship_calls=calls,
    )
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(
            pilot,
            lambda: (
                isinstance(app.screen, ConfirmScreen) and bool(app.screen.query(".confirm-impact"))
            ),
            label="drain impact rendered",
        )
        preview = str(app.screen.query_one(".confirm-preview", Static).render())
        impact = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "web-1" in preview
        assert "agent" in preview
        assert "graph-derived impact (advisory):" in impact
        assert "Pod/default/web-1" in impact
        assert "Node maintenance impact (advisory):" in impact
        assert "the drain impact plan defines exact eviction targets" in impact
        assert {namespace for _, namespace in calls} == {None}
        await pilot.press("escape")


async def test_drain_graph_failure_keeps_plan_and_notes(tmp_path: Path) -> None:
    plan = DrainPlan(
        targets=(_target("web-1"),),
        skipped_daemonset=(),
        skipped_mirror=(),
    )
    calls: list[tuple[str, str | None]] = []

    async def _bad_relationship(meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        calls.append((meta.plural, namespace))
        raise RuntimeError("secret response body")

    store = ResourceStore()
    data: dict[str, list[Summary]] = {
        "pods": [
            PodSummary(
                name="web-1",
                namespace="default",
                phase="Running",
                ready="1/1",
                restarts=0,
                node="worker-1",
                uid="pod-uid-1",
            )
        ],
        "nodes": [
            GenericSummary(
                name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1"
            ),
        ],
    }

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    rec = NodeRecorder(plan)
    audit_path = tmp_path / "audit.jsonl"
    app = KorvidApp(
        config=KorvidConfig(namespace="default", readonly=False),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        write_ops=rec,
        audit=AuditLog(audit_path),
        check_permission=None,
        list_relationship_objects=_bad_relationship,
    )
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(
            pilot,
            lambda: (
                isinstance(app.screen, ConfirmScreen) and bool(app.screen.query(".confirm-impact"))
            ),
            label="drain impact rendered after graph failure",
        )
        preview = str(app.screen.query_one(".confirm-preview", Static).render())
        impact = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "web-1" in preview
        assert "impact unavailable; approval remains available" in impact
        assert "Node maintenance impact (advisory):" in impact
        assert "the drain impact plan defines exact eviction targets" in impact
        assert "secret response body" not in impact
        assert "secret response body" not in preview
        # Declining must not create a write or audit entry
        await pilot.press("escape")
        await until(
            pilot,
            lambda: not isinstance(app.screen, ConfirmScreen),
            label="drain confirmation dismissed",
        )
        assert not any(call[0] == "cordon" for call in rec.calls)
        assert not any(call[0] == "evict" for call in rec.calls)
        assert not audit_path.exists()


async def test_drain_plan_failure_never_calls_graph(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []

    class FailingWithGraph(NodeRecorder):
        async def drain_plan(self, node_name: str) -> DrainPlan:
            raise ApiStatusError(403, "Forbidden")

    rec = FailingWithGraph()
    app = make_app(rec, tmp_path / "audit.jsonl", relationship_calls=calls)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _resource_row_count(app) > 0, label="initial row rendered")
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(
            pilot,
            lambda: any(
                "could not compute the impact plan" in n.message for n in app._notifications
            ),
            label="drain plan failure shown",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert calls == []
        assert not any(call[0] == "cordon" for call in rec.calls)
        assert (
            not (tmp_path / "audit.jsonl").exists()
            or "success" not in (tmp_path / "audit.jsonl").read_text()
        )


# ─────────────────────────────────────────────────────────────────────────────
# Injection-based fakes for drain identity/cancellation race tests (Task 5)
#
# Unlike asyncio.Event blocking (which deadlocks with Textual's pilot because
# _wait_for_screen cannot complete while the action coroutine is suspended),
# these fakes mutate app state FROM WITHIN the awaited call and return
# immediately — the same pattern as _StoreReplacingRecorder above.
# ─────────────────────────────────────────────────────────────────────────────


class _CtxSwitchDuringPlanRecorder(NodeRecorder):
    """Increments the context epoch from within drain_plan, simulating a context switch
    that happens while the API call for the plan is in flight."""

    def __init__(self, plan: DrainPlan | None = None) -> None:
        super().__init__(plan=plan)
        self._app: KorvidApp | None = None

    def attach(self, app: KorvidApp) -> None:
        self._app = app

    async def drain_plan(self, node_name: str) -> DrainPlan:
        self.calls.append(("plan", node_name))
        if self._app is not None:
            self._app._ctx._epoch += 1
        return DrainPlan(
            targets=tuple(t for t in self.plan.targets if t.name not in self.evicted_names),
            skipped_daemonset=self.plan.skipped_daemonset,
            skipped_mirror=self.plan.skipped_mirror,
        )


class _FocusSwitchDuringGraphLister:
    """list_relationship_objects fake that adds a second pane and switches focus
    from within the call, simulating a workspace split that happens mid-load."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._app: KorvidApp | None = None

    def attach(self, app: KorvidApp) -> None:
        self._app = app

    async def __call__(self, meta: ResourceMeta, namespace: str | None) -> list[Summary]:
        self.calls.append((meta.plural, namespace))
        if self._app is not None and meta.plural == "nodes":
            # Replace pane-0 with a fresh object (same kind/scope/table_id but
            # different identity) so origin.pane is not self._app._pane — the
            # same effect as the user focusing a different pane, but without
            # a second ResourceTable widget in the DOM.
            current = self._app._workspace.panes[0]
            replacement = PaneState(current.kind, current.scope, current.table_id)
            self._app._workspace._panes[0] = replacement
        if meta.plural == "nodes":
            return [
                GenericSummary(
                    name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1"
                )
            ]
        if meta.plural == "pods":
            return [
                PodSummary(
                    name="web-1",
                    namespace="default",
                    phase="Running",
                    ready="1/1",
                    restarts=0,
                    node="worker-1",
                    uid="pod-uid-1",
                )
            ]
        return []


class _ScopeChangeDuringGraphLister:
    """list_relationship_objects fake that mutates the pane scope from within
    the call, simulating a :ns re-scope that arrives while the graph LIST is
    in flight."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._app: KorvidApp | None = None

    def attach(self, app: KorvidApp) -> None:
        self._app = app

    async def __call__(self, meta: ResourceMeta, namespace: str | None) -> list[Summary]:
        self.calls.append((meta.plural, namespace))
        if self._app is not None and meta.plural == "nodes":
            self._app._workspace.panes[0].scope = "other-namespace"
        if meta.plural == "nodes":
            return [
                GenericSummary(
                    name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1"
                )
            ]
        if meta.plural == "pods":
            return [
                PodSummary(
                    name="web-1",
                    namespace="default",
                    phase="Running",
                    ready="1/1",
                    restarts=0,
                    node="worker-1",
                    uid="pod-uid-1",
                )
            ]
        return []


class _UidChangeDuringGraphLister:
    """list_relationship_objects fake that replaces the node UID in the store
    from within the call, simulating a Node delete-recreate that completes while
    the graph LIST is in flight."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._store: ResourceStore | None = None
        self._scope: str = "default"

    def attach(self, store: ResourceStore, scope: str) -> None:
        self._store = store
        self._scope = scope

    async def __call__(self, meta: ResourceMeta, namespace: str | None) -> list[Summary]:
        self.calls.append((meta.plural, namespace))
        if self._store is not None and meta.plural == "nodes":
            replacement = GenericSummary(
                name="worker-1", namespace="", kind="Node", created="", uid="node-uid-REPLACED"
            )
            self._store.apply_event("nodes", self._scope, "MODIFIED", replacement)
        if meta.plural == "nodes":
            return [
                GenericSummary(
                    name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1"
                )
            ]
        if meta.plural == "pods":
            return [
                PodSummary(
                    name="web-1",
                    namespace="default",
                    phase="Running",
                    ready="1/1",
                    restarts=0,
                    node="worker-1",
                    uid="pod-uid-1",
                )
            ]
        return []


class _CtxSwitchDuringGraphLister:
    """list_relationship_objects fake that increments the context epoch from within the
    call, simulating a context switch (`:ctx`) that completes while the graph
    LIST is in flight.  The graph load itself returns normally; the identity
    check immediately after it detects the stale epoch and cancels."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._app: KorvidApp | None = None

    def attach(self, app: KorvidApp) -> None:
        self._app = app

    async def __call__(self, meta: ResourceMeta, namespace: str | None) -> list[Summary]:
        self.calls.append((meta.plural, namespace))
        if self._app is not None and meta.plural == "nodes":
            self._app._ctx._epoch += 1
        if meta.plural == "nodes":
            return [
                GenericSummary(
                    name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1"
                )
            ]
        if meta.plural == "pods":
            return [
                PodSummary(
                    name="web-1",
                    namespace="default",
                    phase="Running",
                    ready="1/1",
                    restarts=0,
                    node="worker-1",
                    uid="pod-uid-1",
                )
            ]
        return []


class _BlockingGraphLister:
    """Relationship lister that exposes cancellation while its first LIST is blocked."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self, meta: ResourceMeta, namespace: str | None) -> list[Summary]:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return []


def _make_app_with_custom_lister(
    recorder: NodeRecorder,
    audit_path: Path,
    lister: object,
) -> KorvidApp:
    """Minimal KorvidApp fixture wired with an arbitrary list_relationship_objects."""
    store = ResourceStore()
    data: dict[str, list[Summary]] = {
        "pods": [
            PodSummary(
                name="web-1",
                namespace="default",
                phase="Running",
                ready="1/1",
                restarts=0,
                node="worker-1",
                uid="pod-uid-1",
            )
        ],
        "nodes": [
            GenericSummary(
                name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1"
            ),
        ],
    }

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=False),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        write_ops=recorder,
        audit=AuditLog(audit_path),
        check_permission=None,
        # Race fakes return the broader Summary union used by relationship loading.
        list_relationship_objects=lister,  # type: ignore[arg-type]
    )


async def test_drain_context_switch_during_plan_refuses_confirmation(
    tmp_path: Path,
) -> None:
    """Context switch that arrives while drain_plan is awaited cancels before dialog."""
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = _CtxSwitchDuringPlanRecorder(plan=plan)
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)  # no relationship lister needed
    rec.attach(app)
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(
            pilot,
            lambda: any("cancelled" in n.message for n in app._notifications),
            label="drain cancelled notification",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert app._writes.active_writes() == 0
        assert not any(call[0] == "cordon" for call in rec.calls)
        assert not any(call[0] == "evict" for call in rec.calls)
        assert not audit_path.exists()


async def test_drain_focus_change_during_graph_load_refuses_confirmation(
    tmp_path: Path,
) -> None:
    """Switching pane focus during graph load cancels before the dialog."""
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = NodeRecorder(plan=plan)
    audit_path = tmp_path / "audit.jsonl"
    lister = _FocusSwitchDuringGraphLister()
    app = _make_app_with_custom_lister(rec, audit_path, lister)
    lister.attach(app)
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(
            pilot,
            lambda: any("cancelled" in n.message for n in app._notifications),
            label="drain cancelled notification",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert app._writes.active_writes() == 0
        assert not any(call[0] == "cordon" for call in rec.calls)
        assert not any(call[0] == "evict" for call in rec.calls)
        assert not audit_path.exists()


async def test_drain_scope_change_during_graph_load_refuses_confirmation(
    tmp_path: Path,
) -> None:
    """Re-scoping the originating pane during graph load cancels before the dialog."""
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = NodeRecorder(plan=plan)
    audit_path = tmp_path / "audit.jsonl"
    lister = _ScopeChangeDuringGraphLister()
    app = _make_app_with_custom_lister(rec, audit_path, lister)
    lister.attach(app)
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(
            pilot,
            lambda: any("cancelled" in n.message for n in app._notifications),
            label="drain cancelled notification",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert app._writes.active_writes() == 0
        assert not any(call[0] == "cordon" for call in rec.calls)
        assert not any(call[0] == "evict" for call in rec.calls)
        assert not audit_path.exists()


async def test_drain_uid_change_during_graph_load_refuses_confirmation(
    tmp_path: Path,
) -> None:
    """Node UID replaced while the graph LIST is in flight cancels before dialog."""
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = NodeRecorder(plan=plan)
    audit_path = tmp_path / "audit.jsonl"
    lister = _UidChangeDuringGraphLister()
    app = _make_app_with_custom_lister(rec, audit_path, lister)
    lister.attach(app.store, app.config.namespace or "default")
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        lister.attach(app.store, app.current_scope)
        await pilot.press("D")
        await until(
            pilot,
            lambda: any("cancelled" in n.message for n in app._notifications),
            label="drain cancelled notification",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert app._writes.active_writes() == 0
        assert not any(call[0] == "cordon" for call in rec.calls)
        assert not any(call[0] == "evict" for call in rec.calls)
        assert not audit_path.exists()


async def test_drain_uid_change_in_confirmation_dispatches_nothing(
    tmp_path: Path,
) -> None:
    """UID replaced while the drain confirmation dialog is open: no write, no audit."""
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    calls: list[tuple[str, str | None]] = []
    rec = NodeRecorder(plan=plan)
    rec.release_evictions.clear()  # hold any eviction so nothing can run
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path, relationship_calls=calls)
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        # Replace the node UID while the dialog is open.
        replacement = GenericSummary(
            name="worker-1", namespace="", kind="Node", created="", uid="node-uid-REPLACED"
        )
        app.store.apply_event("nodes", app.current_scope, "MODIFIED", replacement)
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: any("cancelled" in n.message for n in app._notifications),
            label="drain cancelled after uid drift in confirmation",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert app._writes.active_writes() == 0
        assert not any(call[0] == "cordon" for call in rec.calls)
        assert not any(call[0] == "evict" for call in rec.calls)
        assert not audit_path.exists()


async def test_drain_context_switch_during_graph_load_refuses_confirmation(
    tmp_path: Path,
) -> None:
    """Context switch occurring during graph load cancels the drain before dialog."""
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = NodeRecorder(plan=plan)
    audit_path = tmp_path / "audit.jsonl"
    lister = _CtxSwitchDuringGraphLister()
    app = _make_app_with_custom_lister(rec, audit_path, lister)
    lister.attach(app)
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(
            pilot,
            lambda: any("cancelled" in n.message for n in app._notifications),
            label="drain cancelled after context switch during graph",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert app._writes.active_writes() == 0
        assert not any(call[0] == "cordon" for call in rec.calls)
        assert not any(call[0] == "evict" for call in rec.calls)
        assert not audit_path.exists()


async def test_cancelled_drain_graph_load_dispatches_nothing(tmp_path: Path) -> None:
    """Task cancellation propagates from a blocked graph LIST without side effects."""
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = NodeRecorder(plan=plan)
    audit_path = tmp_path / "audit.jsonl"
    lister = _BlockingGraphLister()
    app = _make_app_with_custom_lister(rec, audit_path, lister)
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        task = asyncio.create_task(app.action_drain_node())
        try:
            await until(pilot, lister.entered.is_set, label="drain graph LIST started")
            task.cancel()
            with pytest.raises(asyncio.CancelledError, match=r"^$"):
                await task
            assert lister.cancelled.is_set()
            assert not isinstance(app.screen, ConfirmScreen)
            assert app._writes.active_writes() == 0
            assert not any(call[0] == "cordon" for call in rec.calls)
            assert not any(call[0] == "evict" for call in rec.calls)
            assert not audit_path.exists()
        finally:
            lister.release.set()
