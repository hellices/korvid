"""Fixtures for the live-cluster contract suite (issue #109).

The suite is gated on ``KORVID_CONTRACT_RUN_ID``: without it every test
is skipped at collection time, keeping the fast PR gate untouched. The
workflow sets it to ``${run_id}-${run_attempt}`` so each run owns a
unique namespace and every fixture is labelled for idempotent cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta

RUN_ID = os.environ.get("KORVID_CONTRACT_RUN_ID", "")
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "korvid-contract"
RUN_LABEL = "korvid.dev/contract-run"

pytestmark = pytest.mark.contract

CONFIGMAP = ResourceMeta(
    kind="ConfigMap", plural="configmaps", group="", version="v1", namespaced=True
)
DEPLOYMENT = ResourceMeta(
    kind="Deployment", plural="deployments", group="apps", version="v1", namespaced=True
)
POD = ResourceMeta(kind="Pod", plural="pods", group="", version="v1", namespaced=True)
NAMESPACE = ResourceMeta(
    kind="Namespace", plural="namespaces", group="", version="v1", namespaced=False
)
NODE = ResourceMeta(kind="Node", plural="nodes", group="", version="v1", namespaced=False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if RUN_ID:
        return
    skip = pytest.mark.skip(reason="KORVID_CONTRACT_RUN_ID not set; live contract suite is opt-in")
    for item in items:
        if item.get_closest_marker("contract"):
            item.add_marker(skip)


def run_labels() -> dict[str, str]:
    """Labels every run-owned fixture carries (idempotent cleanup key)."""
    return {MANAGED_BY_LABEL: MANAGED_BY_VALUE, RUN_LABEL: RUN_ID}


async def until(
    condition: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 120.0,
    interval: float = 2.0,
    message: str = "condition not met",
) -> None:
    """Poll *condition* until true or fail the test after *timeout*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await condition():
            return
        await asyncio.sleep(interval)
    pytest.fail(f"timed out after {timeout}s: {message}")


async def preview_until_settled(
    preview: Callable[[], Awaitable[list[str] | None]], *, timeout: float = 60.0
) -> list[str]:
    """Retry a preview until it yields lines.

    Previews are pinned to a GET snapshot's resourceVersion and answer None
    on any failure (they must never block the approval flow), so a controller
    bumping the object's revision mid-preview is a legitimate transient.
    """
    result: list[str] | None = None

    async def attempt() -> bool:
        nonlocal result
        result = await preview()
        return result is not None

    await until(attempt, timeout=timeout, message="preview should succeed once the object settles")
    assert result is not None
    return result


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[KubeClient]:
    kube = KubeClient()
    await kube.connect()
    yield kube
    await kube.close()


@pytest.fixture
async def namespace(client: KubeClient) -> AsyncIterator[str]:
    """Unique per-test namespace; deleted (with everything inside) on teardown.

    A fresh suffix per test avoids colliding with a same-named namespace
    still terminating from the previous test.
    """
    suffix = uuid.uuid4().hex[:6]
    name = f"korvid-contract-{RUN_ID}".lower().replace("_", "-")[:56].rstrip("-") + f"-{suffix}"
    manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": name, "labels": run_labels()},
    }
    await client.create_object(NAMESPACE, None, manifest)
    yield name
    # Already gone is fine; the janitor catches leftovers.
    with contextlib.suppress(Exception):
        await client.delete_object(NAMESPACE, None, name)


def configmap_manifest(name: str, data: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "labels": run_labels()},
        "data": data or {"key": "value"},
    }


def deployment_manifest(name: str, *, replicas: int = 1) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": run_labels()},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name, **run_labels()}},
                "spec": {
                    "nodeSelector": {"korvid.dev/pool": "workload"},
                    "tolerations": [],
                    "containers": [
                        {
                            "name": "pause",
                            "image": "registry.k8s.io/pause:3.10",
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "16Mi"},
                                "limits": {"cpu": "50m", "memory": "32Mi"},
                            },
                        }
                    ],
                },
            },
        },
    }


def pod_manifest(name: str, *, cpu_request: str = "10m") -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": run_labels()},
        "spec": {
            "nodeSelector": {"korvid.dev/pool": "workload"},
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "pause",
                    "image": "registry.k8s.io/pause:3.10",
                    "resources": {
                        "requests": {"cpu": cpu_request, "memory": "16Mi"},
                        "limits": {"cpu": "100m", "memory": "32Mi"},
                    },
                }
            ],
        },
    }
