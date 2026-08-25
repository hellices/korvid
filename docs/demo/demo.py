"""Run the korvid TUI against canned fake data for README GIF recording.

Not shipped with the package — a development harness driven by
``docs/demo/demo.tape`` (VHS). See ``docs/demo/README.md`` for how to
regenerate ``docs/assets/demo.gif``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import random
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from korvid.agent.runtime import AgentRuntime
from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HelmReleaseSummary
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.reads import ReadOps
from korvid.k8s.relationship_facts import (
    RelationKind,
    RelationshipFacts,
    extract_relationship_facts,
)
from korvid.ui.app import EventsFetcher, KorvidApp

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
    _pod("web-frontend-7d4b9c-x2kfp", "shop", cpu="250m", mem="256Mi"),
    _pod("web-frontend-7d4b9c-9qwzr", "shop", cpu="250m", mem="256Mi"),
    _pod("cart-api-5f6d8b-mn4tp", "shop", node="node-2"),
    _pod("cart-api-5f6d8b-kd82v", "shop", node="node-3"),
    _pod(
        "payment-worker-6c9f7d-b3xnq",
        "shop",
        phase="CrashLoopBackOff",
        ready="0/1",
        restarts=17,
        node="node-2",
        uid="pod-payment",
        labels=_PAYMENT_LABELS,
        relationships=_PAYMENT_RELATIONSHIPS,
    ),
    _pod("checkout-svc-84c5d6-ln7wk", "shop", restarts=2),
    _pod("inventory-db-0", "shop", qos="Guaranteed", cpu="500m", mem="1Gi", node="node-3"),
    _pod("search-indexer-59b8c7-tq5mz", "shop", phase="Pending", ready="0/1", node="-"),
    _pod("grafana-7b5c9d-w8xkp", "monitoring", node="node-1"),
    _pod("prometheus-0", "monitoring", qos="Guaranteed", cpu="1", mem="2Gi", node="node-2"),
    _pod("loki-0", "monitoring", node="node-3"),
    _pod("coredns-5d78c9b4-fh2mx", "kube-system", qos="Guaranteed"),
    _pod("coredns-5d78c9b4-pk9vz", "kube-system", qos="Guaranteed", node="node-2"),
    _pod("metrics-server-6f89b5-jw3qd", "kube-system", node="node-3"),
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


EXTRA: dict[str, list[Summary]] = {
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
            created="",
            uid="cm-payment",
        )
    ],
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


async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
    meta = RELATIONSHIP_ALIASES.get(kind)
    if meta is None:
        raise KeyError(f"unknown demo kind: {kind}")
    base = _MANIFESTS.get(meta.plural)
    if base is None:
        api_version = f"{meta.group}/{meta.version}" if meta.group else meta.version
        base = {"apiVersion": api_version, "kind": meta.kind, "metadata": {}}
    manifest = dict(base)
    metadata = dict(base["metadata"])
    metadata["name"] = name
    if namespace:
        metadata["namespace"] = namespace
    manifest["metadata"] = metadata
    return manifest


class DemoEvents(EventsFetcher):
    async def fetch(
        self, namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        if name.startswith("payment-worker-"):
            # The crashlooping demo pod.
            return [
                {
                    "type": "Warning",
                    "reason": "BackOff",
                    "lastTimestamp": (now - timedelta(minutes=2)).isoformat(),
                    "message": "Back-off restarting failed container app in pod " + name,
                    "involvedObject": {"name": name},
                },
                {
                    "type": "Normal",
                    "reason": "Pulled",
                    "lastTimestamp": (now - timedelta(minutes=3)).isoformat(),
                    "message": 'Container image "payment-worker:2.4.1" already present on machine',
                    "involvedObject": {"name": name},
                },
            ]
        return [
            {
                "type": "Normal",
                "reason": "ScalingReplicaSet",
                "lastTimestamp": (now - timedelta(hours=4)).isoformat(),
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


class DemoReadOps(ReadOps):
    """The synthetic fixture behind korvid's real agent read surface.

    `ToolExecutor` depends on this ABC rather than on `KubeClient`, so the
    documentation capture can run the *shipped* read tools — `diagnose_pod`
    and `get_logs` gather, redact, bound and cite exactly as they would
    against a cluster; only the bytes they read are synthetic.
    """

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[Any]:
        # `Any` rather than the ABC's `list[GenericSummary]`: the fixture's pods
        # are `PodSummary`, which is a separate dataclass of the same shape, not
        # a subclass. The read tools only ever read attributes off these rows.
        rows: list[Any] = list(PODS) if meta.plural == "pods" else list(EXTRA.get(meta.plural, []))
        return [row for row in rows if namespace is None or row.namespace == namespace]

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        return await get_manifest(meta.plural, namespace, name)

    async def list_helm_releases(self, namespace: str | None) -> list[HelmReleaseSummary]:
        releases = [
            HelmReleaseSummary(
                name="shop",
                namespace="shop",
                kind="HelmRelease",
                created="",
                revision=4,
                status="deployed",
                chart="shop-0.8.0",
                app_version="2.4.1",
            )
        ]
        return [
            release for release in releases if namespace is None or release.namespace == namespace
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
        del tail_lines
        return stream_logs(
            namespace,
            pod,
            container,
            previous=previous,
            follow=follow,
        )


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


class DemoKorvidApp(KorvidApp):
    """KorvidApp with documentation-only scene choreography."""

    def __init__(self, *args: Any, demo_scene: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._demo_scene = demo_scene

    async def on_mount(self) -> None:
        await super().on_mount()
        if self._demo_scene == "agent":
            # Auto-open and focus the real AgentPanel input shortly after
            # mount so the recording tape can type the prompt itself and
            # press Enter through the genuine Input/on_input_submitted
            # path, instead of the harness synthesizing the submission.
            self.set_timer(0.2, self.action_toggle_agent)


async def list_relationship_objects(
    meta: ResourceMeta,
    namespace: str | None,
) -> list[Any]:
    rows: list[Any] = list(PODS) if meta.plural == "pods" else list(EXTRA.get(meta.plural, []))
    if namespace is None:
        return rows
    return [row for row in rows if row.namespace == namespace]


def _parse_scene() -> str:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scene",
        choices=("base", "agent", "relationships"),
        default="base",
    )
    return str(parser.parse_args().scene)


def main() -> None:
    scene = _parse_scene()
    store = ResourceStore()
    app = DemoKorvidApp(
        config=KorvidConfig(namespace="shop"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=RELATIONSHIP_ALIASES if scene == "relationships" else ALIASES,
        get_manifest=get_manifest,
        get_events=DemoEvents(),
        stream_logs=stream_logs,
        agent_runtime=build_demo_agent_runtime() if scene == "agent" else None,
        agent_model_name="korvid-demo" if scene == "agent" else None,
        list_relationship_objects=(list_relationship_objects if scene == "relationships" else None),
        demo_scene=scene,
    )
    app.run()


if __name__ == "__main__":
    main()
