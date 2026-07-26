"""Cloud provider detection from node metadata (issue #30).

Detection is dynamic: `spec.providerID` scheme prefix plus well-known managed
node labels. There is no hardcoded annotation catalog anywhere — the detected
provider only *informs* the agent context and describe footers.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from kubernetes_asyncio.client.exceptions import ApiException

from korvid.k8s.client import KubeClient
from korvid.k8s.csp import ProviderInfo, detect_provider


def _node(provider_id: str | None = None, labels: dict[str, str] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"metadata": {"name": "n1", "labels": labels or {}}, "spec": {}}
    if provider_id is not None:
        node["spec"]["providerID"] = provider_id
    return node


# ---------------------------------------------------------------------------
# detect_provider (pure)
# ---------------------------------------------------------------------------


def test_azure_provider_id_prefix() -> None:
    info = detect_provider([_node("azure:///subscriptions/abc/resourceGroups/rg/vm")])
    assert info.provider == "azure"
    assert info.distribution is None
    assert info.display == "azure"


def test_aws_provider_id_prefix() -> None:
    info = detect_provider([_node("aws:///us-east-1a/i-0123456789")])
    assert info.provider == "aws"


def test_gce_provider_id_maps_to_gcp() -> None:
    info = detect_provider([_node("gce://my-project/us-central1-a/node-1")])
    assert info.provider == "gcp"


def test_aks_managed_label() -> None:
    info = detect_provider(
        [_node("azure:///vm", {"kubernetes.azure.com/cluster": "MC_rg_cluster_region"})]
    )
    assert info.provider == "azure"
    assert info.distribution == "aks"
    assert info.display == "aks"


def test_eks_managed_label() -> None:
    info = detect_provider([_node("aws:///i-1", {"eks.amazonaws.com/nodegroup": "ng-1"})])
    assert info.distribution == "eks"
    assert info.display == "eks"


def test_gke_managed_label() -> None:
    info = detect_provider([_node("gce://p/z/n", {"cloud.google.com/gke-nodepool": "default"})])
    assert info.distribution == "gke"


def test_managed_label_alone_implies_provider() -> None:
    """Some clusters omit providerID; the managed label still identifies the CSP."""
    info = detect_provider([_node(None, {"kubernetes.azure.com/cluster": "mc"})])
    assert info.provider == "azure"
    assert info.distribution == "aks"


def test_no_nodes_is_unknown() -> None:
    info = detect_provider([])
    assert info.provider == "unknown"
    assert info.distribution is None
    assert info.display == "unknown"
    assert not info.known


def test_no_provider_id_no_labels_is_unknown() -> None:
    info = detect_provider([_node()])
    assert info.provider == "unknown"


def test_unrecognized_scheme_is_unknown() -> None:
    info = detect_provider([_node("weirdcloud://x/y")])
    assert info.provider == "unknown"


def test_first_recognized_node_wins() -> None:
    """Mixed pools: any recognized node decides (skip nodes without providerID)."""
    info = detect_provider([_node(), _node("aws:///i-2")])
    assert info.provider == "aws"


def test_known_property() -> None:
    assert ProviderInfo("azure", None).known
    assert not ProviderInfo("unknown", None).known


# ---------------------------------------------------------------------------
# KubeClient.detect_cloud_provider (cached per connection)
# ---------------------------------------------------------------------------


def _node_list(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"items": list(nodes)}


async def test_detect_cloud_provider_lists_nodes() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_node.return_value = _node_list(_node("azure:///vm"))
    with patch.object(client, "_core_v1", fake_v1):
        info = await client.detect_cloud_provider()
    assert info.provider == "azure"


async def test_detect_cloud_provider_cached_per_connection() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_node.return_value = _node_list(_node("aws:///i-1"))
    with patch.object(client, "_core_v1", fake_v1):
        first = await client.detect_cloud_provider()
        second = await client.detect_cloud_provider()
    assert first == second
    assert fake_v1.list_node.call_count == 1


async def test_detect_cloud_provider_rbac_denied_is_unknown() -> None:
    """No cluster-wide node list permission: unknown is a fine answer, cached."""
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_node.side_effect = ApiException(status=403, reason="Forbidden")
    with patch.object(client, "_core_v1", fake_v1):
        info = await client.detect_cloud_provider()
        again = await client.detect_cloud_provider()
    assert info.provider == "unknown"
    assert again.provider == "unknown"
    assert fake_v1.list_node.call_count == 1


async def test_connect_resets_provider_cache() -> None:
    """A new connection may target a different cluster: rediscover."""
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_node.return_value = _node_list(_node("azure:///vm"))
    with patch.object(client, "_core_v1", fake_v1):
        await client.detect_cloud_provider()
    with (
        patch("korvid.k8s.client.k8s_config.load_kube_config", AsyncMock()),
        patch("korvid.k8s.client.k8s_client.ApiClient", MagicMock()),
        patch("korvid.k8s.client.k8s_client.CoreV1Api", MagicMock()),
    ):
        await client.connect()
    fake_v1_2 = AsyncMock()
    fake_v1_2.list_node.return_value = _node_list(_node("gce://p/z/n"))
    with patch.object(client, "_core_v1", fake_v1_2):
        info = await client.detect_cloud_provider()
    assert info.provider == "gcp"


async def test_detect_cloud_provider_transport_error_is_unknown() -> None:
    """DNS/TLS/connection failures are not ApiExceptions; the best-effort
    probe must still answer (and cache) unknown instead of raising."""
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_node.side_effect = OSError("connection reset by peer")
    with patch.object(client, "_core_v1", fake_v1):
        info = await client.detect_cloud_provider()
        again = await client.detect_cloud_provider()
    assert info.provider == "unknown"
    assert again.provider == "unknown"
    assert fake_v1.list_node.call_count == 1
