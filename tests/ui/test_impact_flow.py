"""Graph-derived impact previews in the delete/rollout-restart flows (#283)
and in the workload scale-down flow (#295).

The app reuses the relationship snapshot loader it already owns for `g`: no
new LIST/GET interface, no new constructor parameter, no composition-root
change. What this module pins beyond "the section renders" is the wiring
that only shows up end to end: the snapshot's scope is chosen by the target
(cluster-scoped kinds cover every namespace), the row's exact UID reaches
the summary, unsupported write flows are untouched, and every awaited gap
still revalidates. The scale-down flow (#295) adds a gap the other two do
not have - the replica-count prompt - so its tests also pin *when* the
origin pane is captured and that no snapshot is loaded once the selection,
the pane or the pane's scope has drifted. It also owns the one gap no other
write flow re-checks - the approval dialog itself, which stays open until
the user answers - so its tests pin that drift landing there is refused on
approval, before any worker, reservation, audit record or operation exists.
This module owns the shared harness (`ImpactEnv`) that
`tests/ui/test_impact_security.py` reuses for the security invariants.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from textual.widgets import Static

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta, build_alias_map
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    RelationKind,
    RelationshipFacts,
    SelectorFact,
    TargetReference,
)
from korvid.k8s.selectors import LabelSelector
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

#: The loader's fixed source catalog, so a test snapshot's only non-complete
#: coverage records are the ones a test asks for (plus the always-absent
#: Gateway API group).
CATALOG_METAS = [
    ResourceMeta("Pod", "pods", "", "v1", True, ("po",)),
    ResourceMeta("Service", "services", "", "v1", True, ("svc",)),
    ResourceMeta("ConfigMap", "configmaps", "", "v1", True, ("cm",)),
    ResourceMeta("Secret", "secrets", "", "v1", True),
    ResourceMeta("PersistentVolumeClaim", "persistentvolumeclaims", "", "v1", True, ("pvc",)),
    ResourceMeta("PersistentVolume", "persistentvolumes", "", "v1", False, ("pv",)),
    ResourceMeta("Node", "nodes", "", "v1", False, ("no",)),
    ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",)),
    ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True, ("rs",)),
    ResourceMeta("StatefulSet", "statefulsets", "apps", "v1", True, ("sts",)),
    ResourceMeta("DaemonSet", "daemonsets", "apps", "v1", True, ("ds",)),
    ResourceMeta("Job", "jobs", "batch", "v1", True),
    ResourceMeta("CronJob", "cronjobs", "batch", "v1", True, ("cj",)),
    ResourceMeta("EndpointSlice", "endpointslices", "discovery.k8s.io", "v1", True),
    ResourceMeta("Ingress", "ingresses", "networking.k8s.io", "v1", True, ("ing",)),
    ResourceMeta("PodDisruptionBudget", "poddisruptionbudgets", "policy", "v1", True, ("pdb",)),
]
CATALOG_ALIASES = build_alias_map(CATALOG_METAS)


def _owner(kind: str, name: str, uid: str, *, group: str) -> ReferenceFact:
    return ReferenceFact(
        relation=RelationKind.OWNED_BY,
        target=TargetReference(group=group, kind=kind, namespace="prod", name=name, uid=uid),
        confidence=FactConfidence.DECLARED,
        field="metadata.ownerReferences[0]",
    )


def _workload_selector(*, app: str = "web") -> SelectorFact:
    """The `spec.selector -> Pod` fact every real workload summary carries.

    Built exactly the way `korvid.k8s.relationship_facts._workload_selector`
    builds it for a Deployment/ReplicaSet/StatefulSet: relation `managed_by`,
    `match_is_subject=True`, so the resulting edge runs *Pod -> workload* and
    the reverse impact walk reaches the Pod from the workload in one hop,
    beside (not through) the ReplicaSet the ownerReferences chain gives it.
    """
    return SelectorFact(
        relation=RelationKind.MANAGED_BY,
        target_group="",
        target_kind="Pod",
        selector=LabelSelector(match_labels=(("app", app),), present=True),
        confidence=FactConfidence.DECLARED,
        field="spec.selector",
        match_is_subject=True,
    )


def _deployment(
    name: str, uid: str, *, desired: int | None = 3, selects_pods: bool = False
) -> GenericSummary:
    """A Deployment row. `desired` is what the scale flow reads to decide
    whether a requested count is a decrease; `None` is a summary that does
    not carry one at all.

    `selects_pods` attaches the workload selector a real Deployment always
    has. It is opt-in only so the delete/rollout-restart fixtures keep the
    exact dependent sets their own assertions were written against; every
    row a scale-down walks carries it, because the hop counts a scale-down
    reports depend on it.
    """
    return GenericSummary(
        name=name,
        namespace="prod",
        kind="Deployment",
        created="",
        desired=desired,
        uid=uid,
        relationships=(
            RelationshipFacts(api_group="apps", selectors=(_workload_selector(),))
            if selects_pods
            # Byte-identical to the pre-#295 fixture when the selector is
            # not asked for: the delete/rollout-restart assertions below are
            # written against exactly that row.
            else RelationshipFacts()
        ),
    )


def _replicaset(*, desired: int | None = 3, selects_pods: bool = False) -> GenericSummary:
    """A ReplicaSet row owned by the `web` Deployment.

    `selects_pods` attaches the `spec.selector -> Pod` fact a real
    ReplicaSet summary always carries, exactly as `_deployment` does and for
    the same reason: it is opt-in only so the delete/rollout-restart
    fixtures keep the dependent sets their own assertions were written
    against, while every row a scale-down walks carries it.
    """
    return GenericSummary(
        name="web-abc",
        namespace="prod",
        kind="ReplicaSet",
        created="",
        desired=desired,
        uid="rs-1",
        relationships=RelationshipFacts(
            api_group="apps",
            references=(_owner("Deployment", "web", "deploy-1", group="apps"),),
            selectors=(_workload_selector(),) if selects_pods else (),
        ),
    )


def _statefulset() -> GenericSummary:
    """A StatefulSet with the workload selector a real one carries.

    The one target kind whose advisory also states that PVC retention
    policy is not evaluated; `db` keeps it clear of the `web` fixtures, so
    its selector can never match their Pods.
    """
    return GenericSummary(
        name="db",
        namespace="prod",
        kind="StatefulSet",
        created="",
        desired=3,
        uid="sts-1",
        relationships=RelationshipFacts(
            api_group="apps",
            selectors=(_workload_selector(app="db"),),
        ),
    )


def _statefulset_pod() -> PodSummary:
    """The Pod that StatefulSet's selector matches."""
    return PodSummary(
        name="db-0",
        namespace="prod",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        uid="pod-db-0",
        labels=(("app", "db"),),
    )


def _staging_deployment() -> GenericSummary:
    """A Deployment in another namespace: visible only to a cluster-wide
    snapshot, and never a dependent of `prod/web`."""
    return GenericSummary(
        name="api", namespace="staging", kind="Deployment", created="", desired=1, uid="deploy-3"
    )


def _staging_replicaset() -> GenericSummary:
    return GenericSummary(
        name="api-def",
        namespace="staging",
        kind="ReplicaSet",
        created="",
        uid="rs-2",
        relationships=RelationshipFacts(
            api_group="apps",
            references=(
                ReferenceFact(
                    relation=RelationKind.OWNED_BY,
                    target=TargetReference(
                        group="apps",
                        kind="Deployment",
                        namespace="staging",
                        name="api",
                        uid="deploy-3",
                    ),
                    confidence=FactConfidence.DECLARED,
                    field="metadata.ownerReferences[0]",
                ),
            ),
        ),
    )


def _pod() -> PodSummary:
    return PodSummary(
        name="web-abc-1",
        namespace="prod",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        uid="pod-1",
        labels=(("app", "web"),),
        relationships=RelationshipFacts(
            references=(
                _owner("ReplicaSet", "web-abc", "rs-1", group="apps"),
                ReferenceFact(
                    relation=RelationKind.USES_CONFIG,
                    target=TargetReference(
                        group="", kind="ConfigMap", namespace="prod", name="app-config"
                    ),
                    confidence=FactConfidence.DECLARED,
                    field="spec.volumes[0].configMap",
                ),
            )
        ),
    )


def _configmap() -> GenericSummary:
    return GenericSummary(
        name="app-config", namespace="prod", kind="ConfigMap", created="", uid="cm-1"
    )


def _service() -> GenericSummary:
    """A Service selecting the Pod: deleting that Pod must never claim the
    Service fails (`selects` has no action semantics)."""
    return GenericSummary(
        name="web",
        namespace="prod",
        kind="Service",
        created="",
        uid="svc-1",
        relationships=RelationshipFacts(
            selectors=(
                SelectorFact(
                    relation=RelationKind.SELECTS,
                    target_group="",
                    target_kind="Pod",
                    selector=LabelSelector(match_labels=(("app", "web"),), present=True),
                    confidence=FactConfidence.DECLARED,
                    field="spec.selector",
                ),
            )
        ),
    )


def _ingress() -> GenericSummary:
    """An Ingress routing to the Service above: a declared `routes_to`
    dependent that only a scale-down follows."""
    return GenericSummary(
        name="web",
        namespace="prod",
        kind="Ingress",
        created="",
        uid="ing-1",
        relationships=RelationshipFacts(
            api_group="networking.k8s.io",
            references=(
                ReferenceFact(
                    relation=RelationKind.ROUTES_TO,
                    target=TargetReference(
                        group="",
                        kind="Service",
                        namespace="prod",
                        name="web",
                        uid="svc-1",
                    ),
                    confidence=FactConfidence.DECLARED,
                    field="spec.rules[0].http.paths[0].backend.service",
                ),
            ),
        ),
    )


def _endpoint_slice() -> GenericSummary:
    """The EndpointSlice the Service's controller writes for the Pod.

    Shaped like the real object: a `discovery.k8s.io/v1` slice whose
    `endpoints[0].targetRef` names the backing Pod by kind, namespace, name
    *and* uid, which `korvid.k8s.relationship_facts._endpoint_targets`
    turns into an *observed* `routes_to` fact (the control plane wrote it
    after the Pod was ready; it is not a declaration in anyone's spec).
    Direction is EndpointSlice -> Pod, so a scale-down of the workload
    reaches the slice one hop past the Pod: the object that stops naming a
    replica when the replica goes away.
    """
    return GenericSummary(
        name="web-xyz",
        namespace="prod",
        kind="EndpointSlice",
        created="",
        uid="eps-1",
        relationships=RelationshipFacts(
            api_group="discovery.k8s.io",
            references=(
                ReferenceFact(
                    relation=RelationKind.ROUTES_TO,
                    target=TargetReference(
                        group="",
                        kind="Pod",
                        namespace="prod",
                        name="web-abc-1",
                        uid="pod-1",
                    ),
                    confidence=FactConfidence.OBSERVED,
                    field="endpoints[0].targetRef",
                ),
            ),
        ),
    )


def _node() -> GenericSummary:
    """A cluster-scoped Node: `namespace` is always empty for these."""
    return GenericSummary(name="worker-1", namespace="", kind="Node", created="", uid="node-1")


def _scheduled_pod(name: str, namespace: str, uid: str) -> PodSummary:
    """A Pod running on `worker-1`, in whichever namespace it belongs to."""
    return PodSummary(
        name=name,
        namespace=namespace,
        phase="Running",
        ready="1/1",
        restarts=0,
        node="worker-1",
        uid=uid,
        relationships=RelationshipFacts(
            references=(
                ReferenceFact(
                    relation=RelationKind.SCHEDULED_ON,
                    target=TargetReference(group="", kind="Node", namespace="", name="worker-1"),
                    confidence=FactConfidence.OBSERVED,
                    field="spec.nodeName",
                ),
            )
        ),
    )


class RecordingLister:
    """Replays snapshot LIST results by plural; records order and calls.

    A real LIST is namespace-scoped, so this one is too: a request for
    namespace `prod` returns only `prod` rows. Without that, a snapshot
    wrongly scoped to one namespace would still see every namespace here
    and the scope tests would prove nothing.
    """

    def __init__(self, rows: dict[str, list[Any]], order: list[str]) -> None:
        self._rows = rows
        self._order = order
        self.calls: list[tuple[str, str | None]] = []
        #: `BaseException`, not `Exception`: a client torn down under a
        #: running LIST raises `asyncio.CancelledError`, which the flow must
        #: propagate rather than fold into the fail-open advisory.
        self.errors: dict[str, BaseException] = {}
        self.delay = 0.0
        #: Fired once, inside the first LIST: how a test simulates a context
        #: switch or selection change landing while the snapshot is loading.
        self.on_first_call: Callable[[], None] | None = None

    async def __call__(self, meta: ResourceMeta, namespace: str | None) -> list[Any]:
        self.calls.append((meta.plural, namespace))
        if not self._order or self._order[-1] != "list":
            self._order.append("list")
        hook, self.on_first_call = self.on_first_call, None
        if hook is not None:
            hook()
        if self.delay:
            await asyncio.sleep(self.delay)
        if meta.plural in self.errors:
            raise self.errors[meta.plural]
        rows = self._rows.get(meta.plural, [])
        if namespace is None:
            return list(rows)
        return [row for row in rows if row.namespace == namespace]


class RecordingOps(WriteOps):
    """Records mutations and dry-run previews; performs none."""

    def __init__(self, order: list[str]) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._order = order
        #: Fired once, inside the first dry-run preview: how a test
        #: simulates a focus, scope or context change landing in the awaited
        #: gap between the dry run and the impact snapshot. What the two
        #: flows then do with that drift differs, and both are asserted
        #: below: a delete/rollout restart still loads its snapshot (scoped
        #: to the *captured* origin, never the drifted-into pane) and is
        #: refused afterwards by the impact-summary gate, while a scale-down
        #: is refused by its own pre-snapshot gate and issues no LIST at all.
        self.on_first_preview: Callable[[], None] | None = None

    def _preview_hook(self) -> None:
        hook, self.on_first_preview = self.on_first_preview, None
        if hook is not None:
            hook()

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("delete", meta.plural, namespace, name, uid))

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("scale", meta.plural, namespace, name, replicas))

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("restart", meta.plural, namespace, name, uid))

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("replace", meta.plural, namespace, name))

    async def preview_delete(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> list[str] | None:
        self._order.append("preview")
        self._preview_hook()
        return [f"- {meta.plural} prod/{name}"]

    async def preview_scale(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> list[str] | None:
        self._order.append("preview")
        self._preview_hook()
        return [f"~ spec.replicas: {replicas}"]

    async def preview_rollout_restart(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> list[str] | None:
        self._order.append("preview")
        self._preview_hook()
        return ["~ spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt"]


class ImpactEnv:
    """App plus recording fakes for the impact-preview integration path.

    `permission` is what the injected SubjectAccessReview fake answers:
    `False` denies every write, which the security tests use to pin that a
    refused write never reaches the snapshot or a dialog.
    """

    def __init__(
        self,
        audit_path: Path,
        *,
        with_lister: bool = True,
        rows: dict[str, list[Any]] | None = None,
        list_rows: dict[str, list[Any]] | None = None,
        permission: bool = True,
    ) -> None:
        self.order: list[str] = []
        self.ops = RecordingOps(self.order)
        #: Rows the watch stream feeds the store, i.e. what is on screen.
        #: `web` sorts before `zz-api`, and the store orders by
        #: `(namespace, name)`, so the default cursor row is always `web` -
        #: the row every delete/restart assertion below names. The second
        #: row exists so a test can *move* the selection during the load.
        self.rows: dict[str, list[Any]] = (
            {
                "pods": [_pod()],
                "deployments": [_deployment("web", "deploy-1"), _deployment("zz-api", "deploy-2")],
                "replicasets": [_replicaset()],
                "configmaps": [_configmap()],
                "services": [_service()],
            }
            if rows is None
            else rows
        )
        #: What the snapshot LISTs return; the watched rows unless a test
        #: needs them to diverge (an object replaced under the same name
        #: between the watch and the snapshot carries a new uid).
        self.list_rows = self.rows if list_rows is None else list_rows
        self.lister = RecordingLister(self.list_rows, self.order)
        #: Fired once, inside the SubjectAccessReview: how a test simulates
        #: drift landing while the permission round trip is in flight, which
        #: is the flow's *first* awaited gap and the only one no LIST or
        #: dry-run hook can reach. Unset by default, so every other test
        #: sees the permission check it always saw.
        self.on_permission_check: Callable[[], None] | None = None
        store = ResourceStore()
        watched = self.rows

        async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
            for row in watched.get(kind, []):
                yield ("ADDED", row)
            while True:
                await asyncio.sleep(0.01)

        async def check_permission(
            verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
        ) -> bool:
            self.order.append("rbac")
            hook, self.on_permission_check = self.on_permission_check, None
            if hook is not None:
                hook()
            # `False` is a denied SubjectAccessReview, which must end the
            # flow before the prompt, the snapshot fan-out and the dialog.
            return permission

        self.app = KorvidApp(
            config=KorvidConfig(namespace="prod"),
            store=store,
            watch_manager=WatchManager(store, source),
            aliases=dict(CATALOG_ALIASES),
            write_ops=self.ops,
            audit=AuditLog(audit_path),
            check_permission=check_permission,
            list_relationship_objects=self.lister if with_lister else None,
        )


async def to_view(pilot: Any, view: str, *, expect: str | None = None) -> None:
    """Navigate to `view` through the command bar and wait for its rows.

    `expect` waits for a specific first row rather than for any row: rows
    from the previous view can still be on screen for a tick after the
    navigation, and every impact assertion depends on which row the cursor
    is on.
    """
    await pilot.press("colon")
    for ch in view:
        await pilot.press(ch)
    await pilot.press("enter")

    def ready() -> bool:
        table = pilot.app.query_one(ResourceTable)
        if table.row_count == 0:
            return False
        return expect is None or str(table.get_row_at(0)[0]) == expect

    await until(pilot, ready, label=f"{view} rows visible")


def impact_text(app: KorvidApp) -> str:
    """The rendered impact section of the open ConfirmScreen."""
    screen = app.screen
    assert isinstance(screen, ConfirmScreen)
    return str(screen.query_one(".confirm-impact", Static).render())


async def open_delete_dialog(
    env: ImpactEnv, pilot: Any, view: str, *, expect: str | None = None
) -> None:
    await to_view(pilot, view, expect=expect)
    await pilot.press("ctrl+d")
    await until(
        pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="confirm dialog opened"
    )


async def test_delete_dialog_shows_direct_and_transitive_dependents(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert "delete apps/Deployment/prod/web" in text
        assert "known direct dependents (may be affected): 1" in text
        assert (
            "apps/ReplicaSet/prod/web-abc via owned_by (declared) at"
            " apps/ReplicaSet/prod/web-abc: metadata.ownerReferences[0]" in text
        )
        assert "known transitive dependents (may be affected): 1" in text
        assert "Pod/prod/web-abc-1 via owned_by (declared)" in text
        assert "scope: prod" in text
        assert env.ops.calls == []


async def test_delete_of_a_pod_never_claims_the_selecting_service_fails(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "pods", expect="web-abc-1")
        text = impact_text(env.app)
        assert "delete Pod/prod/web-abc-1" in text
        assert "Service/prod/web" not in text
        assert "known direct dependents (may be affected): none in this snapshot" in text


async def test_rollout_restart_dialog_shows_the_owner_chain_only(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("r")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="restart dialog"
        )
        text = impact_text(env.app)
        assert "rollout restart apps/Deployment/prod/web" in text
        assert "apps/ReplicaSet/prod/web-abc via owned_by (declared)" in text
        assert "ConfigMap/prod/app-config" not in text
        assert env.ops.calls == []


async def test_rollout_restart_warns_about_an_unresolved_config_reference(tmp_path: Path) -> None:
    """`uses_config` is not a restart relation, but the Pod the restart
    replaces still has to mount its ConfigMap: a dangling reference inside
    the affected set is reported whatever its relation."""
    rows: dict[str, list[Any]] = {
        "deployments": [_deployment("web", "deploy-1"), _deployment("zz-api", "deploy-2")],
        "replicasets": [_replicaset()],
        "pods": [_pod()],  # its ConfigMap is not in this snapshot
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("r")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="restart dialog"
        )
        text = impact_text(env.app)
        assert "unresolved references in the affected set: 1" in text
        assert (
            "Pod/prod/web-abc-1 uses_config (declared) -> ConfigMap/prod/app-config (missing)"
            " at Pod/prod/web-abc-1: spec.volumes[0].configMap" in text
        )
        assert env.ops.calls == []


async def test_deleting_a_cluster_scoped_node_covers_every_namespace(tmp_path: Path) -> None:
    """The pane is scoped to `prod`, but a Node is cluster-scoped: scoping
    its snapshot to the pane would hide the `staging` Pod it also runs and
    let the dialog claim complete coverage of `prod` as if that were the
    whole answer."""
    rows: dict[str, list[Any]] = {
        "nodes": [_node()],
        "pods": [
            _scheduled_pod("web-1", "prod", "pod-1"),
            _scheduled_pod("api-1", "staging", "pod-2"),
        ],
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "nodes", expect="worker-1")
        text = impact_text(env.app)
        assert "delete Node/worker-1" in text
        assert "known direct dependents (may be affected): 2" in text
        assert "Pod/prod/web-1 via scheduled_on (observed) at Pod/prod/web-1: spec.nodeName" in text
        assert (
            "Pod/staging/api-1 via scheduled_on (observed) at Pod/staging/api-1: spec.nodeName"
            in text
        )
        assert "scope: all namespaces" in text
        assert "scope: prod" not in text
        assert ("pods", None) in env.lister.calls


async def test_a_target_replaced_since_the_watch_is_reported_as_unknown(tmp_path: Path) -> None:
    """The row on screen carries uid `deploy-1`; the snapshot only knows a
    `web` with uid `deploy-9`. The write targets the incarnation the user
    saw, so the summary must say it cannot see it - not "no dependents"."""
    env = ImpactEnv(
        tmp_path / "audit.jsonl",
        list_rows={
            "deployments": [_deployment("web", "deploy-9"), _deployment("zz-api", "deploy-2")],
            "replicasets": [_replicaset()],
        },
    )
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert "target not found in this snapshot - dependents unknown" in text
        assert "known direct dependents (may be affected): none in this snapshot" in text
        assert env.ops.calls == []


async def test_no_impact_section_without_a_relationship_loader(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl", with_lister=False)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert not env.app.screen.query(".confirm-impact")
        assert env.app.screen.query(".confirm-preview")
        assert env.lister.calls == []


async def test_incomplete_graph_still_renders_a_summary_with_the_coverage_warning(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    env.lister.errors["configmaps"] = ApiStatusError(403, "configmaps is forbidden")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert (
            "graph coverage: incomplete - a missing dependent here does not prove none exists"
            in text
        )
        assert "core/configmaps @prod: forbidden" in text
        assert "apps/ReplicaSet/prod/web-abc via owned_by (declared)" in text


async def test_impact_timeout_renders_the_static_unavailable_advisory(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    env.lister.delay = 5.0
    with mock.patch("korvid.ui.app._IMPACT_TIMEOUT", 0.01):
        async with env.app.run_test() as pilot:
            await open_delete_dialog(env, pilot, "deploy", expect="web")
            text = impact_text(env.app)
            assert "impact unavailable; approval remains available" in text
            assert "known direct dependents" not in text
            assert env.app.screen.query(".confirm-preview")


async def test_unexpected_loader_failure_renders_the_static_unavailable_advisory(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    env.lister.errors["deployments"] = RuntimeError("parser exploded")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "impact unavailable; approval remains available" in impact_text(env.app)


async def test_a_renderer_failure_renders_the_static_unavailable_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-open boundary covers summarize and render, not just the load.

    A bug in either would otherwise escape `_impact_preview` into the write
    action and take the whole approval dialog with it - the user would lose a
    legitimate confirmation to a *display* failure. Its message is withheld
    for the same reason the loader's is: an exception string can carry
    cluster-derived text.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")

    def boom(summary: Any) -> tuple[str, ...]:
        raise RuntimeError("renderer exploded on AKIAEXAMPLEPAYLOAD")

    monkeypatch.setattr("korvid.ui.app.render_impact_lines", boom)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert "impact unavailable; approval remains available" in text
        assert "AKIAEXAMPLEPAYLOAD" not in text
        assert "known direct dependents" not in text
        assert env.app.screen.query(".confirm-preview")
        assert env.ops.calls == []


async def test_a_summarizer_failure_keeps_the_dialog_and_logs_the_type_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("summarizer exploded on AKIAEXAMPLEPAYLOAD")

    monkeypatch.setattr("korvid.ui.app.summarize_impact", boom)
    with caplog.at_level(logging.DEBUG, logger="korvid.ui.app"):
        async with env.app.run_test() as pilot:
            await open_delete_dialog(env, pilot, "deploy", expect="web")
            text = impact_text(env.app)
            assert "impact unavailable; approval remains available" in text
            assert "AKIAEXAMPLEPAYLOAD" not in text
            assert env.ops.calls == []
    assert "AKIAEXAMPLEPAYLOAD" not in caplog.text
    assert any(
        "impact summary unavailable" in record.getMessage()
        and "RuntimeError" in record.getMessage()
        for record in caplog.records
    )


async def test_a_namespaced_target_in_an_all_namespaces_pane_covers_every_namespace(
    tmp_path: Path,
) -> None:
    """A Deployment is namespaced, but the *pane* is `all`: the snapshot must
    follow what the user is looking at, not the config default. Scoping it to
    `prod` here would list one namespace's dependents while the pane claims to
    show every namespace, and the dialog would then report complete coverage
    of a scope the user never chose."""
    rows: dict[str, list[Any]] = {
        "deployments": [_deployment("web", "deploy-1"), _staging_deployment()],
        "replicasets": [_replicaset(), _staging_replicaset()],
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        # In an all-namespaces pane the first column is NAMESPACE, so the
        # cursor row is identified by both cells.
        await to_view(pilot, "deploy all", expect="prod")
        table = pilot.app.query_one(ResourceTable)
        assert str(table.get_row_at(0)[1]) == "web"
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="confirm dialog opened"
        )
        text = impact_text(env.app)
        assert "delete apps/Deployment/prod/web" in text
        assert "scope: all namespaces" in text
        assert "scope: prod" not in text
        # `None` is what `_impact_scope` hands the loader, and the same value
        # reaches `summarize_impact`: every LIST is cluster-wide.
        assert env.lister.calls != []
        assert {namespace for _, namespace in env.lister.calls} == {None}
        # The staging ReplicaSet is in the snapshot but owned by another
        # Deployment: a cluster-wide scope must not widen the affected set.
        assert "known direct dependents (may be affected): 1" in text
        assert "apps/ReplicaSet/prod/web-abc via owned_by (declared)" in text
        assert "staging" not in text
        assert env.ops.calls == []


#: The rows a scale-down walk is exercised over: the controller chain
#: (Deployment -> ReplicaSet -> Pod), the Service selecting that Pod, the
#: EndpointSlice whose `targetRef` names that Pod, and the Ingress routing
#: to that Service. Both workloads carry the
#: `spec.selector` fact a real summary of their kind always has, so the walk
#: sees the hop counts production would produce (`Deployment -> Pod` and
#: `ReplicaSet -> Pod` are each one hop through `managed_by`, beside - not
#: through - the ownership chain). The EndpointSlice is what makes the
#: *observed* `routes_to` shape production really has - a control-plane
#: object pointing straight at the replica - part of the fixture, beside
#: the Ingress's declared `routes_to` to the Service. The Pod's ConfigMap is
#: included so the walk has no *unrelated* dangling reference: without it
#: the summary also reports `unresolved references in the affected set`,
#: which is the subject of its own test and would otherwise contaminate
#: every assertion about what a scale-down lists.
def _scale_down_rows() -> dict[str, list[Any]]:
    return {
        "pods": [_pod()],
        "deployments": [_deployment("web", "deploy-1", selects_pods=True)],
        "replicasets": [_replicaset(selects_pods=True)],
        "configmaps": [_configmap()],
        "services": [_service()],
        "endpointslices": [_endpoint_slice()],
        "ingresses": [_ingress()],
    }


async def _scale_to_one(env: ImpactEnv, pilot: Any, view: str, *, expect: str) -> None:
    """Open the scale prompt on `view`'s first row and request one replica."""
    await to_view(pilot, view, expect=expect)
    await pilot.press("S")
    await until(pilot, lambda: isinstance(env.app.screen, ReplicasPrompt), label="replicas prompt")
    await pilot.press("1")
    await pilot.press("enter")
    await until(
        pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="scale-down confirm"
    )


#: The exact machine-defined limitation lines a scale-down advisory always
#: carries (`korvid.ui.impact_preview`), asserted here end to end so the
#: renderer's own unit pins cannot be the only place they are checked.
_PDB_LIMITATION = (
    "controller scale-down is not an Eviction API request; PodDisruptionBudgets do not gate it"
)
_HPA_LIMITATION = "HorizontalPodAutoscaler targeting and reconciliation are not evaluated"
_STS_PVC_LIMITATION = "StatefulSet PVC retention policy is not evaluated"


async def test_scale_down_dialog_shows_controller_and_routing_dependents(
    tmp_path: Path,
) -> None:
    """A scale-down follows the ownership chain *and* the relations that
    point at a shrinking workload - the Service selecting its Pods and the
    Ingress routing to that Service. Nothing is claimed to fail.

    A real Deployment carries its own `spec.selector`, so its Pods are one
    `managed_by` hop away rather than two through the ReplicaSet: the
    routing chain is `Deployment -> Pod (managed_by) -> Service (selects) ->
    Ingress (routes_to)`, three hops, inside `ImpactLimits.max_depth`. The
    Ingress is therefore named, and nothing is withheld behind the
    traversal cap. The EndpointSlice the Service's controller wrote reaches
    the same Pod one hop earlier and through the *observed* `routes_to`
    shape production really has - `endpoints[0].targetRef` naming the
    replica directly - so both `routes_to` origins a scale-down can meet
    are named in one dialog. The ReplicaSet is a direct dependent in its own
    right (through its ownerReference), and *both* further routes from it to
    the same Pod - the Pod's own `metadata.ownerReferences` and the
    ReplicaSet's own `spec.selector`, which a real ReplicaSet summary
    carries too - are folded into `additional known paths` because the Pod
    was already reached. That count is rendered `2 or more` rather than `2`:
    the snapshot's coverage is incomplete (the Gateway API group is never
    listed here), so every cluster-derived count in this advisory is a
    lower bound, not a tally.

    The two unconditional limitation lines are asserted here, not only in
    the renderer's unit tests: they are the part of the advisory that no
    cluster state can produce, so nothing else would notice if the flow
    stopped delivering them to the dialog.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=_scale_down_rows())
    async with env.app.run_test() as pilot:
        await _scale_to_one(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert "scale down apps/Deployment/prod/web" in text
        assert _PDB_LIMITATION in text
        assert _HPA_LIMITATION in text
        # Kind-conditional: a Deployment has no PVC retention policy.
        assert _STS_PVC_LIMITATION not in text
        assert "known direct dependents (may be affected): 2 or more" in text
        assert (
            "Pod/prod/web-abc-1 via managed_by (declared) at"
            " apps/Deployment/prod/web: spec.selector" in text
        )
        assert (
            "apps/ReplicaSet/prod/web-abc via owned_by (declared) at"
            " apps/ReplicaSet/prod/web-abc: metadata.ownerReferences[0]" in text
        )
        assert "known transitive dependents (may be affected): 3 or more" in text
        assert (
            "Service/prod/web via managed_by (declared) at"
            " apps/Deployment/prod/web: spec.selector -> selects (declared) at"
            " Service/prod/web: spec.selector" in text
        )
        assert (
            "discovery.k8s.io/EndpointSlice/prod/web-xyz via managed_by (declared) at"
            " apps/Deployment/prod/web: spec.selector -> routes_to (observed) at"
            " discovery.k8s.io/EndpointSlice/prod/web-xyz: endpoints[0].targetRef" in text
        )
        assert (
            "networking.k8s.io/Ingress/prod/web via managed_by (declared) at"
            " apps/Deployment/prod/web: spec.selector -> selects (declared) at"
            " Service/prod/web: spec.selector -> routes_to (declared)" in text
        )
        assert "additional known paths: 2 or more" in text
        assert "traversal capped" not in text
        assert "may be affected" in text
        assert "will fail" not in text
        assert env.ops.calls == []


async def test_scale_down_of_a_replicaset_follows_the_same_routing_chain(
    tmp_path: Path,
) -> None:
    """The same routing chain reached from the ReplicaSet instead.

    A real ReplicaSet declares its own `spec.selector` exactly as a
    Deployment does, so its Pods are one `managed_by` hop away and the first
    path breadth-first offers is that selector's - evidence
    `apps/ReplicaSet/prod/web-abc: spec.selector` - with the same Pod's
    `metadata.ownerReferences` route to it folded into `additional known
    paths` rather than listed twice. `Pod -> Service (selects) -> Ingress
    (routes_to)` follows exactly as it does from the Deployment: three hops,
    inside `ImpactLimits.max_depth`, so the Ingress is named and nothing is
    withheld behind the traversal cap. The EndpointSlice pointing at that
    same Pod through its observed `endpoints[0].targetRef` is the third
    transitive dependent, reached one hop earlier than the Ingress. Every
    count here is a lower bound (`N or more`) because the snapshot's
    coverage is incomplete, not because anything was capped.

    This pins that the flow hands `ImpactAction.SCALE_DOWN` to the
    summarizer for every scalable kind, not just the one the first fixture
    is written for, and that the full `managed_by -> selects -> routes_to`
    scale path is listed either way. `routes_to` alone is not what sets
    scale-down apart - delete already follows it - it is `selects` that
    is scale-down-specific, since delete deliberately omits it.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=_scale_down_rows())
    async with env.app.run_test() as pilot:
        await _scale_to_one(env, pilot, "rs", expect="web-abc")
        text = impact_text(env.app)
        assert "scale down apps/ReplicaSet/prod/web-abc" in text
        assert "known direct dependents (may be affected): 1 or more" in text
        assert (
            "Pod/prod/web-abc-1 via managed_by (declared) at"
            " apps/ReplicaSet/prod/web-abc: spec.selector" in text
        )
        assert "known transitive dependents (may be affected): 3 or more" in text
        assert (
            "Service/prod/web via managed_by (declared) at"
            " apps/ReplicaSet/prod/web-abc: spec.selector -> selects (declared) at"
            " Service/prod/web: spec.selector" in text
        )
        assert (
            "discovery.k8s.io/EndpointSlice/prod/web-xyz via managed_by (declared) at"
            " apps/ReplicaSet/prod/web-abc: spec.selector -> routes_to (observed) at"
            " discovery.k8s.io/EndpointSlice/prod/web-xyz: endpoints[0].targetRef" in text
        )
        assert (
            "networking.k8s.io/Ingress/prod/web via managed_by (declared) at"
            " apps/ReplicaSet/prod/web-abc: spec.selector -> selects (declared) at"
            " Service/prod/web: spec.selector -> routes_to (declared)" in text
        )
        assert "additional known paths: 1 or more" in text
        assert "traversal capped" not in text
        assert "may be affected" in text
        assert "will fail" not in text
        assert env.ops.calls == []


async def test_scale_down_of_a_statefulset_states_the_pvc_limitation(
    tmp_path: Path,
) -> None:
    """The one limitation line that depends on the target's kind.

    A StatefulSet's `persistentVolumeClaimRetentionPolicy` - not this walk -
    decides whether the removed replicas' claims survive, so the advisory
    says so for a StatefulSet and only for one. Driven through the real `S`
    flow rather than the renderer alone: the kind reaching the summary is
    what selects the line, and that identity travels the whole flow.
    """
    rows: dict[str, list[Any]] = {"statefulsets": [_statefulset()], "pods": [_statefulset_pod()]}
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await _scale_to_one(env, pilot, "sts", expect="db")
        text = impact_text(env.app)
        assert "scale down apps/StatefulSet/prod/db" in text
        assert _PDB_LIMITATION in text
        assert _HPA_LIMITATION in text
        assert _STS_PVC_LIMITATION in text
        assert (
            "Pod/prod/db-0 via managed_by (declared) at apps/StatefulSet/prod/db: spec.selector"
            in text
        )
        assert "will fail" not in text
        assert env.ops.calls == []


async def test_scale_down_impact_timeout_still_states_the_static_limitations(
    tmp_path: Path,
) -> None:
    """A snapshot that never arrived costs the user the cluster-derived
    part of the advisory and nothing else.

    The PDB and HPA lines are not findings about this cluster - they say
    what a controller scale-down does not route through and what this walk
    would not have evaluated anyway - so a timeout that dropped them would
    take away a true statement precisely when korvid has least to offer.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=_scale_down_rows())
    env.lister.delay = 5.0
    with mock.patch("korvid.ui.app._IMPACT_TIMEOUT", 0.01):
        async with env.app.run_test() as pilot:
            await _scale_to_one(env, pilot, "deploy", expect="web")
            text = impact_text(env.app)
            assert "impact unavailable; approval remains available" in text
            assert _PDB_LIMITATION in text
            assert _HPA_LIMITATION in text
            # Still kind-conditional when nothing was read: a Deployment
            # has no PVC retention policy either way.
            assert _STS_PVC_LIMITATION not in text
            assert "known direct dependents" not in text
            assert env.app.screen.query(".confirm-preview")
            assert env.ops.calls == []


async def test_scale_down_loader_failure_keeps_the_statefulset_limitations(
    tmp_path: Path,
) -> None:
    """The same for an unexpected loader failure, on the one kind that adds
    a third line - and the exception's message still never reaches the
    dialog, because it can carry cluster-derived text."""
    rows: dict[str, list[Any]] = {"statefulsets": [_statefulset()], "pods": [_statefulset_pod()]}
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    env.lister.errors["statefulsets"] = RuntimeError("lister exploded on AKIAEXAMPLEPAYLOAD")
    async with env.app.run_test() as pilot:
        await _scale_to_one(env, pilot, "sts", expect="db")
        text = impact_text(env.app)
        assert "impact unavailable; approval remains available" in text
        assert _PDB_LIMITATION in text
        assert _HPA_LIMITATION in text
        assert _STS_PVC_LIMITATION in text
        assert "AKIAEXAMPLEPAYLOAD" not in text
        assert "known direct dependents" not in text
        assert env.app.screen.query(".confirm-preview")
        assert env.ops.calls == []


@pytest.mark.parametrize("key", ["ctrl+d", "r"])
async def test_delete_and_restart_keep_the_generic_unavailable_advisory_verbatim(
    tmp_path: Path, key: str
) -> None:
    """The other actions' unavailable advisory is unchanged by #295's
    kind-aware fallback: the generic line and nothing else, with no
    scale-down limitation attached to a delete or a rollout restart."""
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=_scale_down_rows())
    env.lister.errors["deployments"] = RuntimeError("parser exploded")
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press(key)
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="confirm dialog opened"
        )
        text = impact_text(env.app)
        assert "impact unavailable; approval remains available" in text
        assert "parser exploded" not in text
        assert _PDB_LIMITATION not in text
        assert _HPA_LIMITATION not in text
        assert _STS_PVC_LIMITATION not in text
        assert env.ops.calls == []


@pytest.mark.parametrize(
    ("desired", "requested"),
    [(3, 5), (3, 3), (None, 1)],
)
async def test_non_decreasing_or_unknown_scale_never_loads_relationships(
    tmp_path: Path,
    desired: int | None,
    requested: int,
) -> None:
    """Only a *known decrease* has scale-down semantics: scaling up, scaling
    to the same count, and a row whose desired count korvid cannot read get
    no impact section and no snapshot fan-out at all.

    That fan-out is the only thing #295 leaves untouched for them. The
    identity gating around every scale is *strengthened*: the captured uid,
    the origin pane and that pane's scope are revalidated after the
    permission check and again after the dry-run preview, where the flow
    previously rechecked only kind, namespace, name and the context epoch.
    """
    rows: dict[str, list[Any]] = {"deployments": [_deployment("web", "deploy-1", desired=desired)]}
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(
            pilot, lambda: isinstance(env.app.screen, ReplicasPrompt), label="replicas prompt"
        )
        for char in str(requested):
            await pilot.press(char)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="scale confirm")
        assert not env.app.screen.query(".confirm-impact")
        assert env.lister.calls == []


async def test_cordon_dialog_has_no_impact_section(tmp_path: Path) -> None:
    """A second unsupported flow, on a cluster-scoped kind: the delivery
    boundary is delete, rollout restart and scale-down, and everything else
    stays exactly as it was (see the roadmap deviation in Global
    Constraints)."""
    rows: dict[str, list[Any]] = {"nodes": [_node()]}
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "nodes", expect="worker-1")
        await pilot.press("c")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="cordon confirm"
        )
        assert not env.app.screen.query(".confirm-impact")
        assert env.lister.calls == []
        assert env.ops.calls == []


async def test_context_switch_during_the_impact_load_aborts_before_the_dialog(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app

    def bump_epoch() -> None:
        app._ctx_epoch += 1

    env.lister.on_first_call = bump_epoch
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await app.action_delete_resource()
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        # `notify` reaches `_notifications` through the message loop, so the
        # refusal banner is polled for rather than read straight after the
        # await (the repo-wide pattern for notification assertions).
        await until(
            pilot,
            lambda: any(
                "the kube context changed during the impact summary" in n.message
                for n in app._notifications
            ),
            label="impact-summary context refusal",
        )


async def test_selection_change_during_the_impact_load_aborts_before_the_dialog(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app

    def move_cursor() -> None:
        app.query_one(ResourceTable).move_cursor(row=1)

    env.lister.on_first_call = move_cursor
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        # Row 1 is the second deployment: moving there during the load
        # means the dialog would describe a row the user is not on.
        assert str(app.query_one(ResourceTable).get_row_at(1)[0]) == "zz-api"
        await app.action_delete_resource()
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        await until(
            pilot,
            lambda: any(
                "the selection changed during the impact summary" in n.message
                for n in app._notifications
            ),
            label="impact-summary selection refusal",
        )


def _replace_selected_row_with_a_new_incarnation(app: KorvidApp) -> None:
    """Swap the selected `web` row for a same-named object with another uid.

    Exactly what a delete-and-recreate between the two awaits looks like on
    screen: identical kind, namespace and name, so every identity check
    except the uid still passes.
    """
    app.store.apply_event(
        app.current_kind, app.current_scope, "MODIFIED", _deployment("web", "deploy-9")
    )


def _rescale_the_selected_row(app: KorvidApp, desired: int | None) -> None:
    """Change the selected `web` row's desired count, keeping its identity.

    What a controller, an autoscaler or another operator does between two
    awaits: the object is the same incarnation - same kind, namespace, name
    and uid - so every identity check still passes, while the number the
    scale flow captured (and classified the request against) is stale.
    """
    app.store.apply_event(
        app.current_kind,
        app.current_scope,
        "MODIFIED",
        _deployment("web", "deploy-1", desired=desired),
    )


async def _count_refusal(app: KorvidApp, pilot: Any, phase: str, label: str) -> None:
    """Wait for the replica-count drift refusal a scale gate raises after `phase`."""
    await _cancelled(app, pilot, f"the desired replica count changed during {phase}", label)


async def test_same_name_replacement_during_the_impact_load_aborts_the_delete(
    tmp_path: Path,
) -> None:
    """The row the user pressed Ctrl-D on is replaced under the same name
    while the snapshot loads. The dialog would name `web` and the summary
    would describe `web`, but the write pins the uid the user saw - so the
    approval could only ever 409, and the text would describe an object the
    user never selected. Refuse before the dialog exists."""
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    env.lister.on_first_call = lambda: _replace_selected_row_with_a_new_incarnation(app)
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await app.action_delete_resource()
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        await until(
            pilot,
            lambda: any(
                "the selection changed during the impact summary" in n.message
                for n in app._notifications
            ),
            label="impact-summary uid refusal",
        )


async def test_same_name_replacement_during_the_impact_load_aborts_the_restart(
    tmp_path: Path,
) -> None:
    """Same guard on the `r` flow - see the delete case above."""
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    env.lister.on_first_call = lambda: _replace_selected_row_with_a_new_incarnation(app)
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await app.action_rollout_restart()
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        await until(
            pilot,
            lambda: any(
                "the selection changed during the impact summary" in n.message
                for n in app._notifications
            ),
            label="impact-summary uid refusal",
        )


async def test_a_row_that_loses_its_uid_during_the_impact_load_aborts_the_delete(
    tmp_path: Path,
) -> None:
    """The approved incarnation stops being verifiable mid-flow.

    The row keeps its kind, namespace and name but the store's copy no
    longer carries a uid - korvid can no longer prove the object on screen
    is the one the user pressed Ctrl-D on. That is indistinguishable from a
    replacement from here, so the check fails *closed*: treating "no current
    uid" as "nothing to compare" would turn the exact-incarnation guarantee
    into a guarantee only for rows that happen to still resolve.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app

    def drop_the_uid() -> None:
        app.store.apply_event(
            app.current_kind, app.current_scope, "MODIFIED", _deployment("web", "")
        )

    env.lister.on_first_call = drop_the_uid
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await app.action_delete_resource()
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        await until(
            pilot,
            lambda: any(
                "the selection changed during the impact summary" in n.message
                for n in app._notifications
            ),
            label="impact-summary uid refusal",
        )


async def test_scale_down_uid_loss_during_impact_load_aborts_before_confirmation(
    tmp_path: Path,
) -> None:
    """Same fail-closed uid gate on the `S` flow - see the delete case above.

    The scale-down's own gap is the widest of the three: the count prompt
    sits between the RBAC check and the snapshot, so the approved
    incarnation has had a whole modal's lifetime to be replaced.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app

    def drop_the_uid() -> None:
        app.store.apply_event(
            app.current_kind, app.current_scope, "MODIFIED", _deployment("web", "")
        )

    env.lister.on_first_call = drop_the_uid
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        await pilot.press("1")
        await pilot.press("enter")
        await _refusal(app, pilot, "scale-down uid refusal")
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []


async def test_a_row_without_a_uid_opens_the_dialog_with_no_impact_section(
    tmp_path: Path,
) -> None:
    """A summary type that carries no uid gets no impact section at all.

    The summary is keyed on the exact identity the write targets, uid
    included: without one there is nothing to match a snapshot node against.
    Resolving the target by name instead would silently reconnect the
    dialog to whatever object currently holds that name - the same
    reconnection `GraphResource` refuses for an unresolved reference - and
    keeping the uid-less identity would render `target not found in this
    snapshot` for a row that is plainly on screen, which reads as "this
    object is gone" rather than "korvid has no uid for it". So the preview
    is omitted, the snapshot is never even loaded (no LIST fan-out for an
    answer that could not be trusted), and the approval flow is exactly what
    it was before issue #283.
    """
    env = ImpactEnv(
        tmp_path / "audit.jsonl",
        rows={
            "deployments": [_deployment("web", ""), _deployment("zz-api", "deploy-2")],
            "replicasets": [_replicaset()],
        },
    )
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert not env.app.screen.query(".confirm-impact")
        assert env.app.screen.query(".confirm-preview")
        assert env.lister.calls == []
        assert env.ops.calls == []


async def test_impact_loads_after_the_permission_check_and_the_dry_run_preview(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert env.order[:3] == ["rbac", "preview", "list"]


async def _split_workspace(app: KorvidApp, pilot: Any) -> None:
    """`ctrl+w v`: clone the focused view into a second pane (issue #48)."""
    await pilot.press("ctrl+w", "v")
    await until(pilot, lambda: len(app.query(ResourceTable)) == 2, label="second pane mounted")


async def _pane_to_view(pilot: Any, view: str, table_id: str, *, expect: str) -> None:
    """`to_view` for a split workspace: waits on one named pane's table.

    `to_view` queries *the* `ResourceTable`, which is ambiguous once a
    second pane is mounted.
    """
    await pilot.press("colon")
    for ch in view:
        await pilot.press("space" if ch == " " else ch)
    await pilot.press("enter")

    def ready() -> bool:
        table = pilot.app.query_one(f"#{table_id}", ResourceTable)
        return table.row_count > 0 and str(table.get_row_at(0)[0]) == expect

    await until(pilot, ready, label=f"{view} rows visible in {table_id}")


async def _prod_origin_beside_an_all_namespaces_pane(app: KorvidApp, pilot: Any) -> None:
    """Two panes on the same Deployment: pane 0 (focused) scoped to `prod`,
    pane 1 scoped to every namespace with its cursor on the same `prod/web`
    row - same object, same uid, different scope.

    The configuration a pane-blind guard cannot tell apart: every check that
    reads "the focused pane" (kind, namespace, name, uid) still passes after
    focus moves across, while the snapshot's scope - and therefore what
    `graph coverage: complete` claims - is not the one the user acted in.
    """
    await to_view(pilot, "deploy", expect="web")
    await _split_workspace(app, pilot)
    await _pane_to_view(pilot, "deploy all", "pane-1", expect="prod")
    assert str(app.query_one("#pane-1", ResourceTable).get_row_at(0)[1]) == "web"
    await pilot.press("ctrl+w", "w")
    await until(pilot, lambda: app._focused_pane == 0, label="focus back on the prod pane")
    assert app.current_scope == "prod"


#: Every namespaced source in the snapshot catalog. A cluster-scoped source
#: (Node, PersistentVolume) is always LISTed cluster-wide, so its `None`
#: namespace says nothing about the snapshot's scope.
_NAMESPACED_PLURALS = frozenset(meta.plural for meta in CATALOG_METAS if meta.namespaced)


def _namespaced_list_scopes(env: ImpactEnv) -> set[str | None]:
    """The namespaces the snapshot LISTed its namespaced sources in."""
    return {namespace for plural, namespace in env.lister.calls if plural in _NAMESPACED_PLURALS}


async def _cancelled(app: KorvidApp, pilot: Any, message: str, label: str) -> None:
    """Wait for a gate's cancellation banner.

    `notify` reaches `_notifications` through the message loop, so the
    banner is polled for rather than read straight after the await (the
    repo-wide pattern for notification assertions).
    """
    await until(
        pilot,
        lambda: any(message in n.message for n in app._notifications),
        label=label,
    )


async def _refusal_during(app: KorvidApp, pilot: Any, phase: str, label: str) -> None:
    """Wait for the selection-changed refusal a gate raises after `phase`."""
    await _cancelled(app, pilot, f"the selection changed during {phase}", label)


async def _refusal(app: KorvidApp, pilot: Any, label: str) -> None:
    await _refusal_during(app, pilot, "the impact summary", label)


async def test_focus_moving_to_another_pane_during_the_impact_load_aborts_the_delete(
    tmp_path: Path,
) -> None:
    """The user pressed Ctrl-D in the `prod` pane; focus lands in the
    all-namespaces pane while the snapshot loads.

    The other pane's cursor is on the *same* object with the same uid, so
    kind, namespace, name and uid all still match - yet the dialog would now
    be an approval raised from one pane and answered in another, hedged by a
    summary whose scope was chosen for the pane the user left. Refuse before
    the dialog exists.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        origin = app._pane
        env.lister.on_first_call = app._focus_other_pane
        await app.action_delete_resource()
        assert app._pane is not origin
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        # The snapshot still covered the pane the write was raised from.
        assert _namespaced_list_scopes(env) == {"prod"}
        await _refusal(app, pilot, "impact-summary pane refusal")


async def test_focus_moving_to_another_pane_during_the_impact_load_aborts_the_restart(
    tmp_path: Path,
) -> None:
    """Same guard on the `r` flow - see the delete case above."""
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        origin = app._pane
        env.lister.on_first_call = app._focus_other_pane
        await app.action_rollout_restart()
        assert app._pane is not origin
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        assert _namespaced_list_scopes(env) == {"prod"}
        await _refusal(app, pilot, "impact-summary pane refusal")


async def test_the_snapshot_scope_follows_the_pane_the_write_was_raised_from(
    tmp_path: Path,
) -> None:
    """Focus moves to the all-namespaces pane during the *dry-run*, i.e.
    before the snapshot is loaded at all.

    Reading the scope off whichever pane is focused by then would list every
    namespace and then state `scope: all namespaces` for a write the user
    raised in `prod`. The captured pane decides, and the moved focus still
    cancels the flow afterwards.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        env.ops.on_first_preview = app._focus_other_pane
        await app.action_delete_resource()
        assert env.lister.calls != []
        assert _namespaced_list_scopes(env) == {"prod"}
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        await _refusal(app, pilot, "impact-summary pane refusal")


async def test_a_scope_change_on_the_origin_pane_during_the_impact_load_aborts_the_delete(
    tmp_path: Path,
) -> None:
    """Focus never moves: the pane the user acted in re-scopes itself to
    every namespace while the snapshot loads.

    The row, its uid and the pane object are all unchanged, so only the
    scope betrays it - and a `prod` snapshot's `graph coverage: complete`
    would then hedge a dialog raised over a cluster-wide view.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        origin = app._pane

        def widen_the_origin_pane() -> None:
            origin.scope = ALL_NAMESPACES

        env.lister.on_first_call = widen_the_origin_pane
        await app.action_delete_resource()
        assert app._pane is origin
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        assert _namespaced_list_scopes(env) == {"prod"}
        await _refusal(app, pilot, "impact-summary scope refusal")


async def test_scale_down_focus_move_to_same_object_aborts_before_confirmation(
    tmp_path: Path,
) -> None:
    """The `S` flow's pane gate - see the delete case above.

    Focus lands in the all-namespaces pane, whose cursor is on the very same
    object with the same uid, while the scale-down snapshot loads. The
    snapshot still covered the pane the count was entered in, and the
    approval never appears.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        origin = app._pane
        env.lister.on_first_call = app._focus_other_pane
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        await pilot.press("1")
        await pilot.press("enter")
        await _refusal(app, pilot, "scale-down pane refusal")
        assert app._pane is not origin
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        assert _namespaced_list_scopes(env) == {"prod"}


async def test_scale_down_scope_change_on_origin_aborts_before_confirmation(
    tmp_path: Path,
) -> None:
    """The `S` flow's scope gate - see the delete case above. Focus never
    moves: the pane the count was entered in widens to every namespace while
    the scale-down snapshot loads."""
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        origin = app._pane

        def widen_the_origin_pane() -> None:
            origin.scope = ALL_NAMESPACES

        env.lister.on_first_call = widen_the_origin_pane
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        await pilot.press("1")
        await pilot.press("enter")
        await _refusal(app, pilot, "scale-down scope refusal")
        assert app._pane is origin
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        assert _namespaced_list_scopes(env) == {"prod"}


async def test_scale_down_focus_move_while_the_prompt_is_open_aborts_before_any_list(
    tmp_path: Path,
) -> None:
    """Focus crosses to the all-namespaces pane while the replica-count
    prompt is still up, i.e. *before* the count is even submitted.

    The count prompt is the scale flow's own awaited gap, and it is the one
    a user can hold open indefinitely. The pane the write was raised from is
    captured before that prompt is pushed, so the approval that follows is
    still judged against `prod`: the gate refuses, no dialog appears, and no
    snapshot is loaded at all - in particular never the cluster-wide one the
    now-focused pane would ask for. Capturing the origin when the count comes
    back instead would silently adopt the pane the user drifted into and let
    the write through.

    The refusal names the prompt, not the dry run: the gate that sees this
    drift is the one at the top of the confirm step, before the dry-run
    round trip is even issued, so the drift costs no API call either.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        origin = app._pane
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        app._focus_other_pane()
        await until(pilot, lambda: app._pane is not origin, label="focus on the other pane")
        assert isinstance(app.screen, ReplicasPrompt)
        await pilot.press("1")
        await pilot.press("enter")
        await _refusal_during(
            app, pilot, "the replica count prompt", "scale-down prompt-gap refusal"
        )
        assert app._pane is not origin
        assert len(app.screen_stack) == 1
        assert not isinstance(app.screen, ConfirmScreen)
        assert env.ops.calls == []
        # Neither the dry run nor the snapshot ran: the permission check is
        # the only round trip a doomed flow pays for.
        assert env.order == ["rbac"]
        # The snapshot never widened past the captured `prod` origin - here
        # it never ran, because the gate below refuses first.
        assert env.lister.calls == []


#: How a test drifts the identity a scale flow captured, while the flow is
#: awaiting something. Each returns the callable a hook fires; each is
#: invisible to the checks `_precheck_keybinding_write` already makes (kind,
#: namespace, name and the context epoch all still match afterwards), so
#: only the scale flow's own post-permission gate can see it.
def _uid_replacement_drift(app: KorvidApp) -> Callable[[], None]:
    """The selected row is replaced by a same-named object with a new uid."""
    return lambda: _replace_selected_row_with_a_new_incarnation(app)


def _focus_drift(app: KorvidApp) -> Callable[[], None]:
    """Focus lands in the second pane, whose cursor is on the same object."""
    return app._focus_other_pane


def _scope_drift(app: KorvidApp) -> Callable[[], None]:
    """The pane the write was raised from widens to every namespace."""
    origin = app._pane

    def widen() -> None:
        origin.scope = ALL_NAMESPACES

    return widen


def _replica_count_drift(app: KorvidApp) -> Callable[[], None]:
    """The row's desired count changes under the same incarnation."""
    return lambda: _rescale_the_selected_row(app, 1)


@pytest.mark.parametrize(
    ("make_drift", "reason"),
    [
        (_uid_replacement_drift, "the selection changed"),
        (_focus_drift, "the selection changed"),
        (_scope_drift, "the selection changed"),
        (_replica_count_drift, "the desired replica count changed"),
    ],
    ids=["uid", "pane", "scope", "replicas"],
)
async def test_scale_identity_drift_during_the_permission_check_never_prompts(
    tmp_path: Path,
    make_drift: Callable[[KorvidApp], Callable[[], None]],
    reason: str,
) -> None:
    """The scale flow's *first* awaited gap: the SubjectAccessReview.

    `_precheck_keybinding_write` re-validates kind, namespace, name and the
    context epoch after that round trip, and all four still match in each
    case here - a same-named replacement keeps them, and so does a second
    pane sitting on the very same row, the origin pane widening its scope,
    or the row's desired count moving under an unchanged identity. Only the
    uid, the pane identity, that pane's scope and the captured replica count
    betray the drift, which is exactly what the gate added after the
    permission check compares.

    Without it the flow would run on and push the replica prompt: a modal
    the user never asked for, over an object they no longer selected (or a
    count they no longer saw), whose value would then be dry-run,
    summarized and offered for approval. So the assertion is that *nothing*
    happens after the refusal - no prompt, no dry-run preview, no snapshot
    LIST, no write, no audit record.
    """
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        env.on_permission_check = make_drift(app)
        await pilot.press("S")
        await _cancelled(
            app, pilot, f"{reason} during the permission check", "scale permission-gap refusal"
        )
        assert not isinstance(app.screen, ReplicasPrompt)
        assert len(app.screen_stack) == 1
        # The permission check is the only thing that ran: no dry-run
        # preview and no relationship LIST followed it.
        assert env.order == ["rbac"]
        assert env.lister.calls == []
        assert env.ops.calls == []
        assert not audit_path.exists()


async def test_scale_down_focus_move_during_the_dry_run_never_loads_relationships(
    tmp_path: Path,
) -> None:
    """Focus crosses panes inside the dry-run round trip, after the count
    was submitted from the `prod` pane.

    The impact snapshot is a LIST fan-out across every source in the
    catalog; once the flow is already doomed there is no reason to ask the
    API server for it. The gate sits between the dry run and the snapshot,
    so the drift costs zero relationship LISTs, and the approval still never
    appears.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        origin = app._pane
        env.ops.on_first_preview = app._focus_other_pane
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        await pilot.press("1")
        await pilot.press("enter")
        await _refusal_during(app, pilot, "the dry-run preview", "scale-down dry-run refusal")
        assert app._pane is not origin
        assert len(app.screen_stack) == 1
        assert not isinstance(app.screen, ConfirmScreen)
        assert env.ops.calls == []
        assert env.lister.calls == []


async def test_scale_down_context_switch_during_the_dry_run_never_loads_relationships(
    tmp_path: Path,
) -> None:
    """The same gate on the context axis: the cluster context changes while
    the dry run is in flight. The captured epoch no longer matches, so the
    snapshot - which would be loaded against the *new* context's client -
    is never requested."""
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app

    def bump_epoch() -> None:
        app._ctx_epoch += 1

    env.ops.on_first_preview = bump_epoch
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        await pilot.press("1")
        await pilot.press("enter")
        await _cancelled(
            app,
            pilot,
            "the kube context changed during the dry-run preview",
            "scale-down epoch refusal",
        )
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        assert env.lister.calls == []


async def test_scale_down_replica_drift_during_the_prompt_never_previews_or_lists(
    tmp_path: Path,
) -> None:
    """The count the scale-down was classified against moves while the
    replica prompt is open.

    The row keeps its kind, namespace, name and uid - a controller or an
    autoscaler simply changed `spec.replicas` - so every identity check
    still passes. The flow captured 3 replicas before the permission check
    and the user asks for 2, which was a decrease *then*; by the time the
    count comes back the object sits at 1 and the very same request is an
    increase. Continuing would run a scale-down's blast-radius fan-out for
    a scale-up and offer an approval reading `replicas 3 -> 2` for an
    object at 1. The gate at the top of the confirm step refuses first, so
    the drift costs no dry run and no LIST.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        _rescale_the_selected_row(app, 1)
        await pilot.press("2")
        await pilot.press("enter")
        await _count_refusal(app, pilot, "the replica count prompt", "prompt-gap replica refusal")
        assert len(app.screen_stack) == 1
        assert not isinstance(app.screen, ConfirmScreen)
        assert env.order == ["rbac"]
        assert env.lister.calls == []
        assert env.ops.calls == []


async def test_scale_down_replica_drift_during_the_dry_run_never_loads_relationships(
    tmp_path: Path,
) -> None:
    """The same drift inside the dry-run round trip.

    The snapshot is a LIST fan-out across every source in the catalog and
    it is only ever loaded for a *known decrease*; once the count it was
    decided from is stale there is nothing to spend it on, so the gate
    between the dry run and the snapshot refuses and no relationship LIST
    is issued at all.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    env.ops.on_first_preview = lambda: _rescale_the_selected_row(app, 1)
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        await pilot.press("2")
        await pilot.press("enter")
        await _count_refusal(app, pilot, "the dry-run preview", "dry-run replica refusal")
        assert len(app.screen_stack) == 1
        assert not isinstance(app.screen, ConfirmScreen)
        assert env.lister.calls == []
        assert env.ops.calls == []


async def test_scale_down_replica_drift_during_the_impact_load_aborts_before_confirmation(
    tmp_path: Path,
) -> None:
    """The widest window of all: the drift lands inside the snapshot load.

    The summary was already scoped, walked and rendered as a *scale-down*
    of a workload at 3 replicas. Pushing the dialog now would state
    `replicas 3 -> 2` and hang a scale-down's dependent set off a request
    that is an increase against the object as it stands. The classification
    must never flip silently behind an approval, so the last gate before
    the dialog refuses and no confirmation is ever shown.
    """
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    env.lister.on_first_call = lambda: _rescale_the_selected_row(app, 1)
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        await pilot.press("2")
        await pilot.press("enter")
        await _count_refusal(app, pilot, "the impact summary", "impact-summary replica refusal")
        assert len(app.screen_stack) == 1
        assert not isinstance(app.screen, ConfirmScreen)
        assert env.ops.calls == []


async def test_a_count_appearing_mid_flow_never_turns_an_unknown_scale_into_a_decrease(
    tmp_path: Path,
) -> None:
    """The `None` end of the same invariant.

    A summary that carries no desired count is not a decrease: korvid
    cannot tell one from an increase, so the flow loads no snapshot and the
    dialog says `replicas ? -> 2`. If a count appears during the dry run,
    that captured `None` is exactly as stale as a captured number would be
    - the request is now a known decrease with no blast-radius section and
    a `?` where the approver would read 5. `None` is a captured value, not
    a licence to skip the comparison, so the same gate refuses.
    """
    rows: dict[str, list[Any]] = {"deployments": [_deployment("web", "deploy-1", desired=None)]}
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    app = env.app
    env.ops.on_first_preview = lambda: _rescale_the_selected_row(app, 5)
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        await pilot.press("2")
        await pilot.press("enter")
        await _count_refusal(app, pilot, "the dry-run preview", "unknown-count drift refusal")
        assert len(app.screen_stack) == 1
        assert not isinstance(app.screen, ConfirmScreen)
        assert env.lister.calls == []
        assert env.ops.calls == []


@pytest.mark.parametrize(
    ("make_drift", "reason"),
    [
        (_uid_replacement_drift, "the selection changed"),
        (_focus_drift, "the selection changed"),
        (_scope_drift, "the selection changed"),
        (_replica_count_drift, "the desired replica count changed"),
    ],
    ids=["uid", "pane", "scope", "replicas"],
)
async def test_scale_drift_while_the_confirmation_is_open_never_writes(
    tmp_path: Path,
    make_drift: Callable[[KorvidApp], Callable[[], None]],
    reason: str,
) -> None:
    """The last awaited gap of all: the approval dialog itself.

    `ConfirmScreen` is the longest gap in the whole flow - it stays up until
    the user answers, which can be minutes - and until now nothing was
    re-checked across it. Every earlier gate had already passed, so a
    same-UID replica move, a same-named replacement, a focus change to the
    second pane or a re-scope of the origin pane made *while the dialog was
    on screen* was carried straight into `_run_write`: the dialog the user
    approved would have described `replicas 3 -> 1` (or the row they were
    looking at), and the write, the reservation and the audit record would
    have been for what the cluster and the view hold now.

    The guard runs after a *fresh* keystroke approval and after the modal is
    gone - never as a second approval path, never on a decline - and before
    anything else the approval triggers. So the assertion is that the
    refusal names the dialog phase and that the flow left nothing behind:
    no write reservation (which would block `:ctx`), no operation, and no
    audit record at all - not even the fail-closed intent line, because the
    write worker is never constructed.
    """
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        drift = make_drift(app)
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt")
        await pilot.press("1")
        await pilot.press("enter")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="scale-down confirm"
        )
        assert "scale down apps/Deployment/prod/web" in impact_text(app)
        drift()
        await pilot.press("y")
        await _cancelled(
            app, pilot, f"{reason} during the confirmation dialog", "confirm-gap refusal"
        )
        assert len(app.screen_stack) == 1
        assert not isinstance(app.screen, ConfirmScreen)
        assert app._active_cluster_writes == 0
        assert env.ops.calls == []
        assert not audit_path.exists()
