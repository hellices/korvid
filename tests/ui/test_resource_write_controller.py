"""Direct tests for `ResourceWriteController` — the user-triggered resource
and node write workflows (issue #187 / Deep Task 5).

The controller composes the workflows a keybinding raises: delete, rollout
restart, the editor round-trip, scale, in-place pod resize, cordon/uncordon
and drain. It owns no security decision of its own — every approval,
revalidation, reservation, audit record and mutation goes through the single
`WriteCoordinator`, which is constructed here for real (over fake Textual and
view surfaces) so "the workflow cannot bypass the perimeter" is a fact these
tests observe rather than a claim.

`SpyCoordinator` records which perimeter entry point each workflow used and
counts operation-factory construction, without changing the ordering the
coordinator enforces.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from korvid.core.audit import AuditLog
from korvid.core.relationships import GraphResource
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.drain import DrainPlan, DrainTarget
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary
from korvid.k8s.writes import WriteOps
from korvid.ui.drain import DrainController
from korvid.ui.resource_write_controller import ResourceWriteController
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt
from korvid.ui.widgets.resize_prompt import ResizePrompt
from korvid.ui.workspace_state import PaneState
from korvid.ui.write_coordinator import WriteCoordinator

from .test_write_coordinator import BrokenAudit, FakeContext, FakeTimeline, FakeUi, FakeView

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_NODES_META = ResourceMeta("Node", "nodes", "", "v1", False, ("no",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_SUBS_META = ResourceMeta("Subscription", "subscriptions", "operators.coreos.com", "v1alpha1", True)
_HELM_META = ResourceMeta("HelmRelease", "helmreleases", "", "v1", True, synthetic=True)

_ALIASES = {
    "pods": _PODS_META,
    "nodes": _NODES_META,
    "deployments": _DEPLOY_META,
    "subscriptions": _SUBS_META,
    "helmreleases": _HELM_META,
}

_POD_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": "web-1",
        "namespace": "default",
        "uid": "uid-1",
        "resourceVersion": "42",
        "managedFields": [{"manager": "kubectl"}],
    },
    "spec": {
        "containers": [
            {"name": "app", "image": "nginx:1", "resources": {"requests": {"cpu": "100m"}}}
        ]
    },
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeWorker:
    """The `is_running` / `cancel()` half of a Textual Worker, over a task."""

    def __init__(self, task: asyncio.Task[Any]) -> None:
        self.task = task

    @property
    def is_running(self) -> bool:
        return not self.task.done()

    def cancel(self) -> None:
        self.task.cancel()


class WorkerUi(FakeUi):
    """`FakeUi` whose `run_worker` hands back a cancellable worker handle.

    The drain flow keeps the handle so pressing the key again can cancel it,
    and asks `is_running` before it does — an `asyncio.Task` alone answers
    neither question the way a Textual `Worker` does.
    """

    def run_worker(
        self,
        work: Any,
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
        exit_on_error: bool = True,
        thread: bool = False,
    ) -> Any:
        task = asyncio.ensure_future(work)
        self.workers.append(task)
        return FakeWorker(task)

    def suspend(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


class OpsRecorder(WriteOps):
    """WriteOps fake: records awaited mutations and scripted previews."""

    def __init__(
        self,
        *,
        plan: DrainPlan | None = None,
        error: BaseException | None = None,
        evict_error: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.previews: list[str] = []
        self.plan = plan or DrainPlan(targets=(), skipped_daemonset=(), skipped_mirror=())
        self.error = error
        self.evict_error = evict_error
        self.evict_gate: asyncio.Event | None = None
        self.cordon_gate: asyncio.Event | None = None
        self.observed_active_writes: list[int] = []
        self.observe: Callable[[], int] | None = None

    def _record(self, call: tuple[Any, ...]) -> None:
        self.calls.append(call)
        if self.observe is not None:
            self.observed_active_writes.append(self.observe())

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self._record(("delete", meta.plural, namespace, name, uid))
        if self.error is not None:
            raise self.error

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        self._record(("scale", meta.plural, namespace, name, replicas, uid))

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self._record(("restart", meta.plural, namespace, name, uid))

    async def rollout_restart_with_stamp(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> None:
        self._record(("restart", meta.plural, namespace, name, uid, restarted_at))

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        self._record(("replace", meta.plural, namespace, name, manifest, uid))

    async def create_object(
        self, meta: ResourceMeta, namespace: str | None, manifest: dict[str, Any]
    ) -> None:
        self._record(("create", meta.plural, namespace, manifest))

    async def resize_pod(
        self,
        namespace: str,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]],
        *,
        uid: str | None = None,
    ) -> None:
        self._record(("resize", namespace, name, resources, uid))

    async def cordon_node(self, name: str, unschedulable: bool, *, uid: str | None = None) -> None:
        if self.cordon_gate is not None:
            await self.cordon_gate.wait()
        self._record(("cordon", name, unschedulable, uid))
        if self.error is not None:
            raise self.error

    async def evict_pod(self, namespace: str, name: str, *, uid: str | None = None) -> None:
        if self.evict_gate is not None:
            await self.evict_gate.wait()
        self._record(("evict", namespace, name, uid))
        if self.evict_error is not None:
            raise self.evict_error

    async def drain_plan(self, node_name: str) -> DrainPlan:
        return self.plan

    async def pods_on_node(self, node_name: str) -> tuple[str, ...]:
        return ()

    async def preview_scale(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> list[str] | None:
        self.previews.append("scale")
        return ["+ replicas: 5"]

    async def preview_rollout_restart(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> list[str] | None:
        self.previews.append("rollout_restart")
        return ["+ restartedAt"]

    async def preview_delete(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> list[str] | None:
        self.previews.append("delete")
        return ["- pods/web-1"]

    async def preview_resize(
        self,
        namespace: str,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]],
        *,
        uid: str | None = None,
    ) -> list[str] | None:
        self.previews.append("resize")
        return ["+ cpu: 200m"]

    async def preview_cordon(
        self, name: str, unschedulable: bool, *, uid: str | None = None
    ) -> list[str] | None:
        self.previews.append("cordon")
        return ["+ unschedulable"]


class FakeOperators:
    """The two OLM entry points the delete workflow may redirect into."""

    def __init__(self, *, redirect: bool = False) -> None:
        self.uninstalls: list[tuple[str, str | None, str, str | None]] = []
        self.redirects: list[str] = []
        self.redirect = redirect

    async def uninstall(
        self,
        sub_meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        *,
        fetch_kind: str,
        ctx: tuple[ResourceMeta, str | None, str],
    ) -> None:
        self.uninstalls.append((sub_meta.plural, ns, name, uid))

    async def csv_uninstall_redirect(
        self, csv_meta: ResourceMeta, ns: str | None, name: str
    ) -> bool:
        self.redirects.append(name)
        return self.redirect


class SpyCoordinator(WriteCoordinator):
    """Records the perimeter entry points a workflow used.

    It counts *operation-factory construction* too: a declined dialog must
    never build the mutation, and the only way to see that from outside is
    to wrap the factory the workflow handed in.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.confirms: list[dict[str, Any]] = []
        self.runs: list[str] = []
        self.factory_calls: list[str] = []

    async def confirm(
        self,
        title: str,
        operation: str,
        *,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        detail: str = "",
        require_name: str | None = None,
        preview: list[str] | None = None,
        preview_title: str = "server dry-run preview:",
        managed_note: str | None = None,
        impact_lines: tuple[str, ...] | None = None,
        approval_guard: Callable[[], bool] | None = None,
        precondition: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.confirms.append(
            {
                "title": title,
                "operation": operation,
                "action": action,
                "meta": meta,
                "namespace": namespace,
                "name": name,
                "detail": detail,
                "require_name": require_name,
                "preview": preview,
                "managed_note": managed_note,
                "impact_lines": impact_lines,
                "guarded": approval_guard is not None,
            }
        )

        def counted() -> Awaitable[None]:
            self.factory_calls.append(action)
            return op_factory()

        await super().confirm(
            title,
            operation,
            action=action,
            meta=meta,
            namespace=namespace,
            name=name,
            op_factory=counted,
            detail=detail,
            require_name=require_name,
            preview=preview,
            preview_title=preview_title,
            managed_note=managed_note,
            impact_lines=impact_lines,
            approval_guard=approval_guard,
            precondition=precondition,
        )

    def run(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        detail: str = "",
        *,
        precondition: Callable[[], Awaitable[bool]] | None = None,
    ) -> Any:
        self.runs.append(action)
        return super().run(
            action,
            meta,
            namespace,
            name,
            op_factory,
            detail,
            precondition=precondition,
        )


class Env:
    """A controller plus every fake it was built from."""

    def __init__(
        self,
        *,
        tmp_path: Path,
        view: FakeView,
        ops: OpsRecorder | None,
        audit: str = "working",
        manifests: dict[str, dict[str, Any]] | None = None,
        edit_text: Callable[[str], Awaitable[str | None]] | None = None,
        pod_resize_supported: bool = True,
        operators: FakeOperators | None = None,
        loader: Any = None,
    ) -> None:
        self.ui = WorkerUi()
        self.view = view
        self.context = FakeContext()
        self.timeline = FakeTimeline()
        self.audit_path = tmp_path / "audit.jsonl"
        self.audit: AuditLog | None
        if audit == "working":
            self.audit = AuditLog(self.audit_path, context="test")
        elif audit == "broken":
            self.audit = BrokenAudit(self.audit_path, context="test")
        else:
            self.audit = None
        self.ops = ops
        self.pane = PaneState(view.kind, view.scope)
        self.manifests = manifests if manifests is not None else {}
        self.manifest_calls: list[tuple[str, str | None, str]] = []
        self.operators = operators if operators is not None else FakeOperators()
        self.helm_uninstalls = 0
        self.notes: dict[str, str | None] = {}
        self.coordinator = SpyCoordinator(
            ui=self.ui,
            view=view,
            context=self.context,
            audit=lambda: self.audit,
            timeline=self.timeline,
            check_permission=lambda: None,
            relationship_loader=lambda: loader,
            focused_pane=lambda: self.pane,
            canonical_meta_kind=lambda meta: meta.plural,
        )
        self.drain = DrainController(
            notify=self.ui.notify,
            audit_write=self.coordinator.audit_write,
            set_progress=self._set_progress,
        )
        self.drain.wait_timeout = 0.2
        self.drain.wait_poll = 0.01
        self.drain.settle_timeout = 0.1
        self.progress: list[str] = []
        self.controller = ResourceWriteController(
            writes=self.coordinator,
            view=view,
            ui=self.ui,
            drain=self.drain,
            write_ops=lambda: self.ops,
            get_manifest=lambda: self._get_manifest,
            edit_text=lambda: edit_text,
            managed_note=self._managed_note,
            managed_note_from=self._managed_note_from,
            pod_resize_supported=lambda: pod_resize_supported,
            helm_uninstall=self._helm_uninstall,
            operators=self.operators,
        )
        if self.ops is not None:
            self.ops.observe = self.coordinator.active_writes

    def approve(self) -> None:
        """Answer the open dialog with `y`.

        A flow whose `confirm` carried an `approval_guard` defers its launch
        by one loop iteration (Textual pops the dismissed screen *after* the
        result callback), so the deferred call is flushed here too.
        """
        self.ui.answer(True)
        self.ui.flush_deferred()

    @property
    def recorder(self) -> OpsRecorder:
        """The `WriteOps` fake, for the flows that were given one."""
        assert self.ops is not None
        return self.ops

    def _set_progress(self, label: str) -> None:
        self.progress.append(label)

    def _helm_uninstall(self) -> None:
        self.helm_uninstalls += 1

    async def _get_manifest(self, kind: str, ns: str | None, name: str) -> dict[str, Any]:
        self.manifest_calls.append((kind, ns, name))
        manifest = self.manifests.get(name)
        if manifest is None:
            raise ApiStatusError(404, "not found")
        # A deep copy: a flow that mutates the fetched manifest (the edit
        # round-trip strips managedFields) must not rewrite the fixture.
        copied: dict[str, Any] = json.loads(json.dumps(manifest))
        return copied

    async def _managed_note(self, kind_alias: str, ns: str | None, name: str) -> str | None:
        return self.notes.get(name)

    async def _managed_note_from(self, manifest: dict[str, Any], ns: str | None) -> str | None:
        return self.notes.get(str(manifest.get("metadata", {}).get("name", "")))

    def audit_records(self) -> list[tuple[str, str]]:
        if not self.audit_path.exists():
            return []
        return [
            (json.loads(line)["action"], json.loads(line)["outcome"])
            for line in self.audit_path.read_text().splitlines()
        ]


def _pods_view(*, uid: str | None = "uid-1") -> FakeView:
    return FakeView(
        kind="pods",
        scope="default",
        selected=("default", "web-1"),
        uid=uid,
        aliases=_ALIASES,
    )


def _deployments_view(*, desired: int | None = 3) -> FakeView:
    return FakeView(
        kind="deployments",
        scope="default",
        selected=("default", "web"),
        uid="uid-d",
        aliases=_ALIASES,
        rows=[
            GenericSummary(
                name="web",
                namespace="default",
                kind="Deployment",
                created="",
                uid="uid-d",
                desired=desired,
            )
        ],
    )


def _nodes_view() -> FakeView:
    return FakeView(
        kind="nodes",
        scope="default",
        selected=(None, "node-a"),
        uid="uid-n",
        aliases=_ALIASES,
    )


def _subdir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return path


def _drain_target(name: str) -> DrainTarget:
    return DrainTarget(
        namespace="default", name=name, uid=f"uid-{name}", local_storage=False, pdb_blocked=None
    )


# ---------------------------------------------------------------------------
# 1. Approval is a precondition: a declined dialog constructs no operation
# ---------------------------------------------------------------------------


async def test_declined_delete_constructs_no_operation(tmp_path: Path) -> None:
    """The factory is the whole point of `confirm(op_factory=...)`: a
    declined dialog must leave no mutation coroutine, no API call and no
    audit record behind."""
    env = Env(tmp_path=tmp_path, view=_pods_view(), ops=OpsRecorder())
    await env.controller.delete()
    screen, _callback = env.ui.screens[-1]
    assert isinstance(screen, ConfirmScreen)
    env.ui.answer(False)
    await env.ui.settle()
    assert env.coordinator.factory_calls == []
    assert env.recorder.calls == []
    assert env.audit_records() == []


async def test_declined_rollout_restart_constructs_no_operation(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, view=_deployments_view(), ops=OpsRecorder())
    await env.controller.rollout_restart()
    env.ui.answer(False)
    await env.ui.settle()
    assert env.coordinator.factory_calls == []
    assert env.recorder.calls == []
    assert env.audit_records() == []


async def test_declined_edit_constructs_no_operation(tmp_path: Path) -> None:
    async def bump(text: str) -> str | None:
        return text.replace("nginx:1", "nginx:2")

    env = Env(
        tmp_path=tmp_path,
        view=_pods_view(),
        ops=OpsRecorder(),
        manifests={"web-1": _POD_MANIFEST},
        edit_text=bump,
    )
    await env.controller.edit()
    env.ui.answer(False)
    await env.ui.settle()
    assert env.coordinator.factory_calls == []
    assert env.recorder.calls == []
    assert env.audit_records() == []


# ---------------------------------------------------------------------------
# 2. Every workflow mutates only through the coordinator
# ---------------------------------------------------------------------------


async def test_approved_delete_runs_through_the_coordinator(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, view=_pods_view(), ops=OpsRecorder())
    await env.controller.delete()
    env.ui.answer(True)
    await env.ui.settle()
    assert env.coordinator.runs == ["delete"]
    assert env.recorder.calls == [("delete", "pods", "default", "web-1", "uid-1")]
    assert env.audit_records() == [("delete", "intent"), ("delete", "success")]


async def test_approved_rollout_restart_replays_the_previewed_stamp(tmp_path: Path) -> None:
    """One stamp per approval: the previewed request and the executed write
    are byte-identical, so the preview is an exact replay."""
    env = Env(tmp_path=tmp_path, view=_deployments_view(), ops=OpsRecorder())
    await env.controller.rollout_restart()
    env.ui.answer(True)
    await env.ui.settle()
    assert env.coordinator.runs == ["rollout_restart"]
    call = env.recorder.calls[0]
    assert call[:5] == ("restart", "deployments", "default", "web", "uid-d")
    assert call[5]  # the restartedAt stamp travelled from preview to write


async def test_approved_edit_replaces_with_the_edited_manifest(tmp_path: Path) -> None:
    async def bump(text: str) -> str | None:
        return text.replace("nginx:1", "nginx:2")

    env = Env(
        tmp_path=tmp_path,
        view=_pods_view(),
        ops=OpsRecorder(),
        manifests={"web-1": _POD_MANIFEST},
        edit_text=bump,
    )
    await env.controller.edit()
    env.ui.answer(True)
    await env.ui.settle()
    assert env.coordinator.runs == ["edit"]
    verb, plural, ns, name, manifest, uid = env.recorder.calls[0]
    assert (verb, plural, ns, name, uid) == ("replace", "pods", "default", "web-1", "uid-1")
    assert manifest["spec"]["containers"][0]["image"] == "nginx:2"
    # managedFields is server bookkeeping; resourceVersion must survive so a
    # concurrent modification 409s instead of being clobbered.
    assert "managedFields" not in manifest["metadata"]
    assert manifest["metadata"]["resourceVersion"] == "42"


async def test_a_broken_audit_log_blocks_every_resource_write(tmp_path: Path) -> None:
    """Fail-closed auditing is the coordinator's, not the workflow's: a
    workflow that mutated `WriteOps` itself would sail past this."""
    env = Env(tmp_path=tmp_path, view=_pods_view(), ops=OpsRecorder(), audit="broken")
    await env.controller.delete()
    env.ui.answer(True)
    await env.ui.settle()
    assert env.recorder.calls == []
    assert any("audit log unavailable" in message for message in env.ui.messages())


# ---------------------------------------------------------------------------
# 3. Scale: captured identity, captured count, and the scale-down blast radius
# ---------------------------------------------------------------------------


async def _answer_replicas(env: Env, replicas: int | None) -> None:
    screen, callback = env.ui.screens[-1]
    assert isinstance(screen, ReplicasPrompt)
    assert callback is not None
    callback(replicas)
    await asyncio.sleep(0)
    await env.ui.settle()


async def test_scale_confirmation_pins_the_captured_target_and_count(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, view=_deployments_view(desired=3), ops=OpsRecorder())
    await env.controller.scale()
    await _answer_replicas(env, 5)
    confirm = env.coordinator.confirms[-1]
    assert confirm["action"] == "scale"
    assert (confirm["meta"], confirm["namespace"], confirm["name"]) == (
        _DEPLOY_META,
        "default",
        "web",
    )
    assert "replicas 3 -> 5" in confirm["operation"]
    assert confirm["detail"] == "replicas -> 5"
    # The dialog is the longest awaited gap a scale has: the captured
    # identity and count are re-validated once more on approval.
    assert confirm["guarded"] is True


async def test_scale_down_carries_the_blast_radius_and_a_scale_up_does_not(
    tmp_path: Path,
) -> None:
    class Loader:
        def __init__(self) -> None:
            self.loads = 0

        async def load(self, root: GraphResource, namespace: str | None, aliases: Any) -> Any:
            self.loads += 1
            raise RuntimeError("snapshot unavailable")

    loader = Loader()
    env = Env(
        tmp_path=_subdir(tmp_path, "down"),
        view=_deployments_view(desired=3),
        ops=OpsRecorder(),
        loader=loader,
    )
    await env.controller.scale()
    await _answer_replicas(env, 1)
    assert loader.loads == 1
    assert env.coordinator.confirms[-1]["impact_lines"] is not None

    up_loader = Loader()
    up = Env(
        tmp_path=_subdir(tmp_path, "up"),
        view=_deployments_view(desired=3),
        ops=OpsRecorder(),
        loader=up_loader,
    )
    await up.controller.scale()
    await _answer_replicas(up, 9)
    assert up_loader.loads == 0, "a scale-up has no tested scale-down semantics"
    assert up.coordinator.confirms[-1]["impact_lines"] is None


async def test_scale_aborts_when_the_replica_count_moved_during_the_prompt(
    tmp_path: Path,
) -> None:
    """The captured count is what makes the request a decrease and what the
    approval line reads `old -> new` from."""
    view = _deployments_view(desired=3)
    env = Env(tmp_path=tmp_path, view=view, ops=OpsRecorder())
    await env.controller.scale()
    view.rows = [
        GenericSummary(
            name="web",
            namespace="default",
            kind="Deployment",
            created="",
            uid="uid-d",
            desired=7,
        )
    ]
    await _answer_replicas(env, 5)
    assert env.coordinator.confirms == []
    assert env.recorder.previews == []
    assert any("desired replica count changed" in message for message in env.ui.messages())


# ---------------------------------------------------------------------------
# 4. Resize: pod target, summary, and the composed resize impact
# ---------------------------------------------------------------------------


async def _answer_resources(env: Env, resources: dict[str, dict[str, dict[str, str]]]) -> None:
    screen, callback = env.ui.screens[-1]
    assert isinstance(screen, ResizePrompt)
    assert callback is not None
    callback(resources)
    await asyncio.sleep(0)
    await env.ui.settle()


async def test_resize_confirmation_carries_the_pod_target_and_summary(tmp_path: Path) -> None:
    env = Env(
        tmp_path=tmp_path,
        view=_pods_view(),
        ops=OpsRecorder(),
        manifests={"web-1": _POD_MANIFEST},
    )
    await env.controller.resize_pod()
    await _answer_resources(env, {"app": {"requests": {"cpu": "200m"}}})
    confirm = env.coordinator.confirms[-1]
    assert confirm["action"] == "resize"
    assert (confirm["meta"], confirm["namespace"], confirm["name"]) == (
        _PODS_META,
        "default",
        "web-1",
    )
    assert confirm["detail"] == "app: requests.cpu=200m"
    assert "PATCH pods/web-1/resize: app: requests.cpu=200m" in confirm["operation"]
    assert confirm["impact_lines"] is not None
    assert confirm["guarded"] is True


async def test_resize_is_refused_when_the_cluster_lacks_the_subresource(tmp_path: Path) -> None:
    env = Env(
        tmp_path=tmp_path,
        view=_pods_view(),
        ops=OpsRecorder(),
        manifests={"web-1": _POD_MANIFEST},
        pod_resize_supported=False,
    )
    await env.controller.resize_pod()
    assert env.ui.screens == []
    assert any("pods/resize" in message for message in env.ui.messages())


async def test_resize_aborts_when_the_pod_was_replaced_during_the_fetch(tmp_path: Path) -> None:
    replaced = json.loads(json.dumps(_POD_MANIFEST))
    replaced["metadata"]["uid"] = "uid-other"
    env = Env(
        tmp_path=tmp_path, view=_pods_view(), ops=OpsRecorder(), manifests={"web-1": replaced}
    )
    await env.controller.resize_pod()
    assert env.ui.screens == []
    assert any("changed during the manifest fetch" in m for m in env.ui.messages())


# ---------------------------------------------------------------------------
# 5. Node cordon / uncordon
# ---------------------------------------------------------------------------


async def test_cordon_confirmation_carries_the_node_target_and_audit_detail(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=OpsRecorder())
    await env.controller.cordon()
    confirm = env.coordinator.confirms[-1]
    assert confirm["action"] == "cordon"
    assert (confirm["meta"], confirm["namespace"], confirm["name"]) == (
        _NODES_META,
        None,
        "node-a",
    )
    assert confirm["detail"] == "spec.unschedulable=true"
    assert "PATCH nodes/node-a spec.unschedulable=true" in confirm["operation"]
    assert confirm["impact_lines"] is not None
    env.approve()
    await env.ui.settle()
    assert env.recorder.calls == [("cordon", "node-a", True, "uid-n")]
    assert env.audit_records() == [("cordon", "intent"), ("cordon", "success")]


async def test_uncordon_confirmation_carries_the_node_target_and_audit_detail(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=OpsRecorder())
    await env.controller.uncordon()
    confirm = env.coordinator.confirms[-1]
    assert confirm["action"] == "uncordon"
    assert confirm["detail"] == "spec.unschedulable=false"
    env.approve()
    await env.ui.settle()
    assert env.recorder.calls == [("cordon", "node-a", False, "uid-n")]
    assert env.audit_records() == [("uncordon", "intent"), ("uncordon", "success")]


async def test_cordon_is_refused_while_the_selected_node_is_draining(tmp_path: Path) -> None:
    """The drain owns the node's schedulable state until it ends: an
    uncordon behind its back would let new pods land mid-drain."""
    plan = DrainPlan(targets=(_drain_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    ops = OpsRecorder(plan=plan)
    ops.evict_gate = asyncio.Event()
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=ops)
    await env.controller.drain_node()
    env.ui.answer(True)
    await asyncio.sleep(0)
    await env.controller.uncordon()
    assert any("is being drained" in message for message in env.ui.messages())
    assert env.coordinator.confirms == []
    ops.evict_gate.set()
    await env.ui.settle()


# ---------------------------------------------------------------------------
# 6. Drain lifecycle: press-again-to-cancel, and state cleanup
# ---------------------------------------------------------------------------


async def _start_drain(env: Env) -> None:
    await env.controller.drain_node()
    screen, _callback = env.ui.screens[-1]
    assert isinstance(screen, ConfirmScreen)
    env.ui.answer(True)
    await asyncio.sleep(0)


async def test_second_drain_press_cancels_the_worker_without_starting_another(
    tmp_path: Path,
) -> None:
    plan = DrainPlan(targets=(_drain_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    ops = OpsRecorder(plan=plan)
    ops.evict_gate = asyncio.Event()
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=ops)
    await _start_drain(env)
    worker = env.controller.drain_worker
    assert worker is not None
    assert worker.is_running
    screens_before = len(env.ui.screens)

    await env.controller.drain_node()
    await env.ui.settle()

    assert len(env.ui.screens) == screens_before, "no second approval dialog"
    assert worker.is_running is False
    assert env.controller.drain_worker is worker
    assert env.controller.drain_node_name is None


async def test_drain_key_on_another_node_does_not_cancel_the_running_drain(
    tmp_path: Path,
) -> None:
    plan = DrainPlan(targets=(_drain_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    ops = OpsRecorder(plan=plan)
    ops.evict_gate = asyncio.Event()
    view = _nodes_view()
    env = Env(tmp_path=tmp_path, view=view, ops=ops)
    await _start_drain(env)
    worker = env.controller.drain_worker
    assert worker is not None

    view.selected = (None, "node-b")
    await env.controller.drain_node()
    assert worker.is_running, "a different node's key press must not kill the drain"
    assert any("press the drain key on it to cancel" in m for m in env.ui.messages())

    ops.evict_gate.set()
    await env.ui.settle()


async def test_drain_state_is_cleared_after_a_successful_drain(tmp_path: Path) -> None:
    plan = DrainPlan(targets=(_drain_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=OpsRecorder(plan=plan))
    await _start_drain(env)
    await env.ui.settle()
    assert env.controller.drain_node_name is None
    assert env.coordinator.active_writes() == 0
    assert env.progress[-1] == ""
    assert ("drain", "success") in env.audit_records()


async def test_drain_state_is_cleared_after_a_failed_drain(tmp_path: Path) -> None:
    plan = DrainPlan(targets=(_drain_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    ops = OpsRecorder(plan=plan, error=ApiStatusError(500, "cordon exploded"))
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=ops)
    await _start_drain(env)
    await env.ui.settle()
    assert env.controller.drain_node_name is None
    assert env.coordinator.active_writes() == 0
    assert any("failed: cordon" in message for message in env.ui.messages())


async def test_drain_state_is_cleared_after_a_cancelled_drain(tmp_path: Path) -> None:
    plan = DrainPlan(targets=(_drain_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    ops = OpsRecorder(plan=plan)
    ops.evict_gate = asyncio.Event()
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=ops)
    await _start_drain(env)
    assert env.coordinator.active_writes() == 1
    worker = env.controller.drain_worker
    assert worker is not None
    worker.cancel()
    await env.ui.settle()
    assert env.controller.drain_node_name is None
    assert env.coordinator.active_writes() == 0, "a cancelled drain must not wedge `:ctx`"


async def test_a_running_drain_reserves_exactly_one_write(tmp_path: Path) -> None:
    plan = DrainPlan(targets=(_drain_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    ops = OpsRecorder(plan=plan)
    ops.evict_gate = asyncio.Event()
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=ops)
    await env.controller.drain_node()
    env.ui.answer(True)
    # Reserved synchronously where the coroutine is built: a `:ctx` queued
    # before the worker starts must already see the drain in flight.
    assert env.coordinator.active_writes() == 1
    await asyncio.sleep(0)
    assert env.coordinator.active_writes() == 1
    ops.evict_gate.set()
    await env.ui.settle()


async def test_declined_drain_starts_no_worker_and_cordons_nothing(tmp_path: Path) -> None:
    plan = DrainPlan(targets=(_drain_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=OpsRecorder(plan=plan))
    await env.controller.drain_node()
    env.ui.answer(False)
    await env.ui.settle()
    assert env.controller.drain_worker is None
    assert env.recorder.calls == []
    assert env.audit_records() == []


# ---------------------------------------------------------------------------
# 7. Delete redirects: helm releases and OLM subscriptions
# ---------------------------------------------------------------------------


async def test_delete_on_the_helm_release_view_redirects_to_helm_uninstall(
    tmp_path: Path,
) -> None:
    """A raw Secret delete would orphan the release's deployed resources."""
    view = FakeView(
        kind="helmreleases",
        scope="default",
        selected=("default", "web"),
        uid="uid-h",
        aliases=_ALIASES,
    )
    env = Env(tmp_path=tmp_path, view=view, ops=OpsRecorder())
    await env.controller.delete()
    assert env.helm_uninstalls == 1
    assert env.ui.screens == []
    assert env.recorder.calls == []


async def test_delete_on_a_subscription_redirects_to_the_operator_uninstall(
    tmp_path: Path,
) -> None:
    """Deleting the Subscription alone leaves the CSV — and the operator —
    running."""
    view = FakeView(
        kind="subscriptions",
        scope="operators",
        selected=("operators", "cert-manager"),
        uid="uid-s",
        aliases=_ALIASES,
    )
    env = Env(tmp_path=tmp_path, view=view, ops=OpsRecorder())
    await env.controller.delete()
    assert env.operators.uninstalls == [("subscriptions", "operators", "cert-manager", "uid-s")]
    assert env.ui.screens == []
    assert env.recorder.calls == []


# ---------------------------------------------------------------------------
# 8. Eligibility and session guards
# ---------------------------------------------------------------------------


async def test_rollout_restart_is_refused_on_a_pod(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, view=_pods_view(), ops=OpsRecorder())
    await env.controller.rollout_restart()
    assert env.coordinator.confirms == []
    assert any("does not apply" in message for message in env.ui.messages())


async def test_scale_is_refused_on_a_pod(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, view=_pods_view(), ops=OpsRecorder())
    await env.controller.scale()
    assert env.coordinator.confirms == []
    assert any("does not apply" in message for message in env.ui.messages())


async def test_every_workflow_reports_when_the_session_has_no_write_client(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=None)
    await env.controller.delete()
    await env.controller.cordon()
    await env.controller.drain_node()
    assert env.coordinator.confirms == []
    assert env.ui.screens == []
    assert all("unavailable in this session" in m for m in env.ui.messages())


async def test_read_only_sessions_open_no_write_dialog(tmp_path: Path) -> None:
    view = FakeView(
        kind="pods",
        scope="default",
        selected=("default", "web-1"),
        uid="uid-1",
        aliases=_ALIASES,
        readonly=True,
    )
    env = Env(tmp_path=tmp_path, view=view, ops=OpsRecorder())
    await env.controller.delete()
    assert env.ui.screens == []
    assert any("Read-only mode" in message for message in env.ui.messages())


# ---------------------------------------------------------------------------
# 9. The editor round-trip
# ---------------------------------------------------------------------------


async def test_edit_reports_invalid_yaml_and_opens_no_dialog(tmp_path: Path) -> None:
    async def broken(text: str) -> str | None:
        return "not: [valid"

    env = Env(
        tmp_path=tmp_path,
        view=_pods_view(),
        ops=OpsRecorder(),
        manifests={"web-1": _POD_MANIFEST},
        edit_text=broken,
    )
    await env.controller.edit()
    assert env.ui.screens == []
    assert any("invalid YAML" in message for message in env.ui.messages())


async def test_edit_restores_a_deleted_resource_version(tmp_path: Path) -> None:
    """An unversioned PUT would silently clobber concurrent changes."""

    async def strip_version(text: str) -> str | None:
        return text.replace("resourceVersion: '42'\n", "").replace("nginx:1", "nginx:2")

    env = Env(
        tmp_path=tmp_path,
        view=_pods_view(),
        ops=OpsRecorder(),
        manifests={"web-1": _POD_MANIFEST},
        edit_text=strip_version,
    )
    await env.controller.edit()
    env.ui.answer(True)
    await env.ui.settle()
    manifest = env.recorder.calls[0][4]
    assert manifest["metadata"]["resourceVersion"] == "42"


async def test_edit_reports_no_changes_and_opens_no_dialog(tmp_path: Path) -> None:
    async def unchanged(text: str) -> str | None:
        return text

    env = Env(
        tmp_path=tmp_path,
        view=_pods_view(),
        ops=OpsRecorder(),
        manifests={"web-1": _POD_MANIFEST},
        edit_text=unchanged,
    )
    await env.controller.edit()
    assert env.ui.screens == []
    assert any("no changes" in message for message in env.ui.messages())


async def test_edit_detail_names_the_changed_top_level_sections(tmp_path: Path) -> None:
    async def bump(text: str) -> str | None:
        return text.replace("nginx:1", "nginx:2")

    env = Env(
        tmp_path=tmp_path,
        view=_pods_view(),
        ops=OpsRecorder(),
        manifests={"web-1": _POD_MANIFEST},
        edit_text=bump,
    )
    await env.controller.edit()
    assert env.coordinator.confirms[-1]["detail"] == "changed: spec"


# ---------------------------------------------------------------------------
# 10. Post-await revalidation belongs to the perimeter, and is not skipped
# ---------------------------------------------------------------------------


async def test_delete_aborts_when_the_context_switched_during_the_preview(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path=tmp_path, view=_pods_view(), ops=OpsRecorder())
    original = env.coordinator.dry_run_preview

    async def switch_then_preview(coro: Any) -> list[str] | None:
        result = await original(coro)
        env.context.value += 1
        return result

    env.coordinator.dry_run_preview = switch_then_preview  # type: ignore[method-assign]  # narrow fake seam
    await env.controller.delete()
    assert env.ui.screens == []
    assert env.coordinator.confirms == []
    assert any("kube context changed" in message for message in env.ui.messages())


async def test_delete_aborts_when_the_selection_moved_during_the_impact_summary(
    tmp_path: Path,
) -> None:
    view = _pods_view()
    env = Env(tmp_path=tmp_path, view=view, ops=OpsRecorder())
    original = env.coordinator.impact_preview

    async def move_then_summarize(*args: Any, **kwargs: Any) -> tuple[str, ...] | None:
        result = await original(*args, **kwargs)
        view.selected = ("default", "web-2")
        return result

    env.coordinator.impact_preview = move_then_summarize  # type: ignore[method-assign]  # narrow fake seam
    await env.controller.delete()
    assert env.coordinator.confirms == []
    assert any("selection changed" in message for message in env.ui.messages())


async def test_a_switch_in_progress_refuses_the_write_before_any_dialog(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path=tmp_path, view=_pods_view(), ops=OpsRecorder())
    env.context.is_switching = True
    await env.controller.delete()
    assert env.ui.screens == []
    assert any("context switch is in progress" in message for message in env.ui.messages())


# ---------------------------------------------------------------------------
# 11. The external editor is the controller's, and fails closed on cancel
# ---------------------------------------------------------------------------


async def test_external_editor_cancels_when_the_editor_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path=tmp_path, view=_pods_view(), ops=OpsRecorder())
    monkeypatch.setenv("EDITOR", "true")

    def failed(argv: list[str]) -> int:
        return 1

    monkeypatch.setattr("subprocess.call", failed)
    assert await env.controller.edit_in_external_editor("a: 1\n") is None


async def test_external_editor_reports_an_unusable_editor_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path=tmp_path, view=_pods_view(), ops=OpsRecorder())
    monkeypatch.setenv("EDITOR", "   ")
    monkeypatch.setenv("VISUAL", "   ")
    assert await env.controller.edit_in_external_editor("a: 1\n") is None
    assert any("failed" in message for message in env.ui.messages())


# ---------------------------------------------------------------------------
# 12. The controller never reaches around the perimeter
# ---------------------------------------------------------------------------


def test_the_controller_holds_no_app_reference() -> None:
    """`ResourceWriteController` must be constructible without Textual's
    `App`: the moment it can reach `KorvidApp`, the perimeter stops being
    the only way to a mutation."""
    import ast
    import inspect

    from korvid.ui import resource_write_controller

    tree = ast.parse(inspect.getsource(resource_write_controller))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert "korvid.ui.app" not in modules
    assert not any(module.startswith("korvid.ui.app.") for module in modules)


async def test_no_workflow_awaits_write_ops_without_a_coordinator_run(
    tmp_path: Path,
) -> None:
    """Every mutation this controller performs is one the coordinator ran.

    The counts are compared per workflow: a flow that called `WriteOps`
    directly would add a mutation with no matching `run`.
    """
    flows: list[tuple[str, Callable[[Env], Awaitable[None]]]] = [
        ("delete", lambda env: env.controller.delete()),
        ("cordon", lambda env: env.controller.cordon()),
        ("uncordon", lambda env: env.controller.uncordon()),
    ]
    for action, flow in flows:
        view = _pods_view() if action == "delete" else _nodes_view()
        env = Env(tmp_path=_subdir(tmp_path, action), view=view, ops=OpsRecorder())
        await flow(env)
        env.approve()
        await env.ui.settle()
        assert env.coordinator.runs == [action]
        mutations = [call for call in env.recorder.calls if call[0] in ("delete", "cordon")]
        assert len(mutations) == len(env.coordinator.runs)


async def test_a_cancelled_write_worker_releases_its_reservation(tmp_path: Path) -> None:
    """A leaked `+1` would block every later `:ctx` switch for the session."""
    ops = OpsRecorder()
    ops.cordon_gate = asyncio.Event()
    env = Env(tmp_path=tmp_path, view=_nodes_view(), ops=ops)
    await env.controller.cordon()
    env.approve()
    assert env.coordinator.active_writes() == 1
    for task in env.ui.workers:
        task.cancel()
    for task in env.ui.workers:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert env.coordinator.active_writes() == 0
