"""In-place pod resize contract (pods/resize subresource)."""

from __future__ import annotations

import pytest

from korvid.k8s.client import KubeClient

from .conftest import POD, pod_manifest, preview_until_settled, until

pytestmark = pytest.mark.contract


async def _wait_running(client: KubeClient, namespace: str, name: str) -> None:
    async def running() -> bool:
        manifest = await client.get_object(POD, namespace, name)
        return bool(manifest.get("status", {}).get("phase") == "Running")

    await until(running, timeout=180, message=f"pod {name} should reach Running")


async def test_preview_resize_causes_no_persistent_mutation(
    client: KubeClient, namespace: str
) -> None:
    # The dedicated AKS cluster runs k8s >= 1.33 (resize GA): a negative
    # capability probe here is a discovery regression, not an environment to
    # skip — skipping would silently drop all live resize coverage.
    assert await client.supports_pod_resize(), (
        "capability probe must detect pods/resize on the contract cluster"
    )
    await client.create_object(POD, namespace, pod_manifest("resize-preview"))
    await _wait_running(client, namespace, "resize-preview")
    before = await client.get_object(POD, namespace, "resize-preview")
    uid = before["metadata"]["uid"]
    new_resources = {"pause": {"requests": {"cpu": "20m"}}}

    lines = await preview_until_settled(
        lambda: client.preview_resize(namespace, "resize-preview", new_resources, uid=uid)
    )

    # The diff renderer truncates long values, so assert the change site
    # rather than the literal quantity.
    assert any("spec.containers" in line for line in lines)
    after = await client.get_object(POD, namespace, "resize-preview")
    cpu = after["spec"]["containers"][0]["resources"]["requests"]["cpu"]
    assert cpu == "10m", "preview must not change container requests"


async def test_execute_resize_mutates_exactly_once(client: KubeClient, namespace: str) -> None:
    # The dedicated AKS cluster runs k8s >= 1.33 (resize GA): a negative
    # capability probe here is a discovery regression, not an environment to
    # skip — skipping would silently drop all live resize coverage.
    assert await client.supports_pod_resize(), (
        "capability probe must detect pods/resize on the contract cluster"
    )
    await client.create_object(POD, namespace, pod_manifest("resize-execute"))
    await _wait_running(client, namespace, "resize-execute")
    before = await client.get_object(POD, namespace, "resize-execute")
    uid = before["metadata"]["uid"]

    await client.resize_pod(
        namespace, "resize-execute", {"pause": {"requests": {"cpu": "20m"}}}, uid=uid
    )

    after = await client.get_object(POD, namespace, "resize-execute")
    cpu = after["spec"]["containers"][0]["resources"]["requests"]["cpu"]
    assert cpu == "20m", "execute must apply the new request"
    assert after["metadata"]["uid"] == uid, "resize must happen in place, not by replacement"
