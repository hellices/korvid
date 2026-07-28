"""Run the korvid TUI against canned fake data for README GIF recording.

Not shipped with the package — a development harness driven by
``docs/demo/demo.tape`` (VHS). See ``docs/demo/README.md`` for how to
regenerate ``docs/assets/demo.gif``.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui.app import EventsFetcher, KorvidApp

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_SVC_META = ResourceMeta("Service", "services", "", "v1", True, ("svc",))

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


MANIFEST: dict[str, Any] = {
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


async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
    manifest = dict(MANIFEST)
    metadata = dict(MANIFEST["metadata"])
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


def main() -> None:
    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="shop"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=ALIASES,
        get_manifest=get_manifest,
        get_events=DemoEvents(),
        stream_logs=stream_logs,
        provider_hint="demo",
    )
    app.run()


if __name__ == "__main__":
    main()
