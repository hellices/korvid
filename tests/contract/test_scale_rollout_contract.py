"""Scale and rollout-restart contract: preview leaves the object
untouched; execute changes the spec exactly once (one generation bump).
"""

from __future__ import annotations

import pytest

from korvid.k8s.client import KubeClient

from .conftest import DEPLOYMENT, deployment_manifest, preview_until_settled, until

pytestmark = pytest.mark.contract

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"


async def _deployment_exists(client: KubeClient, namespace: str, name: str) -> None:
    async def created() -> bool:
        manifest = await client.get_object(DEPLOYMENT, namespace, name)
        return bool(manifest["metadata"].get("uid"))

    await until(created, message=f"deployment {name} should exist")


async def test_preview_scale_causes_no_persistent_mutation(
    client: KubeClient, namespace: str
) -> None:
    await client.create_object(DEPLOYMENT, namespace, deployment_manifest("scale-preview"))
    await _deployment_exists(client, namespace, "scale-preview")
    before = await client.get_object(DEPLOYMENT, namespace, "scale-preview")
    uid = before["metadata"]["uid"]

    lines = await preview_until_settled(
        lambda: client.preview_scale(DEPLOYMENT, namespace, "scale-preview", 3, uid=uid)
    )

    assert any("3" in line for line in lines)
    after = await client.get_object(DEPLOYMENT, namespace, "scale-preview")
    assert after["spec"]["replicas"] == 1, "preview must not change spec.replicas"
    assert after["metadata"]["generation"] == before["metadata"]["generation"]


async def test_execute_scale_mutates_exactly_once(client: KubeClient, namespace: str) -> None:
    await client.create_object(DEPLOYMENT, namespace, deployment_manifest("scale-execute"))
    await _deployment_exists(client, namespace, "scale-execute")
    before = await client.get_object(DEPLOYMENT, namespace, "scale-execute")
    uid = before["metadata"]["uid"]

    await client.scale_object(DEPLOYMENT, namespace, "scale-execute", 2, uid=uid)

    after = await client.get_object(DEPLOYMENT, namespace, "scale-execute")
    assert after["spec"]["replicas"] == 2
    assert after["metadata"]["generation"] == before["metadata"]["generation"] + 1, (
        "exactly one spec mutation"
    )


async def test_preview_rollout_restart_causes_no_persistent_mutation(
    client: KubeClient, namespace: str
) -> None:
    await client.create_object(DEPLOYMENT, namespace, deployment_manifest("restart-preview"))
    await _deployment_exists(client, namespace, "restart-preview")
    before = await client.get_object(DEPLOYMENT, namespace, "restart-preview")
    uid = before["metadata"]["uid"]
    stamp = "2026-01-01T00:00:00Z"

    lines = await preview_until_settled(
        lambda: client.preview_rollout_restart(
            DEPLOYMENT, namespace, "restart-preview", uid=uid, restarted_at=stamp
        )
    )

    assert any(stamp in line for line in lines), (
        "preview must show the exact stamp the execute path will send"
    )
    after = await client.get_object(DEPLOYMENT, namespace, "restart-preview")
    annotations = after["spec"]["template"]["metadata"].get("annotations") or {}
    assert RESTART_ANNOTATION not in annotations, "preview must not stamp the template"
    assert after["metadata"]["generation"] == before["metadata"]["generation"]


async def test_execute_rollout_restart_applies_previewed_stamp_exactly_once(
    client: KubeClient, namespace: str
) -> None:
    await client.create_object(DEPLOYMENT, namespace, deployment_manifest("restart-execute"))
    await _deployment_exists(client, namespace, "restart-execute")
    before = await client.get_object(DEPLOYMENT, namespace, "restart-execute")
    uid = before["metadata"]["uid"]
    stamp = "2026-01-01T00:00:00Z"

    await client.rollout_restart_with_stamp(
        DEPLOYMENT, namespace, "restart-execute", uid=uid, restarted_at=stamp
    )

    after = await client.get_object(DEPLOYMENT, namespace, "restart-execute")
    annotations = after["spec"]["template"]["metadata"]["annotations"]
    assert annotations[RESTART_ANNOTATION] == stamp, "execute must send the previewed stamp"
    assert after["metadata"]["generation"] == before["metadata"]["generation"] + 1
