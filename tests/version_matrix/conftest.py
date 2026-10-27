"""Fixtures for the Kubernetes version-matrix scenarios (#409, slice S5).

The suite is opt-in: without `KORVID_VERSION_MATRIX_CONTEXT`, every test is
skipped at collection, so the ordinary test run never reaches a cluster. The
workflow sets it to the admin context of a kind cluster it created for the
run, after `setup.sh` has installed the fixtures and the principals'
contexts.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from korvid.core.api_versions import DiscoverySnapshot, observe_discovery
from korvid.k8s.client import KubeClient

ADMIN_CONTEXT = os.environ.get("KORVID_VERSION_MATRIX_CONTEXT", "")
#: Lists only core events; denied events.k8s.io events and everything else.
EVENTS_CORE_CONTEXT = "matrix-events-core"
#: Lists only events.k8s.io events; denied core events and everything else.
EVENTS_GROUP_CONTEXT = "matrix-events-group"
#: No credentials, so the API server treats it as anonymous.
ANONYMOUS_CONTEXT = "matrix-anonymous"
NAMESPACE = "korvid-matrix"

Connect = Callable[[str], Awaitable[KubeClient]]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if ADMIN_CONTEXT:
        return
    skip = pytest.mark.skip(
        reason="KORVID_VERSION_MATRIX_CONTEXT not set; the version matrix is opt-in"
    )
    for item in items:
        if item.get_closest_marker("version_matrix"):
            item.add_marker(skip)


@pytest.fixture
async def connect() -> AsyncIterator[Connect]:
    """Connect a client to one of the matrix contexts; every one is closed afterwards."""
    clients: list[KubeClient] = []

    async def open_client(context: str) -> KubeClient:
        kube = KubeClient()
        await kube.connect(context)
        clients.append(kube)
        return kube

    yield open_client
    for kube in clients:
        await kube.close()


async def snapshot_of(kube: KubeClient) -> DiscoverySnapshot:
    """The observed discovery of `kube`'s current session."""
    return observe_discovery(await kube.discovery_snapshot())
