"""Node-operation contract: cordon/uncordon/drain/eviction against the
disposable workload node ONLY. System nodes are never touched — every
test asserts the target carries ``korvid.dev/disposable=true`` first.
"""

from __future__ import annotations

import pytest

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError

from .conftest import NODE, POD, pod_manifest, preview_until_settled, run_labels, until

pytestmark = pytest.mark.contract

PDB = ResourceMeta(
    kind="PodDisruptionBudget",
    plural="poddisruptionbudgets",
    group="policy",
    version="v1",
    namespaced=True,
)


async def _disposable_node(client: KubeClient, namespace_hint: str = "") -> str:
    summaries = await client.list_objects(NODE, None)
    for summary in summaries:
        manifest = await client.get_object(NODE, None, summary.name)
        labels = manifest["metadata"].get("labels") or {}
        if labels.get("korvid.dev/disposable") == "true":
            return summary.name
    pytest.fail("no disposable workload node found — refusing to touch system nodes")


async def test_preview_cordon_causes_no_persistent_mutation(client: KubeClient) -> None:
    node = await _disposable_node(client)
    before = await client.get_object(NODE, None, node)
    uid = before["metadata"]["uid"]
    assert not before["spec"].get("unschedulable"), "node must start schedulable"

    lines = await preview_until_settled(lambda: client.preview_cordon(node, True, uid=uid))

    assert lines
    after = await client.get_object(NODE, None, node)
    assert not after["spec"].get("unschedulable"), "preview must not cordon the node"


async def test_execute_cordon_and_uncordon(client: KubeClient) -> None:
    node = await _disposable_node(client)
    before = await client.get_object(NODE, None, node)
    uid = before["metadata"]["uid"]

    await client.cordon_node(node, True, uid=uid)
    try:
        cordoned = await client.get_object(NODE, None, node)
        assert cordoned["spec"]["unschedulable"] is True
    finally:
        await client.cordon_node(node, False, uid=uid)

    restored = await client.get_object(NODE, None, node)
    assert not restored["spec"].get("unschedulable"), "node must end schedulable"


async def test_drain_plan_classifies_run_pod(client: KubeClient, namespace: str) -> None:
    await client.create_object(POD, namespace, pod_manifest("drain-subject"))

    async def scheduled() -> bool:
        manifest = await client.get_object(POD, namespace, "drain-subject")
        return bool(manifest["spec"].get("nodeName"))

    await until(scheduled, timeout=180, message="pod should be scheduled")
    manifest = await client.get_object(POD, namespace, "drain-subject")
    node = manifest["spec"]["nodeName"]
    node_manifest = await client.get_object(NODE, None, node)
    assert (node_manifest["metadata"].get("labels") or {}).get("korvid.dev/disposable") == "true"

    plan = await client.drain_plan(node)

    refs = {target.ref for target in plan.targets}
    assert f"{namespace}/drain-subject" in refs, "drain plan must include the run pod"


async def test_eviction_honours_pdb_then_succeeds(client: KubeClient, namespace: str) -> None:
    await client.create_object(POD, namespace, pod_manifest("evict-subject"))

    async def running() -> bool:
        manifest = await client.get_object(POD, namespace, "evict-subject")
        return bool(manifest.get("status", {}).get("phase") == "Running")

    await until(running, timeout=180, message="pod should run before eviction")

    pdb_manifest = {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": "evict-guard", "labels": run_labels()},
        "spec": {
            "maxUnavailable": 0,
            "selector": {
                "matchLabels": {"korvid.dev/contract-run": run_labels()["korvid.dev/contract-run"]}
            },
        },
    }
    await client.create_object(PDB, namespace, pdb_manifest)

    async def pdb_active() -> bool:
        manifest = await client.get_object(PDB, namespace, "evict-guard")
        return "disruptionsAllowed" in manifest.get("status", {})

    await until(pdb_active, timeout=60, message="PDB status should be computed")

    # Blocked: zero disruptions allowed -> API answers 429, pod survives.
    with pytest.raises(ApiStatusError, match="429"):
        await client.evict_pod(namespace, "evict-subject", uid=None)
    survivor = await client.get_object(POD, namespace, "evict-subject")
    assert "deletionTimestamp" not in survivor["metadata"]

    # Unblocked: removing the PDB lets the same eviction succeed.
    await client.delete_object(PDB, namespace, "evict-guard")

    async def evicted() -> bool:
        try:
            manifest = await client.get_object(POD, namespace, "evict-subject")
        except ApiStatusError as exc:
            return exc.status == 404
        return "deletionTimestamp" in manifest["metadata"]

    async def try_evict() -> bool:
        try:
            await client.evict_pod(namespace, "evict-subject", uid=None)
        except ApiStatusError as exc:
            if exc.status == 404:
                return True
            if exc.status == 429:
                return False  # PDB deletion not yet observed
            raise
        return True

    await until(try_evict, timeout=60, message="eviction should succeed once the PDB is gone")
    await until(evicted, timeout=60, message="pod should terminate after eviction")
