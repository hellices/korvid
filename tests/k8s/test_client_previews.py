"""Server-side dry-run previews for approval dialogs (issue #19).

``preview_*`` fetches the current state, replays the mutation with
``dryRun=All``, and returns compact diff lines for the ConfirmScreen.
Any failure returns None: a preview must never block the approval flow.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from kubernetes_asyncio.client.exceptions import ApiException

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.writes import WriteOps


def _deploy_meta() -> ResourceMeta:
    return ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))


def _resp(payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.read = AsyncMock(return_value=json.dumps(payload).encode())
    return resp


async def test_preview_scale_diffs_scale_subresource() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(
        side_effect=[
            _resp({"spec": {"replicas": 3}, "status": {"replicas": 3}}),
            _resp({"spec": {"replicas": 5}, "status": {"replicas": 3}}),
        ]
    )
    with patch.object(client, "_api", api):
        lines = await client.preview_scale(_deploy_meta(), "default", "web", 5)
    assert lines == ["~ spec.replicas: 3 -> 5"]
    first_args = api.call_api.call_args_list[0][0]
    assert first_args[0] == "/apis/apps/v1/namespaces/default/deployments/web/scale"
    assert first_args[1] == "GET"
    args, kwargs = api.call_api.call_args_list[1]
    assert args[0] == "/apis/apps/v1/namespaces/default/deployments/web/scale"
    assert args[1] == "PATCH"
    assert ("dryRun", "All") in kwargs["query_params"]
    assert kwargs["body"] == {"spec": {"replicas": 5}}


async def test_preview_rollout_restart_diffs_object() -> None:
    client = KubeClient()
    api = MagicMock()
    current = {"metadata": {"name": "web"}, "spec": {"template": {"metadata": {}}}}
    proposed = {
        "metadata": {"name": "web"},
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": "2026-01-01T00:00:00"}
                }
            }
        },
    }
    api.call_api = AsyncMock(side_effect=[_resp(current), _resp(proposed)])
    with patch.object(client, "_api", api):
        lines = await client.preview_rollout_restart(_deploy_meta(), "default", "web")
    assert lines == [
        "+ spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt:"
        ' "2026-01-01T00:00:00"'
    ]
    args, kwargs = api.call_api.call_args_list[1]
    assert args[1] == "PATCH"
    assert ("dryRun", "All") in kwargs["query_params"]


async def test_preview_delete_summarizes_object_and_cascade() -> None:
    client = KubeClient()
    api = MagicMock()
    manifest = {
        "metadata": {
            "name": "web",
            "uid": "abc-123",
            "creationTimestamp": "2026-07-20T01:02:03Z",
        }
    }
    api.call_api = AsyncMock(side_effect=[_resp(manifest), _resp({"kind": "Status"})])
    with patch.object(client, "_api", api):
        lines = await client.preview_delete(_deploy_meta(), "default", "web")
    assert lines is not None
    assert lines[0] == "- deployments/web (uid abc-123, created 2026-07-20T01:02:03Z)"
    assert any("background" in ln for ln in lines)
    args, kwargs = api.call_api.call_args_list[1]
    assert args[1] == "DELETE"
    assert ("dryRun", "All") in kwargs["query_params"]


async def test_preview_returns_none_on_api_error() -> None:
    """403 / admission failure / missing object: no preview, never an exception."""
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))
    with patch.object(client, "_api", api):
        assert await client.preview_scale(_deploy_meta(), "default", "web", 5) is None
        assert await client.preview_rollout_restart(_deploy_meta(), "default", "web") is None
        assert await client.preview_delete(_deploy_meta(), "default", "web") is None


async def test_preview_scale_no_change_yields_empty() -> None:
    client = KubeClient()
    api = MagicMock()
    same = {"spec": {"replicas": 3}}
    api.call_api = AsyncMock(side_effect=[_resp(same), _resp(same)])
    with patch.object(client, "_api", api):
        lines = await client.preview_scale(_deploy_meta(), "default", "web", 3)
    assert lines == []


async def test_write_ops_defaults_return_none() -> None:
    """The ABC defaults keep existing WriteOps implementations valid: no
    preview support means the dialog falls back to the synthesized string."""

    class Minimal(WriteOps):
        async def delete_object(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # signature match not under test
            pass

        async def scale_object(self, meta, namespace, name, replicas, *, uid=None):  # type: ignore[no-untyped-def]  # signature match not under test
            pass

        async def rollout_restart(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # signature match not under test
            pass

        async def replace_object(self, meta, namespace, name, manifest, *, uid=None):  # type: ignore[no-untyped-def]  # signature match not under test
            pass

    ops = Minimal()
    meta = _deploy_meta()
    assert await ops.preview_scale(meta, "default", "web", 5) is None
    assert await ops.preview_rollout_restart(meta, "default", "web") is None
    assert await ops.preview_delete(meta, "default", "web") is None


async def test_preview_scale_pins_uid_precondition() -> None:
    """The dry-run must replay the request being approved: with a captured
    uid the preview patch carries the same metadata.uid precondition as the
    real write, so a same-named replacement fails the preview (409 -> None)
    instead of rendering a diff the approved write can never apply."""
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(
        side_effect=[
            _resp({"spec": {"replicas": 3}}),
            _resp({"spec": {"replicas": 5}}),
        ]
    )
    with patch.object(client, "_api", api):
        lines = await client.preview_scale(_deploy_meta(), "default", "web", 5, uid="u-1")
    assert lines == ["~ spec.replicas: 3 -> 5"]
    _, kwargs = api.call_api.call_args_list[1]
    assert kwargs["body"] == {"spec": {"replicas": 5}, "metadata": {"uid": "u-1"}}


async def test_preview_rollout_restart_pins_uid_precondition() -> None:
    client = KubeClient()
    api = MagicMock()
    current = {"metadata": {"name": "web"}, "spec": {"template": {"metadata": {}}}}
    api.call_api = AsyncMock(side_effect=[_resp(current), _resp(current)])
    with patch.object(client, "_api", api):
        await client.preview_rollout_restart(_deploy_meta(), "default", "web", uid="u-1")
    _, kwargs = api.call_api.call_args_list[1]
    assert kwargs["body"]["metadata"] == {"uid": "u-1"}


async def test_preview_delete_pins_uid_precondition() -> None:
    """The dry-run DELETE carries the same DeleteOptions preconditions body
    as the real delete: a replacement object is rejected server-side and the
    preview degrades to None instead of summarizing the wrong incarnation."""
    client = KubeClient()
    api = MagicMock()
    manifest = {"metadata": {"name": "web", "uid": "u-1", "creationTimestamp": "t"}}
    api.call_api = AsyncMock(side_effect=[_resp(manifest), _resp({"kind": "Status"})])
    with patch.object(client, "_api", api):
        lines = await client.preview_delete(_deploy_meta(), "default", "web", uid="u-1")
    assert lines is not None
    _, kwargs = api.call_api.call_args_list[1]
    assert kwargs["body"] == {
        "propagationPolicy": "Background",
        "preconditions": {"uid": "u-1"},
    }
    assert kwargs["header_params"]["Content-Type"] == "application/json"


async def test_preview_delete_replacement_returns_none() -> None:
    """A 409 from the uid precondition (object was recreated) yields no
    preview - the dialog then opens without one rather than showing the
    replacement's identity."""
    client = KubeClient()
    api = MagicMock()
    manifest = {"metadata": {"name": "web", "uid": "u-2", "creationTimestamp": "t"}}
    api.call_api = AsyncMock(
        side_effect=[_resp(manifest), ApiException(status=409, reason="Conflict")]
    )
    with patch.object(client, "_api", api):
        assert await client.preview_delete(_deploy_meta(), "default", "web", uid="u-1") is None


async def test_preview_and_write_share_caller_provided_restart_stamp() -> None:
    """Exact-replay guarantee: the caller generates one restartedAt stamp per
    approval request and passes the same value to the preview and to the
    executed write, so the diff shown is byte-identical to what runs."""
    client = KubeClient()
    api = MagicMock()
    current = {"metadata": {"name": "web"}, "spec": {"template": {"metadata": {}}}}
    api.call_api = AsyncMock(side_effect=[_resp(current), _resp(current), _resp(current)])
    stamp = "2026-07-26T00:00:00+00:00"
    with patch.object(client, "_api", api):
        await client.preview_rollout_restart(
            _deploy_meta(), "default", "web", uid="u-1", restarted_at=stamp
        )
        await client.rollout_restart_with_stamp(
            _deploy_meta(), "default", "web", uid="u-1", restarted_at=stamp
        )
    preview_body = api.call_api.call_args_list[1][1]["body"]
    write_body = api.call_api.call_args_list[2][1]["body"]
    assert preview_body == write_body
    annotations = write_body["spec"]["template"]["metadata"]["annotations"]
    assert annotations["kubectl.kubernetes.io/restartedAt"] == stamp


async def test_stamp_hook_delegates_to_legacy_rollout_restart() -> None:
    """Compatibility: a WriteOps subclass implementing only the original
    abstract signature must keep working when approval executes through the
    timestamp-aware hook - the default hook drops the stamp and delegates."""

    class Legacy(WriteOps):
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        async def delete_object(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # signature match not under test
            pass

        async def scale_object(self, meta, namespace, name, replicas, *, uid=None):  # type: ignore[no-untyped-def]  # signature match not under test
            pass

        async def rollout_restart(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # signature match not under test
            self.calls.append(("restart", namespace, name, uid))

        async def replace_object(self, meta, namespace, name, manifest, *, uid=None):  # type: ignore[no-untyped-def]  # signature match not under test
            pass

    ops = Legacy()
    await ops.rollout_restart_with_stamp(
        _deploy_meta(), "default", "web", uid="u-1", restarted_at="stamp"
    )
    assert ops.calls == [("restart", "default", "web", "u-1")]


async def test_preview_scale_binds_dry_run_to_get_snapshot() -> None:
    """The GET snapshot and the dry-run PATCH are bound by the snapshot's
    resourceVersion (an apiserver optimistic-concurrency precondition): a
    concurrent update (e.g. an HPA) turns into a 409 and no preview, never a
    diff that mixes two revisions the server never evaluated together."""
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(
        side_effect=[
            _resp({"metadata": {"resourceVersion": "42"}, "spec": {"replicas": 3}}),
            _resp({"metadata": {"resourceVersion": "43"}, "spec": {"replicas": 5}}),
        ]
    )
    with patch.object(client, "_api", api):
        await client.preview_scale(_deploy_meta(), "default", "web", 5, uid="u-1")
    body = api.call_api.call_args_list[1][1]["body"]
    assert body["metadata"] == {"uid": "u-1", "resourceVersion": "42"}


async def test_preview_rollout_restart_binds_dry_run_to_get_snapshot() -> None:
    client = KubeClient()
    api = MagicMock()
    current = {"metadata": {"name": "web", "resourceVersion": "7"}, "spec": {}}
    api.call_api = AsyncMock(side_effect=[_resp(current), _resp(current)])
    with patch.object(client, "_api", api):
        await client.preview_rollout_restart(_deploy_meta(), "default", "web", uid="u-1")
    body = api.call_api.call_args_list[1][1]["body"]
    assert body["metadata"] == {"uid": "u-1", "resourceVersion": "7"}


async def test_preview_conflict_during_dry_run_returns_none() -> None:
    """A concurrent update between GET and dry-run: the resourceVersion
    precondition makes the server answer 409 and the preview degrades to
    None (dialog opens without it) instead of a mixed-revision diff."""
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(
        side_effect=[
            _resp({"metadata": {"resourceVersion": "42"}, "spec": {"replicas": 3}}),
            ApiException(status=409, reason="Conflict"),
        ]
    )
    with patch.object(client, "_api", api):
        assert await client.preview_scale(_deploy_meta(), "default", "web", 5) is None


async def test_preview_delete_binds_dry_run_to_get_snapshot() -> None:
    """The dry-run DELETE is pinned to the fetched revision so the summary
    always describes the object the server validated."""
    client = KubeClient()
    api = MagicMock()
    manifest = {
        "metadata": {"name": "web", "uid": "u-1", "creationTimestamp": "t", "resourceVersion": "9"}
    }
    api.call_api = AsyncMock(side_effect=[_resp(manifest), _resp({"kind": "Status"})])
    with patch.object(client, "_api", api):
        lines = await client.preview_delete(_deploy_meta(), "default", "web", uid="u-1")
    assert lines is not None
    body = api.call_api.call_args_list[1][1]["body"]
    assert body["preconditions"] == {"uid": "u-1", "resourceVersion": "9"}
