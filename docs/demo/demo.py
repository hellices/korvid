"""Run the korvid TUI against canned fake data for README GIF recording.

Not shipped with the package — a development harness driven by
``docs/demo/demo.tape`` (VHS). See ``docs/demo/README.md`` for how to
regenerate ``docs/assets/demo.gif``.
"""

from __future__ import annotations

import argparse
import asyncio
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from korvid.agent.events import (
    AgentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
)
from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    RelationKind,
    RelationshipFacts,
    TargetReference,
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
for alias in ("configmaps", "configmap", "cm"):
    ALIASES[alias] = _CONFIGMAP_META


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
        relationships=relationships or RelationshipFacts(),
    )


_PAYMENT_RELATIONSHIPS = RelationshipFacts(
    references=(
        ReferenceFact(
            relation=RelationKind.USES_CONFIG,
            target=TargetReference(
                group="",
                kind="ConfigMap",
                namespace="shop",
                name="payment-config",
            ),
            confidence=FactConfidence.DECLARED,
            field="spec.volumes[0].configMap.name",
        ),
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


def _svc(name: str, ns: str, days: int) -> GenericSummary:
    created = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    return GenericSummary(name=name, namespace=ns, kind="Service", created=created)


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
        "nodeName": "node-2",
        "restartPolicy": "Always",
    },
    "status": {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady"}],
    },
}

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
        "selector": {"app": "payment-worker"},
        "ports": [{"name": "http", "port": 80, "targetPort": 8080, "protocol": "TCP"}],
    },
    "status": {"loadBalancer": {}},
}

_MANIFESTS: dict[str, dict[str, Any]] = {
    "pods": POD_MANIFEST,
    "deployments": DEPLOY_MANIFEST,
    "services": SVC_MANIFEST,
}


async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
    base = _MANIFESTS.get(ALIASES[kind].plural if kind in ALIASES else kind, POD_MANIFEST)
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


class ScriptedAgentRuntime:
    """Deterministic real AgentPanel input for documentation captures."""

    def __init__(self) -> None:
        self.total_tokens = (0, 0)
        self.usage_estimated = False

    async def run_turn(self, user_text: str, screen_context: str) -> AsyncIterator[AgentEvent]:
        del user_text, screen_context
        yield ToolCallStarted(
            call_id="demo-diagnose",
            name="diagnose_pod",
            arguments='{"namespace":"shop","name":"payment-worker-6c9f7d-b3xnq"}',
        )
        await asyncio.sleep(0.8)
        yield ToolCallFinished(
            call_id="demo-diagnose",
            name="diagnose_pod",
            ok=True,
            summary="CrashLoopBackOff · 17 restarts · gateway 503 evidence [E1]",
        )
        await asyncio.sleep(0.5)
        yield TextDelta(
            text=(
                "The payment worker is crash-looping after repeated gateway 503s. "
                "Open its logs and inspect the owner before changing it. [E1]"
            )
        )
        yield TurnComplete(
            input_tokens=612,
            output_tokens=43,
            estimated=False,
            cited=("E1",),
        )


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
        aliases=ALIASES,
        get_manifest=get_manifest,
        get_events=DemoEvents(),
        stream_logs=stream_logs,
        agent_runtime=ScriptedAgentRuntime() if scene == "agent" else None,
        agent_model_name="korvid-demo" if scene == "agent" else None,
        list_relationship_objects=(list_relationship_objects if scene == "relationships" else None),
        demo_scene=scene,
    )
    app.run()


if __name__ == "__main__":
    main()
