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
        self.calls.append(("evict", namespace, name, uid))

    async def drain_plan(self, node_name: str) -> DrainPlan:
        self.calls.append(("plan", node_name))
        return self.plan


def make_app(
    recorder: NodeRecorder,
    audit_path: Path,
    *,
    readonly: bool = False,
    check_calls: list[tuple[str, str, str, str | None, str, str]] | None = None,
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
            GenericSummary(name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1")
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
    await pilot.pause(0.1)


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
        evict_errors={"db-1": ApiStatusError(429, "Cannot evict pod: PDB violated")},
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
