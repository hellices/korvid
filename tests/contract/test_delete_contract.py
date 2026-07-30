"""DELETE contract: dry-run causes no mutation; execute deletes exactly once.

Issue #103 background: a DELETE preview once reached the real mutation
path even though unit tests passed. These tests read state back from
the live API server around every preview/execute call.
"""

from __future__ import annotations

import pytest

from korvid.k8s.client import KubeClient
from korvid.k8s.errors import ApiStatusError

from .conftest import CONFIGMAP, configmap_manifest, until

pytestmark = pytest.mark.contract


async def test_preview_delete_causes_no_persistent_mutation(
    client: KubeClient, namespace: str
) -> None:
    await client.create_object(CONFIGMAP, namespace, configmap_manifest("delete-preview"))
    before = await client.get_object(CONFIGMAP, namespace, "delete-preview")
    uid = before["metadata"]["uid"]
    rv = before["metadata"]["resourceVersion"]

    lines = await client.preview_delete(CONFIGMAP, namespace, "delete-preview", uid=uid)

    assert lines, "server accepted the dry-run delete and produced a preview"
    after = await client.get_object(CONFIGMAP, namespace, "delete-preview")
    assert after["metadata"]["uid"] == uid, "object must survive the preview"
    assert after["metadata"]["resourceVersion"] == rv, "preview must not bump resourceVersion"
    assert "deletionTimestamp" not in after["metadata"], "preview must not begin deletion"


async def test_preview_delete_carries_uid_precondition(client: KubeClient, namespace: str) -> None:
    await client.create_object(CONFIGMAP, namespace, configmap_manifest("delete-uid-guard"))
    wrong_uid = "00000000-0000-0000-0000-000000000000"

    # preview_delete's contract: the server rejects the dry-run for the wrong
    # incarnation (409) and the client reports "no preview" instead of
    # summarizing the wrong object.
    lines = await client.preview_delete(CONFIGMAP, namespace, "delete-uid-guard", uid=wrong_uid)

    assert lines is None, "wrong uid must not produce a preview"
    still_there = await client.get_object(CONFIGMAP, namespace, "delete-uid-guard")
    assert "deletionTimestamp" not in still_there["metadata"]


async def test_execute_delete_mutates_exactly_once(client: KubeClient, namespace: str) -> None:
    await client.create_object(CONFIGMAP, namespace, configmap_manifest("delete-execute"))
    before = await client.get_object(CONFIGMAP, namespace, "delete-execute")
    uid = before["metadata"]["uid"]

    await client.delete_object(CONFIGMAP, namespace, "delete-execute", uid=uid)

    async def gone() -> bool:
        try:
            await client.get_object(CONFIGMAP, namespace, "delete-execute")
        except ApiStatusError as exc:
            return exc.status == 404
        return False

    await until(gone, message="configmap should be deleted")

    # Exactly once: replaying the same delete finds nothing to mutate.
    with pytest.raises(ApiStatusError, match="404"):
        await client.delete_object(CONFIGMAP, namespace, "delete-execute", uid=uid)
