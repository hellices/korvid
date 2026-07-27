"""Node cordon / uncordon / drain transport (issue #40): cordon patches
``spec.unschedulable``, drain enumerates pods + PDBs into a DrainPlan and
evicts through the Eviction API (429 = PDB refused)."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from korvid.k8s.client import KubeClient
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.writes import WriteOps


def _resp(payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.read = AsyncMock(return_value=json.dumps(payload).encode())
    return resp


async def test_cordon_node_patches_unschedulable() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(return_value=_resp({}))
    with patch.object(client, "_api", api):
        await client.cordon_node("worker-1", True, uid="u1")
    args, kwargs = api.call_api.call_args
    assert args[0] == "/api/v1/nodes/worker-1"
    assert args[1] == "PATCH"
    assert kwargs["body"] == {"metadata": {"uid": "u1"}, "spec": {"unschedulable": True}}
    assert kwargs["header_params"]["Content-Type"] == "application/strategic-merge-patch+json"


async def test_uncordon_node_clears_unschedulable_without_uid() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(return_value=_resp({}))
    with patch.object(client, "_api", api):
        await client.cordon_node("worker-1", False)
    _, kwargs = api.call_api.call_args
    assert kwargs["body"] == {"spec": {"unschedulable": False}}


async def test_preview_cordon_diffs_node() -> None:
    client = KubeClient()
    api = MagicMock()
    current = {
        "metadata": {"name": "worker-1", "resourceVersion": "7"},
        "spec": {},
    }
    proposed = {
        "metadata": {"name": "worker-1"},
        "spec": {"unschedulable": True},
    }
    api.call_api = AsyncMock(side_effect=[_resp(current), _resp(proposed)])
    with patch.object(client, "_api", api):
        lines = await client.preview_cordon("worker-1", True)
    assert lines is not None
    assert any("unschedulable" in line for line in lines)
    args, kwargs = api.call_api.call_args_list[1]
    assert args[0] == "/api/v1/nodes/worker-1"
    assert ("dryRun", "All") in kwargs["query_params"]
    # pinned to the GET snapshot so a concurrent update turns into a 409
    assert kwargs["body"]["metadata"]["resourceVersion"] == "7"


async def test_preview_cordon_returns_none_on_failure() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(client, "_api", api):
        assert await client.preview_cordon("worker-1", True) is None


async def test_evict_pod_posts_eviction() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(return_value=_resp({}))
    with patch.object(client, "_api", api):
        await client.evict_pod("default", "web-1", uid="u1")
    args, kwargs = api.call_api.call_args
    assert args[0] == "/api/v1/namespaces/default/pods/web-1/eviction"
    assert args[1] == "POST"
    assert kwargs["body"] == {
        "apiVersion": "policy/v1",
        "kind": "Eviction",
        "metadata": {"name": "web-1", "namespace": "default"},
        "deleteOptions": {"preconditions": {"uid": "u1"}},
    }


async def test_evict_pod_without_uid_omits_preconditions() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(return_value=_resp({}))
    with patch.object(client, "_api", api):
        await client.evict_pod("default", "web-1")
    _, kwargs = api.call_api.call_args
    assert kwargs["body"]["deleteOptions"] == {}


async def test_evict_pod_surfaces_api_status() -> None:
    from kubernetes_asyncio.client.exceptions import ApiException

    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(side_effect=ApiException(status=429, reason="Too Many Requests"))
    with (
        patch.object(client, "_api", api),
        pytest.raises(ApiStatusError, match="429") as excinfo,
    ):
        await client.evict_pod("default", "web-1")
    assert excinfo.value.status == 429


async def test_drain_plan_fetches_node_pods_and_pdbs() -> None:
    client = KubeClient()
    api = MagicMock()
    pods = {
        "items": [
            {
                "metadata": {"name": "web-1", "namespace": "default", "uid": "u1"},
                "spec": {},
                "status": {"phase": "Running"},
            }
        ]
    }
    pdbs: dict[str, Any] = {"items": []}
    api.call_api = AsyncMock(side_effect=[_resp(pods), _resp(pdbs)])
    with patch.object(client, "_api", api):
        plan = await client.drain_plan("worker-1")
    assert len(plan.targets) == 1
    assert plan.targets[0].name == "web-1"
    pods_call = api.call_api.call_args_list[0]
    assert pods_call.args[0] == "/api/v1/pods"
    assert ("fieldSelector", "spec.nodeName=worker-1") in pods_call.kwargs["query_params"]
    pdbs_call = api.call_api.call_args_list[1]
    assert pdbs_call.args[0] == "/apis/policy/v1/poddisruptionbudgets"


async def test_drain_plan_tolerates_missing_pdb_api() -> None:
    from kubernetes_asyncio.client.exceptions import ApiException

    client = KubeClient()
    api = MagicMock()
    pods: dict[str, Any] = {"items": []}
    api.call_api = AsyncMock(
        side_effect=[_resp(pods), ApiException(status=404, reason="Not Found")]
    )
    with patch.object(client, "_api", api):
        plan = await client.drain_plan("worker-1")
    assert plan.targets == ()


async def test_writeops_defaults_reject_node_ops() -> None:
    class Minimal(WriteOps):
        async def delete_object(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # fake
            pass

        async def scale_object(self, meta, namespace, name, replicas, *, uid=None):  # type: ignore[no-untyped-def]  # fake
            pass

        async def rollout_restart(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # fake
            pass

        async def replace_object(self, meta, namespace, name, manifest, *, uid=None):  # type: ignore[no-untyped-def]  # fake
            pass

    ops = Minimal()
    with pytest.raises(NotImplementedError, match="cordon"):
        await ops.cordon_node("n", True)
    with pytest.raises(NotImplementedError, match="evict"):
        await ops.evict_pod("ns", "p")
    with pytest.raises(NotImplementedError, match="drain"):
        await ops.drain_plan("n")
    assert await ops.preview_cordon("n", True) is None


async def test_drain_plan_propagates_non_404_pdb_failure() -> None:
    """Auth failure (or any PDB-list failure other than 404/403) must abort
    the plan rather than silently presenting a falsely PDB-aware preview."""
    from kubernetes_asyncio.client.exceptions import ApiException

    client = KubeClient()
    api = MagicMock()
    pods: dict[str, Any] = {"items": []}
    api.call_api = AsyncMock(
        side_effect=[_resp(pods), ApiException(status=401, reason="Unauthorized")]
    )
    with patch.object(client, "_api", api), pytest.raises(ApiStatusError, match="401"):
        await client.drain_plan("worker-1")


async def test_drain_plan_falls_back_to_namespaced_pdbs_on_403() -> None:
    """A namespace-scoped user cannot list PDBs cluster-wide (403); the
    plan retries per namespace of the drained pods so up-front PDB warnings
    still work."""
    from kubernetes_asyncio.client.exceptions import ApiException

    client = KubeClient()
    api = MagicMock()
    pods = {
        "items": [
            {
                "metadata": {"name": "web-1", "namespace": "team-a", "uid": "u1"},
                "spec": {},
                "status": {"phase": "Running"},
            }
        ]
    }
    pdb = {
        "metadata": {"name": "web-pdb", "namespace": "team-a"},
        "spec": {"selector": {}},
        "status": {"disruptionsAllowed": 0},
    }
    api.call_api = AsyncMock(
        side_effect=[
            _resp(pods),
            ApiException(status=403, reason="Forbidden"),
            _resp({"items": [pdb]}),
        ]
    )
    with patch.object(client, "_api", api):
        plan = await client.drain_plan("worker-1")
    ns_call = api.call_api.call_args_list[2]
    assert ns_call.args[0] == "/apis/policy/v1/namespaces/team-a/poddisruptionbudgets"
    assert plan.targets[0].pdb_blocked == "web-pdb"
