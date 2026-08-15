"""Graph-derived impact previews in the delete/rollout-restart flows (#283).

The app reuses the relationship snapshot loader it already owns for `g`: no
new LIST/GET interface, no new constructor parameter, no composition-root
change. What this module pins beyond "the section renders" is the wiring
that only shows up end to end: the snapshot's scope is chosen by the target
(cluster-scoped kinds cover every namespace), the row's exact UID reaches
the summary, unsupported write flows are untouched, and both awaited gaps
still revalidate. This module owns the shared harness (`ImpactEnv`) that
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
from korvid.core.store import ResourceStore, Summary
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


def _deployment(name: str, uid: str) -> GenericSummary:
    return GenericSummary(
        name=name, namespace="prod", kind="Deployment", created="", desired=3, uid=uid
    )


def _replicaset() -> GenericSummary:
    return GenericSummary(
        name="web-abc",
        namespace="prod",
        kind="ReplicaSet",
        created="",
        uid="rs-1",
        relationships=RelationshipFacts(
            api_group="apps",
            references=(_owner("Deployment", "web", "deploy-1", group="apps"),),
        ),
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
        self.errors: dict[str, Exception] = {}
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
        return [f"- {meta.plural} prod/{name}"]

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
        return ["~ spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt"]


class ImpactEnv:
    """App plus recording fakes for the impact-preview integration path."""

    def __init__(
        self,
        audit_path: Path,
        *,
        with_lister: bool = True,
        rows: dict[str, list[Any]] | None = None,
        list_rows: dict[str, list[Any]] | None = None,
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
            return True

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
            "apps/ReplicaSet/prod/web-abc via owned_by (declared) at metadata.ownerReferences[0]"
            in text
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
            "Pod/prod/web-abc-1 uses_config -> ConfigMap/prod/app-config (missing)"
            " at spec.volumes[0].configMap" in text
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
        assert "Pod/prod/web-1 via scheduled_on (observed) at spec.nodeName" in text
        assert "Pod/staging/api-1 via scheduled_on (observed) at spec.nodeName" in text
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


async def test_scale_dialog_has_no_impact_section(tmp_path: Path) -> None:
    """Only delete and rollout restart have tested action semantics."""
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(
            pilot, lambda: isinstance(env.app.screen, ReplicasPrompt), label="replicas prompt"
        )
        await pilot.press("5")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="scale confirm")
        assert not env.app.screen.query(".confirm-impact")
        assert env.lister.calls == []


async def test_cordon_dialog_has_no_impact_section(tmp_path: Path) -> None:
    """A second unsupported flow, on a cluster-scoped kind: the delivery
    boundary is delete/rollout restart, and everything else stays exactly
    as it was (see the roadmap deviation in Global Constraints)."""
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


async def test_impact_loads_after_the_permission_check_and_the_dry_run_preview(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert env.order[:3] == ["rbac", "preview", "list"]
