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

from textual.css.query import NoMatches
from textual.widgets import Static

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.drain import DrainPlan, DrainTarget
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar

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

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=readonly),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        write_ops=recorder,
        audit=AuditLog(audit_path),
        check_permission=None if check_calls is None else check_permission,
    )


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
        await pilot.pause(0.1)
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
        await pilot.pause(0.1)
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
        await pilot.pause(0.1)
        await pilot.press("c")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_readonly_blocks_node_ops(tmp_path: Path) -> None:
    rec = NodeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", readonly=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        for key in ("c", "u", "D"):
            await pilot.press(key)
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_cordon_permission_precheck_patches_nodes(tmp_path: Path) -> None:
    checks: list[tuple[str, str, str, str | None, str, str]] = []
    rec = NodeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", check_calls=checks)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
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
        await pilot.pause(0.1)
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
        await pilot.pause(0.2)
        assert not any(call[0] == "cordon" for call in rec.calls)
        await pilot.press("backspace")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="drain finished",
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
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "evicted 1" in audit_path.read_text(),
            label="drain finished",
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
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(pilot, lambda: rec.evict_started.is_set(), label="first eviction in flight")
        await pilot.press("D")  # pressing drain again cancels the running drain
        await until(
            pilot,
            lambda: audit_path.exists() and "cancelled" in audit_path.read_text(),
            label="drain cancelled",
        )
        # The node was cordoned and stays cordoned; no eviction completed.
        assert ("cordon", "worker-1", True, "node-uid-1") in rec.calls
        assert not any(call[0] == "evict" for call in rec.calls)


async def test_drain_with_nothing_to_evict_still_cordons(tmp_path: Path) -> None:
    rec = NodeRecorder()  # empty plan
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        preview = str(app.screen.query_one(".confirm-preview", Static).render())
        assert "No pods to evict" in preview
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="drain finished",
        )
        assert ("cordon", "worker-1", True, "node-uid-1") in rec.calls


async def test_drain_plan_failure_aborts_before_dialog(tmp_path: Path) -> None:
    class FailingRecorder(NodeRecorder):
        async def drain_plan(self, node_name: str) -> DrainPlan:
            raise ApiStatusError(403, "Forbidden")

    rec = FailingRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await pilot.pause(0.2)
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
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="cordon dialog")
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "error" in audit_path.read_text(),
            label="failure audited",
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
        await pilot.pause(0.1)
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
            label="drain cancelled",
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
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "plan changed after cordon" in audit_path.read_text(),
            label="drain aborted on plan change",
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
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(pilot, lambda: rec.evict_started.is_set(), label="first eviction in flight")
        await pilot.press("down")  # select worker-2
        await pilot.press("D")  # must NOT cancel worker-1's drain
        await pilot.pause(0.2)
        assert "cancelled" not in (audit_path.read_text() if audit_path.exists() else "")
        assert app._drain_worker is not None
        assert app._drain_worker.is_running
        rec.release_evictions.set()
        await until(
            pilot,
            lambda: audit_path.exists() and "evicted 1" in audit_path.read_text(),
            label="drain finished",
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
        await pilot.pause(0.1)
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
            label="progress on status bar",
        )
        rec.release_evictions.set()
        await until(
            pilot,
            lambda: audit_path.exists() and "evicted 2" in audit_path.read_text(),
            label="drain finished",
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
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "not yet terminated" in audit_path.read_text(),
            label="drain finished with lingering pod",
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
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="drain finished",
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
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(pilot, lambda: rec.evict_started.is_set(), label="eviction in flight")
        await pilot.press("u")
        await pilot.pause(0.2)
        # No uncordon dialog opened and no schedulable write was issued.
        assert not isinstance(app.screen, ConfirmScreen)
        assert not any(call[:3] == ("cordon", "worker-1", False) for call in rec.calls)
        rec.release_evictions.set()
        await until(
            pilot,
            lambda: audit_path.exists() and "evicted 1" in audit_path.read_text(),
            label="drain finished",
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
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "evicted 1" in audit_path.read_text(),
            label="drain finished",
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
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await _confirm_typed(pilot, "worker-1")
        await until(
            pilot,
            lambda: audit_path.exists() and "failed 1" in audit_path.read_text(),
            label="drain finished",
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
        await pilot.pause(0.1)
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
            label="drain cancelled",
        )
        # The eviction that was in flight when cancel arrived is counted.
        assert ("evict", "default", "web-1", "uid-web-1") in rec.calls
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert "evicted 1 of 2" in entries[-1]["detail"]
