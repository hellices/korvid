"""Tests for ResourceMeta, build_alias_map, and KubeClient.discover_resources."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.errors import ApiStatusError


def test_api_base_core_and_group() -> None:
    assert PODS_META.api_base == "/api/v1"
    deploy = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
    assert deploy.api_base == "/apis/apps/v1"


def test_alias_map_covers_plural_kind_and_shortnames() -> None:
    deploy = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
    aliases = build_alias_map([deploy])
    assert aliases["deployments"] is deploy
    assert aliases["deployment"] is deploy
    assert aliases["deploy"] is deploy


def test_alias_map_first_meta_wins_on_conflict() -> None:
    a = ResourceMeta("Foo", "foos", "a.io", "v1", True, ("f",))
    b = ResourceMeta("Bar", "bars", "b.io", "v1", True, ("f",))
    aliases = build_alias_map([a, b])
    assert aliases["f"] is a  # deterministic: earlier discovery order wins


_CORE: dict[str, Any] = {
    "resources": [
        {
            "name": "pods",
            "kind": "Pod",
            "namespaced": True,
            "shortNames": ["po"],
            "verbs": ["list", "watch", "get"],
        },
        {"name": "pods/log", "kind": "Pod", "namespaced": True, "verbs": ["get"]},
    ]
}
_APIS: dict[str, Any] = {"groups": [{"name": "apps", "preferredVersion": {"version": "v1"}}]}
_APPS: dict[str, Any] = {
    "resources": [
        {
            "name": "deployments",
            "kind": "Deployment",
            "namespaced": True,
            "shortNames": ["deploy"],
            "verbs": ["list", "watch"],
        },
    ]
}


async def test_discover_resources_filters_subresources_and_non_watchable() -> None:
    client = KubeClient()
    responses: dict[str, dict[str, Any]] = {
        "/api/v1": _CORE,
        "/apis": _APIS,
        "/apis/apps/v1": _APPS,
    }

    async def fake_request(path: str) -> dict[str, Any]:
        return responses[path]

    with patch.object(client, "_request_json", side_effect=fake_request):
        metas = await client.discover_resources()
    by_plural = {m.plural: m for m in metas}
    assert by_plural["pods"].shortnames == ("po",)
    assert by_plural["deployments"].group == "apps"
    assert "pods/log" not in by_plural  # subresources excluded


async def test_discover_resources_skips_broken_group() -> None:
    """A broken aggregated API (ApiStatusError) is skipped, not fatal."""
    client = KubeClient()

    async def fake_request(path: str) -> dict[str, Any]:
        if path == "/api/v1":
            return _CORE
        if path == "/apis":
            return {"groups": [{"name": "broken.io", "preferredVersion": {"version": "v1"}}]}
        raise ApiStatusError(503, "Service Unavailable")

    with patch.object(client, "_request_json", side_effect=fake_request):
        metas = await client.discover_resources()
    assert any(m.plural == "pods" for m in metas)


async def test_request_json_wraps_api_exception_as_api_status_error() -> None:
    """_request_json must wrap ApiException as ApiStatusError."""
    from kubernetes_asyncio.client.exceptions import ApiException

    client = KubeClient()
    fake_api = AsyncMock()
    fake_api.call_api.side_effect = ApiException(status=403, reason="Forbidden")
    client._api = fake_api

    with pytest.raises(ApiStatusError, match="API 403: Forbidden"):
        await client._request_json("/api/v1")


async def test_request_json_raises_runtime_error_when_not_connected() -> None:
    """_request_json raises RuntimeError if connect() was not called."""
    client = KubeClient()
    with pytest.raises(RuntimeError, match="connect"):
        await client._request_json("/api/v1")
