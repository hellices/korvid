"""In-place pod resize via the pods/resize subresource (1.35 GA, issue #27)."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from korvid.k8s.client import KubeClient


def _resp(payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.read = AsyncMock(return_value=json.dumps(payload).encode())
    return resp


_RESOURCES = {
    "app": {
        "requests": {"cpu": "200m", "memory": "256Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }
}


async def test_resize_pod_patches_resize_subresource() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(return_value=_resp({}))
    with patch.object(client, "_api", api):
        await client.resize_pod("default", "web-1", _RESOURCES, uid="u1")
    args, kwargs = api.call_api.call_args
    assert args[0] == "/api/v1/namespaces/default/pods/web-1/resize"
    assert args[1] == "PATCH"
    assert kwargs["body"] == {
        "metadata": {"uid": "u1"},
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "256Mi"},
                        "limits": {"cpu": "500m", "memory": "512Mi"},
                    },
                }
            ]
        },
    }
    assert kwargs["header_params"]["Content-Type"] == "application/strategic-merge-patch+json"


async def test_resize_pod_without_uid_omits_metadata() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(return_value=_resp({}))
    with patch.object(client, "_api", api):
        await client.resize_pod("default", "web-1", _RESOURCES)
    _, kwargs = api.call_api.call_args
    assert "metadata" not in kwargs["body"]


async def test_preview_resize_diffs_pod() -> None:
    client = KubeClient()
    api = MagicMock()
    current = {
        "metadata": {"name": "web-1", "resourceVersion": "42"},
        "spec": {"containers": [{"name": "app", "resources": {"requests": {"cpu": "100m"}}}]},
    }
    proposed = {
        "metadata": {"name": "web-1"},
        "spec": {"containers": [{"name": "app", "resources": {"requests": {"cpu": "200m"}}}]},
    }
    api.call_api = AsyncMock(side_effect=[_resp(current), _resp(proposed)])
    with patch.object(client, "_api", api):
        lines = await client.preview_resize(
            "default", "web-1", {"app": {"requests": {"cpu": "200m"}}}
        )
    # diff_manifests compares lists atomically; one changed-containers line
    assert lines is not None
    assert len(lines) == 1
    assert lines[0].startswith("~ spec.containers:")
    assert "200m" in lines[0]
    args, kwargs = api.call_api.call_args_list[1]
    assert args[0] == "/api/v1/namespaces/default/pods/web-1/resize"
    assert ("dryRun", "All") in kwargs["query_params"]
    # pinned to the GET snapshot so a concurrent update turns into a 409
    assert kwargs["body"]["metadata"]["resourceVersion"] == "42"


async def test_preview_resize_returns_none_on_failure() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(client, "_api", api):
        assert await client.preview_resize("default", "web-1", {}) is None


async def test_supports_pod_resize_true_when_discovered() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(
        return_value=_resp(
            {"resources": [{"name": "pods"}, {"name": "pods/resize", "verbs": ["patch"]}]}
        )
    )
    with patch.object(client, "_api", api):
        assert await client.supports_pod_resize() is True
        # cached: no second discovery round trip
        assert await client.supports_pod_resize() is True
    assert api.call_api.call_count == 1


async def test_supports_pod_resize_false_when_absent() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(return_value=_resp({"resources": [{"name": "pods"}]}))
    with patch.object(client, "_api", api):
        assert await client.supports_pod_resize() is False


async def test_supports_pod_resize_false_on_error_and_not_cached() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(side_effect=RuntimeError("down"))
    with patch.object(client, "_api", api):
        assert await client.supports_pod_resize() is False
    api.call_api = AsyncMock(
        return_value=_resp({"resources": [{"name": "pods/resize", "verbs": ["patch"]}]})
    )
    with patch.object(client, "_api", api):
        # a transient failure must not permanently disable the feature
        assert await client.supports_pod_resize() is True


async def test_supports_pod_resize_false_without_patch_verb() -> None:
    """Discovery may list the subresource without advertising patch (an
    aggregated or restricted apiserver); resize_pod would then always fail."""
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(
        return_value=_resp({"resources": [{"name": "pods/resize", "verbs": ["get"]}]})
    )
    with patch.object(client, "_api", api):
        assert await client.supports_pod_resize() is False


@pytest.mark.asyncio
async def test_connect_resets_resize_discovery_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnecting (possibly to another cluster) must invalidate the cache."""

    async def fake_load(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("korvid.k8s.client.k8s_config.load_kube_config", fake_load)
    monkeypatch.setattr("korvid.k8s.client.k8s_client.ApiClient", lambda: object())
    monkeypatch.setattr("korvid.k8s.client.k8s_client.CoreV1Api", lambda api: object())
    client = KubeClient()
    client._pod_resize_supported = True
    await client.connect(context="other-cluster")
    assert client._pod_resize_supported is None
