"""Create/replace contract and discovery/watch semantics.

create/replace have no server-side dry-run preview in korvid's write
surface (``WriteOps`` defines previews for scale/rollout/delete/resize/
cordon only): the edit flow's approval dialog shows a locally computed
diff, and the server-side guard for both flows is the ``uid``
precondition. That precondition is exactly what these tests prove
against the live API server — a wrong uid is refused (409) and read-back
shows zero mutation; the right uid mutates exactly once.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy

import pytest

from korvid.k8s.client import KubeClient
from korvid.k8s.errors import ApiStatusError

from .conftest import CONFIGMAP, NODE, configmap_manifest

pytestmark = pytest.mark.contract


async def test_create_object_creates_exactly_once(client: KubeClient, namespace: str) -> None:
    manifest = configmap_manifest("create-once", {"round": "1"})

    await client.create_object(CONFIGMAP, namespace, manifest)

    created = await client.get_object(CONFIGMAP, namespace, "create-once")
    assert created["data"] == {"round": "1"}

    # Exactly once: the collection rejects a second POST for the same name.
    with pytest.raises(ApiStatusError, match="409"):
        await client.create_object(CONFIGMAP, namespace, manifest)


async def test_replace_object_carries_uid_and_mutates_exactly_once(
    client: KubeClient, namespace: str
) -> None:
    await client.create_object(CONFIGMAP, namespace, configmap_manifest("replace-target"))
    before = await client.get_object(CONFIGMAP, namespace, "replace-target")
    uid = before["metadata"]["uid"]

    edited = copy.deepcopy(before)
    edited["data"] = {"key": "edited"}

    # Wrong uid must be rejected by the server without mutating anything.
    with pytest.raises(ApiStatusError, match="409"):
        await client.replace_object(
            CONFIGMAP,
            namespace,
            "replace-target",
            edited,
            uid="00000000-0000-0000-0000-000000000000",
        )
    untouched = await client.get_object(CONFIGMAP, namespace, "replace-target")
    assert untouched["data"] == {"key": "value"}

    await client.replace_object(CONFIGMAP, namespace, "replace-target", edited, uid=uid)
    after = await client.get_object(CONFIGMAP, namespace, "replace-target")
    assert after["data"] == {"key": "edited"}
    assert after["metadata"]["uid"] == uid, "replace must keep the object identity"


async def test_discovery_covers_namespaced_and_cluster_scoped(client: KubeClient) -> None:
    metas = await client.discover_resources()

    by_plural = {meta.plural: meta for meta in metas if not meta.synthetic}
    assert by_plural["configmaps"].namespaced is True
    assert by_plural["nodes"].namespaced is False
    assert by_plural["deployments"].group == "apps"


async def test_watch_sees_live_object_lifecycle(client: KubeClient, namespace: str) -> None:
    async def create_later() -> None:
        await asyncio.sleep(1.0)
        await client.create_object(CONFIGMAP, namespace, configmap_manifest("watch-me"))

    creator = asyncio.create_task(create_later())
    seen: list[str] = []
    try:
        async with asyncio.timeout(60):
            async for _event, summary in client.watch_objects(CONFIGMAP, namespace):
                seen.append(summary.name)
                if "watch-me" in seen:
                    break
    finally:
        creator.cancel()
        # Await the cancellation so the creator can't race namespace
        # teardown or outlive the event loop.
        with contextlib.suppress(asyncio.CancelledError):
            await creator

    assert "watch-me" in seen, "watch must deliver the created object"


async def test_cluster_scoped_list_returns_nodes(client: KubeClient) -> None:
    nodes = await client.list_objects(NODE, None)

    assert nodes, "the running cluster must report at least one node"
    assert all(summary.namespace == "" for summary in nodes)
