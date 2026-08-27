"""Run the korvid TUI against canned fake data for README GIF recording.

Not shipped with the package — a development harness driven by
``docs/demo/demo.tape`` (VHS). See ``docs/demo/README.md`` for how to
regenerate ``docs/assets/demo.gif``.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import random
import sys
from collections import deque
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from types import ModuleType
from typing import Any

from korvid.agent.runtime import AgentRuntime
from korvid.core.config import KorvidConfig
from korvid.core.mcp import MCPControllerBase
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META, HelmReleaseSummary, release_uid
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, PodListSummary, PodSummary
from korvid.k8s.reads import ReadOps
from korvid.k8s.relationship_facts import (
    RelationKind,
    RelationshipFacts,
    extract_relationship_facts,
)
from korvid.tools.executor import ToolExecutor, UIBridge
from korvid.tools.registry import mcp_tool_schemas
from korvid.tools.structured import ERROR_PREFIX
from korvid.ui.app import AppUIBridge, EventsFetcher, KorvidApp
from korvid.ui.widgets.describe_screen import DescribeScreen

#: The loopback port the `mcp` scene serves and `docs/demo/mcp_client.py`
#: connects to. korvid's own default, so the recorded status line reads the
#: way a real session's does.
MCP_DEMO_PORT = 7878

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_SVC_META = ResourceMeta("Service", "services", "", "v1", True, ("svc",))
_CONFIGMAP_META = ResourceMeta("ConfigMap", "configmaps", "", "v1", True, ("cm",))

ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "po": _PODS_META,
    "pod": _PODS_META,
    "deployments": _DEPLOY_META,
    "deployment": _DEPLOY_META,
    "deploy": _DEPLOY_META,
    "services": _SVC_META,
    "service": _SVC_META,
    "svc": _SVC_META,
}

#: Every remaining kind `RelationshipSnapshotLoader` LISTs for a snapshot.
#:
#: The loader reports each catalog kind the discovery aliases do not offer
#: as `unavailable` before it issues a single LIST, so a four-kind fixture
#: renders a truthful — but wholly unrepresentative — "Coverage: incomplete"
#: panel over fourteen missing kinds. Publishing them here lets
#: `list_relationship_objects` answer with an empty list, which is a
#: complete answer for a synthetic cluster that genuinely has none of them.
#: One Gateway API kind is included because the loader groups "no Gateway
#: API resource was discovered at all" into its own unavailable record.
_RELATIONSHIP_ONLY_METAS: tuple[ResourceMeta, ...] = (
    ResourceMeta("Secret", "secrets", "", "v1", True, ("sec",)),
    ResourceMeta("PersistentVolumeClaim", "persistentvolumeclaims", "", "v1", True, ("pvc",)),
    ResourceMeta("PersistentVolume", "persistentvolumes", "", "v1", False, ("pv",)),
    ResourceMeta("Node", "nodes", "", "v1", False, ("no",)),
    ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True, ("rs",)),
    ResourceMeta("StatefulSet", "statefulsets", "apps", "v1", True, ("sts",)),
    ResourceMeta("DaemonSet", "daemonsets", "apps", "v1", True, ("ds",)),
    ResourceMeta("Job", "jobs", "batch", "v1", True),
    ResourceMeta("CronJob", "cronjobs", "batch", "v1", True, ("cj",)),
    ResourceMeta("EndpointSlice", "endpointslices", "discovery.k8s.io", "v1", True),
    ResourceMeta("Ingress", "ingresses", "networking.k8s.io", "v1", True, ("ing",)),
    ResourceMeta("PodDisruptionBudget", "poddisruptionbudgets", "policy", "v1", True, ("pdb",)),
    ResourceMeta("HTTPRoute", "httproutes", "gateway.networking.k8s.io", "v1", True),
)

RELATIONSHIP_ALIASES: dict[str, ResourceMeta] = dict(ALIASES)
for _alias in ("configmaps", "configmap", "cm"):
    RELATIONSHIP_ALIASES[_alias] = _CONFIGMAP_META
for _meta in _RELATIONSHIP_ONLY_METAS:
    RELATIONSHIP_ALIASES[_meta.plural] = _meta
    for _alias in (_meta.kind.lower(), *_meta.shortnames):
        RELATIONSHIP_ALIASES.setdefault(_alias, _meta)

#: The `mcp` scene's discovery surface. An external host that calls
#: `helm_list_releases` is mirrored onto korvid's Helm browser
#: (`tools/follow.py`), and that mirror is a plain view navigation — so the
#: releases view has to exist here, or the last step of the follow story
#: answers `ERROR: unknown view 'helm'` instead of moving the screen.
MCP_ALIASES: dict[str, ResourceMeta] = dict(ALIASES)
MCP_ALIASES[HELM_RELEASES_META.plural] = HELM_RELEASES_META
for _alias in (HELM_RELEASES_META.kind.lower(), *HELM_RELEASES_META.shortnames):
    MCP_ALIASES.setdefault(_alias, HELM_RELEASES_META)


def _ago(**delta: float) -> str:
    """An RFC 3339 UTC timestamp `delta` before now, to whole seconds.

    Every age in the fixture is relative, so a capture made today and one
    made a year later render identically and no frame bakes in a stale
    calendar date. Whole seconds keep the tool output readable at the
    recording's font size.
    """
    stamp = (datetime.now(UTC) - timedelta(**delta)).replace(microsecond=0)
    return stamp.isoformat()


def _pod(
    name: str,
    ns: str,
    phase: str = "Running",
    ready: str = "1/1",
    restarts: int = 0,
    node: str = "node-1",
    qos: str = "Burstable",
    cpu: str = "100m",
    mem: str = "128Mi",
    *,
    hours: float = 6.0,
    uid: str = "",
    labels: tuple[tuple[str, str], ...] = (),
    relationships: RelationshipFacts | None = None,
) -> PodSummary:
    return PodSummary(
        name=name,
        namespace=ns,
        phase=phase,
        ready=ready,
        restarts=restarts,
        node=node,
        qos=qos,
        cpu_request=cpu,
        mem_request=mem,
        containers=("app",),
        uid=uid,
        labels=labels,
        # Relative, never absolute: the AGE column and every tool answer
        # derived from it read the same way in a capture made today and one
        # made next year, and no frame ever carries a calendar date.
        created=_ago(hours=hours),
        relationships=relationships or RelationshipFacts(),
    )


_PAYMENT_LABELS = (("app", "payment-worker"), ("tier", "backend"))
_PAYMENT_SELECTOR = {"app": "payment-worker"}

POD_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": "payment-worker-6c9f7d-b3xnq",
        "namespace": "shop",
        "labels": {"app": "payment-worker", "tier": "backend"},
    },
    "spec": {
        "containers": [
            {
                "name": "app",
                "image": "registry.example.com/shop/payment-worker:2.4.1",
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "256Mi"},
                },
                "env": [{"name": "PAYMENT_GATEWAY_URL", "value": "https://pay.example.com"}],
            }
        ],
        "volumes": [{"name": "payment-config", "configMap": {"name": "payment-config"}}],
        "nodeName": "node-2",
        "restartPolicy": "Always",
    },
    "status": {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady"}],
        "containerStatuses": [
            {
                "name": "app",
                "ready": False,
                "restartCount": 17,
                "image": "registry.example.com/shop/payment-worker:2.4.1",
                "state": {
                    "waiting": {
                        "reason": "CrashLoopBackOff",
                        "message": "back-off 5m0s restarting failed container=app",
                    }
                },
                "lastState": {
                    "terminated": {
                        "reason": "Error",
                        "exitCode": 1,
                        "message": "payment gateway unreachable: 503 after 3 retries",
                    }
                },
            }
        ],
    },
}

_PAYMENT_RELATIONSHIPS = RelationshipFacts(
    references=tuple(
        reference
        for reference in extract_relationship_facts("Pod", "", "v1", POD_MANIFEST).references
        if reference.relation is RelationKind.USES_CONFIG
    ),
)


PODS = [
    _pod("web-frontend-7d4b9c-x2kfp", "shop", cpu="250m", mem="256Mi", hours=9),
    _pod("web-frontend-7d4b9c-9qwzr", "shop", cpu="250m", mem="256Mi", hours=9),
    _pod("cart-api-5f6d8b-mn4tp", "shop", node="node-2", hours=31),
    _pod("cart-api-5f6d8b-kd82v", "shop", node="node-3", hours=31),
    _pod(
        "payment-worker-6c9f7d-b3xnq",
        "shop",
        phase="CrashLoopBackOff",
        ready="0/1",
        restarts=17,
        node="node-2",
        hours=5,
        uid="pod-payment",
        labels=_PAYMENT_LABELS,
        relationships=_PAYMENT_RELATIONSHIPS,
    ),
    _pod("checkout-svc-84c5d6-ln7wk", "shop", restarts=2, hours=54),
    _pod(
        "inventory-db-0", "shop", qos="Guaranteed", cpu="500m", mem="1Gi", node="node-3", hours=792
    ),
    _pod("search-indexer-59b8c7-tq5mz", "shop", phase="Pending", ready="0/1", node="-", hours=0.2),
    _pod("grafana-7b5c9d-w8xkp", "monitoring", node="node-1", hours=430),
    _pod(
        "prometheus-0", "monitoring", qos="Guaranteed", cpu="1", mem="2Gi", node="node-2", hours=430
    ),
    _pod("loki-0", "monitoring", node="node-3", hours=430),
    _pod("coredns-5d78c9b4-fh2mx", "kube-system", qos="Guaranteed", hours=1080),
    _pod("coredns-5d78c9b4-pk9vz", "kube-system", qos="Guaranteed", node="node-2", hours=1080),
    _pod("metrics-server-6f89b5-jw3qd", "kube-system", node="node-3", hours=1080),
]


def _deploy(name: str, ns: str, desired: int, days: int) -> GenericSummary:
    created = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    return GenericSummary(
        name=name, namespace=ns, kind="Deployment", created=created, desired=desired
    )


def _svc(
    name: str,
    ns: str,
    days: int,
    *,
    uid: str = "",
    selector: dict[str, str] | None = None,
) -> GenericSummary:
    """One synthetic Service summary.

    A `selector` is run through the product's own
    `extract_relationship_facts` against a real Service manifest shape, so
    the resulting `SELECTS` fact is exactly what korvid would derive from a
    live cluster — the relationship screen then has a genuine dependent
    edge to render, not a hand-written one.
    """
    created = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    facts = RelationshipFacts()
    if selector is not None:
        facts = extract_relationship_facts(
            "Service",
            "",
            "v1",
            {
                "metadata": {"name": name, "namespace": ns},
                "spec": {"selector": dict(selector)},
            },
        )
    return GenericSummary(
        name=name, namespace=ns, kind="Service", created=created, uid=uid, relationships=facts
    )


def _release(
    name: str,
    ns: str,
    days: int,
    *,
    revision: int,
    chart: str,
    app_version: str,
) -> HelmReleaseSummary:
    """One synthetic release at its latest revision.

    Both surfaces that answer for Helm read this fixture: the tool
    (`DemoReadOps.list_helm_releases`, which is what an external MCP host
    receives) and the store the releases view renders. A capture in which
    they disagreed would show korvid following the client onto a screen
    that contradicts the answer the client just printed.
    """
    created = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    return HelmReleaseSummary(
        name=name,
        namespace=ns,
        kind="HelmRelease",
        created=created,
        uid=release_uid(ns, name),
        revision=revision,
        status="deployed",
        chart=chart,
        app_version=app_version,
    )


def _node(name: str, days: int) -> GenericSummary:
    return GenericSummary(name=name, namespace="", kind="Node", created=_ago(days=days))


#: The `shop` namespace's releases: the umbrella chart behind the web,
#: cart, payment, checkout and indexer workloads, and the database chart
#: behind `inventory-db-0`.
HELM_RELEASES: list[HelmReleaseSummary] = [
    _release("shop", "shop", 41, revision=4, chart="shop-0.8.0", app_version="2.4.1"),
    _release(
        "inventory-db",
        "shop",
        33,
        revision=2,
        chart="postgresql-15.5.1",
        app_version="16.2.0",
    ),
]


EXTRA: dict[str, list[GenericSummary]] = {
    "deployments": [
        _deploy("web-frontend", "shop", 2, 41),
        _deploy("cart-api", "shop", 2, 41),
        _deploy("payment-worker", "shop", 1, 12),
        _deploy("checkout-svc", "shop", 1, 33),
        _deploy("search-indexer", "shop", 1, 5),
        _deploy("grafana", "monitoring", 1, 90),
    ],
    "services": [
        _svc("web-frontend", "shop", 41),
        _svc("cart-api", "shop", 41),
        _svc("checkout-svc", "shop", 33),
        _svc(
            "payment-worker",
            "shop",
            12,
            uid="svc-payment",
            selector=dict(_PAYMENT_SELECTOR),
        ),
        _svc("grafana", "monitoring", 90),
        _svc("prometheus", "monitoring", 90),
    ],
    "configmaps": [
        GenericSummary(
            name="payment-config",
            namespace="shop",
            kind="ConfigMap",
            # Relative like every other row: this ConfigMap is listed by
            # `list_objects` and by `list_relationship_objects`, so an empty
            # timestamp would render korvid's "-" placeholder in one AGE cell
            # while its neighbours show real ages.
            created=_ago(hours=12),
            uid="cm-payment",
        )
    ],
    # Internal diagnose enrichment resolves each scheduled pod's node.
    "nodes": [
        _node("node-1", 400),
        _node("node-2", 410),
        _node("node-3", 420),
    ],
    HELM_RELEASES_META.plural: list(HELM_RELEASES),
}


async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
    data: list[Summary] = list(PODS) if kind == "pods" else list(EXTRA.get(kind, []))
    for obj in data:
        if scope == ALL_NAMESPACES or obj.namespace == scope:
            yield ("ADDED", obj)
    while True:
        await asyncio.sleep(3600)


async def list_namespaces() -> list[str]:
    return ["shop", "monitoring", "kube-system", "default"]


DEPLOY_MANIFEST: dict[str, Any] = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {
        "name": "payment-worker",
        "namespace": "shop",
        "labels": {"app": "payment-worker", "tier": "backend"},
    },
    "spec": {
        "replicas": 1,
        "selector": {"matchLabels": {"app": "payment-worker"}},
        "strategy": {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
        },
        "template": {
            "metadata": {"labels": {"app": "payment-worker", "tier": "backend"}},
            "spec": {
                "containers": [
                    {
                        "name": "app",
                        "image": "registry.example.com/shop/payment-worker:2.4.1",
                    }
                ]
            },
        },
    },
    "status": {"replicas": 1, "updatedReplicas": 1, "unavailableReplicas": 1},
}

SVC_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Service",
    "metadata": {
        "name": "payment-worker",
        "namespace": "shop",
        "labels": {"app": "payment-worker"},
    },
    "spec": {
        "type": "ClusterIP",
        "clusterIP": "10.96.114.23",
        "selector": dict(_PAYMENT_SELECTOR),
        "ports": [{"name": "http", "port": 80, "targetPort": 8080, "protocol": "TCP"}],
    },
    "status": {"loadBalancer": {}},
}

CONFIGMAP_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "payment-config", "namespace": "shop"},
    "data": {"gateway": "pay.example.com"},
}

_MANIFESTS: dict[str, dict[str, Any]] = {
    "pods": POD_MANIFEST,
    "deployments": DEPLOY_MANIFEST,
    "services": SVC_MANIFEST,
    "configmaps": CONFIGMAP_MANIFEST,
}

#: Every kind any scene can ask to describe. Discovery is per scene — the
#: relationship scene browses `RELATIONSHIP_ALIASES`, the `mcp` scene
#: `MCP_ALIASES` — but one `get_manifest` answers both, so resolving only
#: the relationship set raised `KeyError: unknown demo kind: helmreleases`
#: the moment the follow story described a release. The relationship
#: entries win the (identical, base-`ALIASES`) overlaps, keeping that
#: scene's fixture untouched. Unknown kinds still raise: describing them
#: as Pods is what this lookup exists to prevent.
MANIFEST_ALIASES: dict[str, ResourceMeta] = {**MCP_ALIASES, **RELATIONSHIP_ALIASES}

#: Longest slice of a requested identity a refusal repeats. `kind`,
#: `namespace` and `name` are all caller-supplied — an external MCP host
#: can ask for an arbitrarily long one — and a failed describe is rendered
#: by the UI, which is what a landing clip publishes. 63 is the DNS-1123
#: label limit a namespace obeys; every identity in this fixture is far
#: shorter, so no answerable request is ever clipped.
_IDENTITY_CLIP = 63

#: The five values `status.phase` can hold. A fixture row's STATUS column
#: may carry a display status kubectl derives instead (`CrashLoopBackOff`),
#: which lives in a container's waiting reason — korvid reads it back from
#: there (`_display_phase`), so putting it in `phase` would describe a pod
#: state the API server never reports.
_POD_PHASES = frozenset({"Pending", "Running", "Succeeded", "Failed", "Unknown"})

#: Either shape a fixture row takes: the pods view's own summary, or the
#: generic one every other kind — Helm releases included — is listed with.
FixtureRow = PodSummary | GenericSummary


def _clipped(value: str) -> str:
    """`value`, shortened to something a failure message can safely carry."""
    return value if len(value) <= _IDENTITY_CLIP else value[:_IDENTITY_CLIP] + "…"


def _fixture_rows(plural: str) -> list[FixtureRow]:
    """Every fixture row of one kind — the rows `list_objects` answers with."""
    rows: list[FixtureRow] = list(PODS) if plural == "pods" else list(EXTRA.get(plural, []))
    return rows


def _matches_namespace(meta: ResourceMeta, row: FixtureRow, namespace: str | None) -> bool:
    return namespace is None or not meta.namespaced or row.namespace == namespace


def _fixture_row(meta: ResourceMeta, namespace: str | None, name: str) -> FixtureRow:
    """The fixture row `namespace/name` names.

    Args:
        meta: The resolved kind metadata.
        namespace: The requested namespace. Cluster-scoped kinds ignore it.
        name: The requested object name.

    Returns:
        The one row the fixture holds for that identity.

    Raises:
        KeyError: if no row matches. Answering anyway is what let
            `get_object` succeed for objects `list_objects` never lists.
    """
    for row in _fixture_rows(meta.plural):
        if row.name == name and _matches_namespace(meta, row, namespace):
            return row
    identity = f"{meta.plural}/{_clipped(namespace or '-')}/{_clipped(name)}"
    raise KeyError(f"unknown demo object: {identity}")


def _describes(base: dict[str, Any], namespace: str | None, name: str) -> bool:
    """Whether the hand-written `base` is the manifest of `namespace/name`.

    Each detailed fixture describes exactly one object — the payment
    worker's pod, its Deployment, its Service and its ConfigMap. Stamping
    another row's name onto one of them is what described a Running pod as
    crashlooping on the payment image, so a base is used only for the
    object it was written for.
    """
    metadata = base.get("metadata") or {}
    if metadata.get("name") != name:
        return False
    return not namespace or metadata.get("namespace") == namespace


def _container_names(pod: PodSummary) -> list[str]:
    """One container name per entry the row's READY cell counts.

    The count comes from the cell rather than from `containers` because
    READY is what korvid renders and what an external host was told; the
    two agree throughout this fixture, and reading the cell is what keeps
    a future row's manifest agreeing with its table row if they stop.
    """
    _ready, _, total = pod.ready.partition("/")
    count = int(total) if total.isdigit() else len(pod.containers)
    names = list(pod.containers)
    return [names[index] if index < len(names) else f"container-{index}" for index in range(count)]


def _pod_resources(pod: PodSummary) -> dict[str, Any]:
    """The row's aggregate requests, plus matching Guaranteed limits."""
    requests = {
        key: value
        for key, value in (("cpu", pod.cpu_request), ("memory", pod.mem_request))
        if value and value != "-"
    }
    if not requests:
        return {}
    resources: dict[str, Any] = {"requests": requests}
    if pod.qos == "Guaranteed":
        # Guaranteed *is* limits equal to requests. Reporting the class
        # beside requests alone would describe a Burstable pod.
        resources["limits"] = dict(requests)
    return resources


def _pod_spec(pod: PodSummary) -> dict[str, Any]:
    """A `spec` stating only what the pods table already shows."""
    containers = [{"name": name} for name in _container_names(pod)]
    spec: dict[str, Any] = {"containers": containers, "restartPolicy": "Always"}
    resources = _pod_resources(pod)
    if resources:
        # The table carries the pod's effective aggregate, not a per-container
        # split. Pod-level resources preserve that fact without inventing one.
        spec["resources"] = resources
    if pod.node and pod.node != "-":
        # "-" is korvid's placeholder for an unscheduled pod, and a real
        # manifest says exactly that by carrying no `nodeName` at all.
        spec["nodeName"] = pod.node
    return spec


def _pod_status(pod: PodSummary) -> dict[str, Any]:
    """A `status` korvid's own parser reads back as the row it came from."""
    ready_text, _, _total = pod.ready.partition("/")
    ready_count = int(ready_text) if ready_text.isdigit() else 0
    waiting = None if pod.phase in _POD_PHASES else pod.phase
    statuses: list[dict[str, Any]] = []
    for index, name in enumerate(_container_names(pod)):
        ready = index < ready_count
        state: dict[str, Any] = {"running": {"startedAt": pod.created}} if ready else {}
        if waiting is not None and not ready:
            state = {"waiting": {"reason": waiting}}
        statuses.append(
            {
                "name": name,
                "ready": ready,
                # korvid sums this list into one RESTARTS cell and the row
                # carries one total, so it sits on the first container
                # rather than being split into counts the fixture has not
                # got.
                "restartCount": pod.restarts if index == 0 else 0,
                "state": state,
            }
        )
    return {
        "phase": pod.phase if pod.phase in _POD_PHASES else "Running",
        "qosClass": pod.qos,
        "conditions": [{"type": "Ready", "status": "True" if _pod_is_ready(pod) else "False"}],
        "containerStatuses": statuses,
    }


def _synthesised_manifest(meta: ResourceMeta, row: FixtureRow) -> dict[str, Any]:
    """A minimal manifest for a fixture row that has no hand-written one.

    Minimal, never contradictory: every fact in it is read from the row the
    tables and the tool answers are built from, so a describe frame and the
    list beside it say the same thing. Only the facts korvid actually
    renders are stated — inventing the rest is what this function exists to
    stop.
    """
    api_version = f"{meta.group}/{meta.version}" if meta.group else meta.version
    metadata: dict[str, Any] = {"creationTimestamp": row.created}
    if row.labels:
        metadata["labels"] = dict(row.labels)
    manifest: dict[str, Any] = {
        "apiVersion": api_version,
        "kind": meta.kind,
        "metadata": metadata,
    }
    if isinstance(row, PodSummary):
        manifest["spec"] = _pod_spec(row)
        manifest["status"] = _pod_status(row)
    elif isinstance(row, HelmReleaseSummary):
        # korvid's HelmRelease is synthetic — derived from Helm's release
        # secret, with no upstream schema — so the manifest reports exactly
        # the four facts the releases browser lists.
        manifest["status"] = {
            "status": row.status,
            "revision": row.revision,
            "chart": row.chart,
            "appVersion": row.app_version,
        }
    elif row.desired is not None:
        manifest["spec"] = {"replicas": row.desired}
    return manifest


async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
    """The manifest of one fixture object.

    The describe path and `DemoReadOps.get_object` — the boundary an
    external MCP host reaches — both answer from here, so this must return
    the object the tables and `list_resources` already describe. Kind and
    object are resolved in that order and neither is invented: answering
    an unknown name used to hand back the crashlooping payment fixture
    under whatever name was asked for, which contradicted the `Running
    1/1` the same frame showed and let two tools reading one fixture
    disagree.

    Args:
        kind: A kind alias from either scene's discovery surface.
        namespace: The requested namespace, or None to match on name alone.
        name: The requested object name.

    Returns:
        The hand-written fixture when one was written for exactly this
        object, and a minimal manifest agreeing with the row's own table
        facts otherwise. Always a fresh deep copy.

    Raises:
        KeyError: if `kind` is not a demo kind, or the fixture holds no
            such object. Both messages carry a clipped identity: a failed
            describe is rendered by the UI and can reach a recorded frame.
    """
    meta = MANIFEST_ALIASES.get(kind)
    if meta is None:
        raise KeyError(f"unknown demo kind: {_clipped(kind)}")
    row = _fixture_row(meta, namespace, name)
    base = _MANIFESTS.get(meta.plural)
    manifest = (
        # Deep, not shallow: the describe screen and `DemoReadOps.get_object`
        # both return these fixtures, so sharing nested branches would let one
        # consumer's in-place edit rewrite every later frame and tool answer.
        copy.deepcopy(base)
        if base is not None and _describes(base, namespace, name)
        else _synthesised_manifest(meta, row)
    )
    manifest["metadata"]["name"] = name
    if meta.namespaced and namespace:
        manifest["metadata"]["namespace"] = namespace
    return manifest


class DemoEvents(EventsFetcher):
    async def fetch(
        self, namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        if name.startswith("payment-worker-"):
            # The crashlooping demo pod.
            return [
                {
                    "type": "Warning",
                    "reason": "BackOff",
                    "lastTimestamp": _ago(minutes=2),
                    "message": "Back-off restarting failed container app in pod " + name,
                    "involvedObject": {"name": name},
                },
                {
                    "type": "Normal",
                    "reason": "Pulled",
                    "lastTimestamp": _ago(minutes=3),
                    "message": 'Container image "payment-worker:2.4.1" already present on machine',
                    "involvedObject": {"name": name},
                },
            ]
        return [
            {
                "type": "Normal",
                "reason": "ScalingReplicaSet",
                "lastTimestamp": _ago(hours=4),
                "message": f"Scaled up replica set {name}-7d4b9c to 2",
                "involvedObject": {"name": name},
            },
        ]


_LOG_TEMPLATES = [
    "INFO  processing payment intent id=pi_{n:06d} amount=79.99 currency=USD",
    "INFO  charge authorized id=ch_{n:06d} latency=42ms",
    "INFO  webhook delivered event=payment.succeeded id=evt_{n:06d}",
    "WARN  retrying gateway call attempt=2 id=pi_{n:06d} (timeout 800ms)",
    "INFO  settlement batch flushed count=48 duration=112ms",
    "ERROR gateway 503 for id=pi_{n:06d}; scheduling retry in 5s",
]


async def stream_logs(
    namespace: str,
    pod: str,
    container: str,
    *,
    previous: bool = False,
    follow: bool = True,
    **_kwargs: Any,
) -> AsyncIterator[LogLine]:
    rng = random.Random(42)
    ts = datetime.now(UTC) - timedelta(seconds=30)
    for _ in range(18):
        ts += timedelta(seconds=rng.uniform(0.5, 2.5))
        text = rng.choice(_LOG_TEMPLATES).format(n=rng.randint(1, 999999))
        yield LogLine(pod=pod, container=container, text=text, timestamp=ts)
    while follow:
        await asyncio.sleep(rng.uniform(0.4, 1.2))
        text = rng.choice(_LOG_TEMPLATES).format(n=rng.randint(1, 999999))
        yield LogLine(pod=pod, container=container, text=text, timestamp=datetime.now(UTC))


def _pod_is_ready(pod: PodSummary) -> bool:
    """Whether korvid would call `pod` Ready.

    One rule, two readers: the `PodListSummary` an external MCP host is
    answered with, and the `status.conditions` of the manifest a describe
    renders. Spelled twice, the two could disagree inside a single frame.
    """
    ready, separator, total = pod.ready.partition("/")
    return (
        pod.phase == "Running"
        and separator == "/"
        and ready.isdigit()
        and total.isdigit()
        and int(total) > 0
        and ready == total
    )


def _pod_list_row(pod: PodSummary) -> PodListSummary:
    """The fixture's pod as the summary type the tool LIST path returns.

    The TUI's pods view streams the richer `PodSummary` through its own
    watch path, while `list_objects` — the boundary `list_resources` reads
    — answers with `PodListSummary` (issue #158). Both are built from the
    one fixture row here, so the external client's answer and korvid's
    table cannot disagree about a pod's status.
    """
    return PodListSummary(
        name=pod.name,
        namespace=pod.namespace,
        kind="Pod",
        created=pod.created,
        uid=pod.uid,
        labels=pod.labels,
        phase=pod.phase,
        ready=pod.ready,
        restarts=pod.restarts,
        node=pod.node or "",
        ready_condition=_pod_is_ready(pod),
    )


async def _tail_stream(source: AsyncIterator[LogLine], tail_lines: int) -> AsyncIterator[LogLine]:
    """The last `tail_lines` lines of a finite log stream, in order.

    The shipped log tools (`get_logs`, and `diagnose_pod`'s excerpt) ask
    this read surface for a bounded, non-following read, and the MCP clip's
    whole claim is that korvid answers such a call *bounded*. Dropping the
    argument made the answer's size a property of the fixture instead, so
    the bound is applied here, over the same synthetic stream.

    Args:
        source: The finite stream to bound.
        tail_lines: How many trailing lines to keep. Zero or less keeps
            none, matching what the API server does with `tailLines=0`.

    Yields:
        The stream's last `tail_lines` lines, oldest first.
    """
    kept: deque[LogLine] = deque(maxlen=max(tail_lines, 0))
    async for line in source:
        kept.append(line)
    for line in kept:
        yield line


class DemoReadOps(ReadOps):
    """The synthetic fixture behind korvid's real agent read surface.

    `ToolExecutor` depends on this ABC rather than on `KubeClient`, so the
    documentation capture can run the *shipped* read tools — `diagnose_pod`
    and `get_logs` gather, redact, bound and cite exactly as they would
    against a cluster; only the bytes they read are synthetic.
    """

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        # Pods cross this boundary as `PodListSummary`, exactly as the real
        # client returns them (issue #158's column-parity contract): the
        # fixture's own `PodSummary` is a separate dataclass of the same
        # shape, and `list_resources` dispatches its status facts on the
        # summary *type*, so an unconverted row would answer name+age — or
        # fail outright on the generic renderer's `desired`.
        rows: list[GenericSummary] = (
            [_pod_list_row(pod) for pod in PODS]
            if meta.plural == "pods"
            else list(EXTRA.get(meta.plural, []))
        )
        return [row for row in rows if _matches_namespace(meta, row, namespace)]

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        return await get_manifest(meta.plural, namespace, name)

    async def list_helm_releases(self, namespace: str | None) -> list[HelmReleaseSummary]:
        return [
            release
            for release in HELM_RELEASES
            if namespace is None or release.namespace == namespace
        ]

    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        del kind
        return await DemoEvents().fetch(namespace, name, uid=uid)

    def stream_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncIterator[LogLine]:
        source = stream_logs(
            namespace,
            pod,
            container,
            previous=previous,
            follow=follow,
        )
        if follow:
            # The endless stream the log pane consumes. Collecting it into
            # a tail buffer would never yield a line at all, so the bound
            # belongs to the finite read below.
            return source
        return _tail_stream(source, tail_lines)


def load_agent_story() -> ModuleType:
    """Import the sibling deterministic-provider module by path.

    This harness is executed as a script *and* imported by path from the
    documentation contracts, so a plain `import agent_story` would only
    resolve in the first case.
    """
    cached = sys.modules.get("korvid_docs_agent_story")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "korvid_docs_agent_story", Path(__file__).with_name("agent_story.py")
    )
    if spec is None or spec.loader is None:
        raise ImportError("docs/demo/agent_story.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules["korvid_docs_agent_story"] = module
    spec.loader.exec_module(module)
    return module


def build_demo_agent_runtime() -> AgentRuntime:
    """korvid's own `AgentRuntime` over the synthetic cluster."""
    story = load_agent_story()
    runtime: AgentRuntime = story.build_demo_agent_runtime(DemoReadOps(), ALIASES)
    return runtime


#: Seconds a follow-opened describe modal stays on screen in the `mcp`
#: scene before the harness dismisses it. Long enough to read the failing
#: pod's manifest and its BackOff event, short enough that the external
#: client's next read is not refused.
MCP_DESCRIBE_HOLD = 2.2
#: How often the `mcp` scene checks whether that modal is up.
_DESCRIBE_POLL = 0.2

#: The repository-local file the `mcp` scene publishes once its MCP server
#: is bound *and* the TUI has mounted. `docs/demo/mcp-follow.tape` waits for
#: it before releasing the external client: a fixed sleep releases the client
#: whether or not port 7878 is listening, so a slow cold checkout would open
#: the recorded story on a connection error. It never leaves the checkout
#: being recorded — the tape removes it on both sides of the run, and so does
#: `run_mcp_demo`.
MCP_READY_FILE = Path(".korvid-mcp-demo-ready")


def signal_mcp_ready(path: Path = MCP_READY_FILE) -> None:
    """Publish the readiness signal the recording tape waits for."""
    path.write_text("", encoding="utf-8")


def clear_mcp_ready(path: Path = MCP_READY_FILE) -> None:
    """Drop the readiness signal, including one an interrupted run left."""
    path.unlink(missing_ok=True)


class DemoKorvidApp(KorvidApp):
    """KorvidApp with documentation-only scene choreography."""

    def __init__(self, *args: Any, demo_scene: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._demo_scene = demo_scene
        self._describe_shown_at: float | None = None
        #: Armed by `run_mcp_demo` once the MCP server reports itself bound,
        #: so mounting can only publish readiness when both halves are up.
        self.on_mcp_ready: Callable[[], None] | None = None

    async def on_mount(self) -> None:
        await super().on_mount()
        if self._demo_scene == "agent":
            # Auto-open and focus the real AgentPanel input shortly after
            # mount so the recording tape can type the prompt itself and
            # press Enter through the genuine Input/on_input_submitted
            # path, instead of the harness synthesizing the submission.
            self.set_timer(0.2, self.action_toggle_agent)
        elif self._demo_scene == "mcp":
            self.set_interval(_DESCRIBE_POLL, self._dismiss_settled_describe)
            if self.on_mcp_ready is not None:
                # First frame of a mounted TUI over a bound server: the
                # earliest moment an external call can be answered *and*
                # mirrored on screen.
                self.on_mcp_ready()

    def _dismiss_settled_describe(self) -> None:
        """Close a follow-opened describe modal after it has been read.

        korvid refuses to follow an external read while a describe screen
        is on top — the user is reading it, and user action takes priority
        (`AgentUiController._describe_precheck`). That rule is real and must
        not be weakened, so the capture needs the Esc a watching operator
        would press: this timer is that keystroke's stand-in, and nothing
        else in the scene bypasses the rule.
        """
        if not isinstance(self.screen, DescribeScreen):
            self._describe_shown_at = None
            return
        now = monotonic()
        if self._describe_shown_at is None:
            self._describe_shown_at = now
        elif now - self._describe_shown_at >= MCP_DESCRIBE_HOLD:
            self._describe_shown_at = None
            self.pop_screen()


async def list_relationship_objects(
    meta: ResourceMeta,
    namespace: str | None,
) -> list[Any]:
    rows: list[Any] = list(PODS) if meta.plural == "pods" else list(EXTRA.get(meta.plural, []))
    return [row for row in rows if _matches_namespace(meta, row, namespace)]


def _parse_scene() -> str:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scene",
        choices=("base", "agent", "relationships", "mcp"),
        default="base",
    )
    return str(parser.parse_args().scene)


class _UIBridgeProxy(UIBridge):
    """Late-bound UI bridge, the same shape the composition root uses.

    `ToolExecutor` and `KorvidMCPServer` are built before the app exists, so
    they hold this proxy and the harness points `target` at the app's own
    `AppUIBridge` right after construction. The lock matters for the same
    reason it matters in `korvid/__main__.py`: an external host's reads are
    concurrent, and log-pane swaps and describe views are not safe to
    interleave.
    """

    _NOT_READY = f"{ERROR_PREFIX} UI not ready"

    def __init__(self) -> None:
        self.target: UIBridge | None = None
        self._lock = asyncio.Lock()

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            result: str = await getattr(self.target, method)(*args, **kwargs)
            return result

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return await self._call("agent_navigate", view, namespace)

    async def agent_set_filter(self, pattern: str) -> str:
        return await self._call("agent_set_filter", pattern)

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        return await self._call("agent_open_logs", pod, namespace, container)

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        return await self._call("agent_open_describe", kind, name, namespace)

    async def agent_drill_down(self, name: str) -> str:
        return await self._call("agent_drill_down", name)

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        return await self._call(
            "agent_request_write", action, kind, name, namespace, replicas, resources
        )

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        return await self._call(
            "agent_submit_write_proposal",
            action,
            kind,
            name,
            namespace,
            replicas,
            resources,
            session_id=session_id,
            client_name=client_name,
            client_version=client_version,
        )

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        return await self._call("agent_get_write_proposal", proposal_id)

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        return await self._call("agent_cancel_write_proposal", proposal_id, session_id=session_id)


class _MCPAppHooks:
    """Late-bound follow hooks, the same shape the composition root uses.

    The server reads follow state from — and sends activity notes to — the
    live app's `IntegrationController`, which does not exist yet when the
    server factory is built.
    """

    def __init__(self) -> None:
        self.app: KorvidApp | None = None

    def follow_enabled(self) -> bool:
        return self.app is not None and self.app.integrations.follow_enabled

    def note_activity(self, line: str) -> None:
        if self.app is not None:
            self.app.integrations.note_activity(line)


def build_demo_mcp_controller(
    ui: UIBridge,
    hooks: _MCPAppHooks,
) -> MCPControllerBase:
    """korvid's own MCP server over the synthetic cluster.

    Nothing here is a stand-in: the capture serves the shipped
    `KorvidMCPServer` with the shipped `mcp_tool_schemas()` surface over the
    shipped `ToolExecutor`, so an external SDK client really does drive the
    running TUI through follow mode. Only the bytes the read tools return
    are synthetic (`DemoReadOps`).

    `endpoint_path=None` keeps the recording out of the user's XDG state
    directory: the discovery file exists to auto-configure real hosts, and
    the capture's client is handed the URL directly.
    """
    from korvid.mcp.server import KorvidMCPServer, MCPController

    def factory() -> KorvidMCPServer:
        return KorvidMCPServer(
            ToolExecutor(DemoReadOps(), MCP_ALIASES, ui=ui),
            mcp_tool_schemas(),
            port=MCP_DEMO_PORT,
            ui=ui,
            follow_enabled=hooks.follow_enabled,
            note_activity=hooks.note_activity,
        )

    return MCPController(factory)


async def run_mcp_demo(
    app: DemoKorvidApp,
    controller: MCPControllerBase,
    *,
    ready_file: Path = MCP_READY_FILE,
) -> None:
    """Serve MCP for exactly as long as the TUI runs.

    The readiness signal the recording waits for is published here rather
    than at process start, and only through the app's mount hook: the server
    is bound first, so the file appears at the one moment an external client
    can both be answered and be mirrored on screen.

    `MCPController.start` reports a bind failure by *returning* an error line,
    so a failure is turned into an exception instead of a TUI with no server
    quietly publishing readiness — the tape's bounded wait then expires and
    the recording fails loudly rather than capturing a connection error.

    That return value is not the only way `start()` ends: a cancellation
    during the bind, or a failure creating the server task, propagates as an
    exception with part of the server possibly already holding the port. So
    the start happens *inside* the cleanup block — whichever way it ends, the
    readiness file is cleared and the controller is stopped, instead of
    leaving port 7878 occupied until the process exits and failing the next
    take for a reason that has nothing to do with that take.

    `controller.running` alone is not a safe gate: it means "the server task
    is alive", and a task can stay alive for a moment after a start timeout
    the controller failed to reap within its own deadline, while `start()`
    has already returned its `ERROR:` line. Readiness is armed only when the
    returned status is *not* an error line *and* the controller reports
    itself running — either signal alone can be wrong for a beat.

    Args:
        app: The mounted scene app; its `on_mcp_ready` hook is armed here.
        controller: The MCP controller serving the scene.
        ready_file: Where to publish readiness. Overridden by the contract
            tests so a run never touches the checkout.

    Raises:
        RuntimeError: The MCP server did not bind.
    """
    clear_mcp_ready(ready_file)
    try:
        status = await controller.start()
        if status.startswith(ERROR_PREFIX) or not controller.running:
            raise RuntimeError(f"the mcp scene needs a bound MCP server; start reported: {status}")
        app.on_mcp_ready = lambda: signal_mcp_ready(ready_file)
        await app.run_async()
    finally:
        try:
            clear_mcp_ready(ready_file)
        finally:
            await controller.stop()


def build_demo_app(scene: str, controller: MCPControllerBase | None) -> DemoKorvidApp:
    """The scene's app, wired the one way `main` wires it.

    Exposed so the capture contracts can mount the real app and watch the
    readiness signal fire, instead of trusting a reading of `on_mount`.

    Args:
        scene: One of `base`, `agent`, `relationships`, `mcp`.
        controller: The MCP controller for the `mcp` scene; `None` elsewhere.

    Returns:
        The demo app, not yet running.
    """
    store = ResourceStore()
    aliases = ALIASES
    if scene == "relationships":
        aliases = RELATIONSHIP_ALIASES
    elif scene == "mcp":
        aliases = MCP_ALIASES
    serving_mcp = controller is not None
    return DemoKorvidApp(
        config=KorvidConfig(
            namespace="shop",
            mcp_enabled=serving_mcp,
            mcp_port=MCP_DEMO_PORT,
            mcp_follow=serving_mcp,
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=aliases,
        get_manifest=get_manifest,
        get_events=DemoEvents(),
        stream_logs=stream_logs,
        agent_runtime=build_demo_agent_runtime() if scene == "agent" else None,
        agent_model_name="korvid-demo" if scene == "agent" else None,
        list_relationship_objects=(list_relationship_objects if scene == "relationships" else None),
        mcp=controller,
        demo_scene=scene,
    )


def main() -> None:
    scene = _parse_scene()
    ui_proxy = _UIBridgeProxy()
    mcp_hooks = _MCPAppHooks()
    controller = build_demo_mcp_controller(ui_proxy, mcp_hooks) if scene == "mcp" else None
    app = build_demo_app(scene, controller)
    if controller is None:
        app.run()
        return
    # Late-bind exactly the way the composition root does: the executor and
    # the server were built before the app existed.
    ui_proxy.target = AppUIBridge(app)
    mcp_hooks.app = app
    asyncio.run(run_mcp_demo(app, controller))


if __name__ == "__main__":
    main()
