"""Tests for ResourceMeta, build_alias_map, and KubeClient.discover_resources."""

from __future__ import annotations

import asyncio
import ssl
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientConnectionError

from korvid.k8s import errors as errors_mod
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.errors import ApiStatusError, KubeClientError


def test_api_base_core_and_group() -> None:
    assert PODS_META.api_base == "/api/v1"
    deploy = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
    assert deploy.api_base == "/apis/apps/v1"


def test_kube_client_error_is_a_k8s_layer_exception() -> None:
    error_type = getattr(errors_mod, "KubeClientError", None)
    assert isinstance(error_type, type)
    assert issubclass(error_type, Exception)


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


def test_same_plural_resources_keep_qualified_aliases() -> None:
    native = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
    custom = ResourceMeta("Deployment", "deployments", "example.io", "v1", True)
    aliases = build_alias_map([custom, native])
    assert aliases["deployments"] is custom
    assert aliases["deployments.apps"] is native
    assert aliases["deployments.example.io"] is custom


def test_qualified_names_cannot_be_shadowed_by_shortnames() -> None:
    custom = ResourceMeta("Custom", "customs", "example.io", "v1", True, ("deployments.apps",))
    native = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
    aliases = build_alias_map([custom, native])
    assert aliases["deployments.apps"] is native


def test_synthetic_helm_and_flux_have_independent_aliases() -> None:
    from korvid.k8s.helm import HELM_RELEASES_META

    flux = ResourceMeta(
        "HelmRelease", "helmreleases", "helm.toolkit.fluxcd.io", "v2", True, ("hr",)
    )
    aliases = build_alias_map([HELM_RELEASES_META, flux])
    assert aliases["helmreleases"] is HELM_RELEASES_META
    assert aliases["helmreleases.helm.toolkit.fluxcd.io"] is flux
    assert aliases["hr"] is flux


def test_resource_lookup_preserves_group_and_synthetic_identity() -> None:
    from korvid.k8s.discovery import canonical_resource_alias, resolve_resource
    from korvid.k8s.helm import HELM_RELEASES_META

    flux = ResourceMeta("HelmRelease", "helmreleases", "helm.toolkit.fluxcd.io", "v2", True)
    aliases = build_alias_map([HELM_RELEASES_META, flux])
    assert resolve_resource(aliases, flux.group, flux.plural) is flux
    assert resolve_resource(aliases, "", "helmreleases") is None
    assert resolve_resource(aliases, "", "helmreleases", synthetic=True) is HELM_RELEASES_META
    assert canonical_resource_alias(aliases, flux) == "helmreleases.helm.toolkit.fluxcd.io"
    assert canonical_resource_alias(aliases, HELM_RELEASES_META) == "helmreleases"


def test_resource_lookup_uses_identity_not_a_colliding_bare_alias() -> None:
    from korvid.k8s.discovery import canonical_resource_alias, resolve_resource

    native = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
    custom = ResourceMeta("Deployment", "deployments", "example.io", "v1", True)
    aliases = build_alias_map([custom, native])
    assert resolve_resource(aliases, "apps", "deployments") is native
    assert canonical_resource_alias(aliases, native) == "deployments.apps"
    assert canonical_resource_alias(aliases, custom) == "deployments"


def test_partial_alias_map_selection_is_independent_of_insertion_order() -> None:
    from korvid.k8s.discovery import canonical_resource_alias

    aliases = {"pod": PODS_META, "po": PODS_META}
    reverse = dict(reversed(list(aliases.items())))
    assert canonical_resource_alias(aliases, PODS_META) == "po"
    assert canonical_resource_alias(reverse, PODS_META) == "po"


def test_canonical_alias_does_not_fabricate_an_undiscovered_view() -> None:
    from korvid.k8s.discovery import canonical_resource_alias

    with pytest.raises(ValueError, match="not discovered"):
        canonical_resource_alias({}, PODS_META)


def test_resource_configuration_prefers_qualified_keys_and_preserves_empty_values() -> None:
    meta = ResourceMeta("HelmRelease", "helmreleases", "helm.toolkit.fluxcd.io", "v2", True)
    configured: dict[str, tuple[str, ...]] = {
        "helmreleases": ("bare",),
        meta.qualified_name: ("qualified",),
    }
    assert meta.configured_value(configured) == ("qualified",)
    configured[meta.qualified_name] = ()
    assert meta.configured_value(configured) == ()
    del configured[meta.qualified_name]
    assert meta.configured_value(configured) == ("bare",)
    assert meta.configured_value({}) is None


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
    assert by_plural["pods"].watchable
    assert by_plural["deployments"].group == "apps"
    assert "pods/log" not in by_plural  # subresources excluded


async def test_discover_resources_keeps_list_only_kinds_as_unwatchable() -> None:
    """Aggregated APIs like OLM's packageserver serve list but not watch
    (issue #141): the kind must still be discovered - marked unwatchable so
    the watch source polls instead - and kinds without even `list` stay
    excluded."""
    client = KubeClient()
    packages: dict[str, Any] = {
        "resources": [
            {
                "name": "packagemanifests",
                "kind": "PackageManifest",
                "namespaced": True,
                "verbs": ["get", "list"],
            },
            {"name": "peeks", "kind": "Peek", "namespaced": True, "verbs": ["get"]},
        ]
    }
    responses: dict[str, dict[str, Any]] = {
        "/api/v1": _CORE,
        "/apis": {
            "groups": [
                {"name": "packages.operators.coreos.com", "preferredVersion": {"version": "v1"}}
            ]
        },
        "/apis/packages.operators.coreos.com/v1": packages,
    }

    async def fake_request(path: str) -> dict[str, Any]:
        return responses[path]

    with patch.object(client, "_request_json", side_effect=fake_request):
        metas = await client.discover_resources()
    by_plural = {m.plural: m for m in metas}
    assert "packagemanifests" in by_plural
    assert not by_plural["packagemanifests"].watchable
    assert "peeks" not in by_plural  # no list verb: not a view


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ApiStatusError(503, "Service Unavailable"), id="api-status"),
        pytest.param(
            KubeClientError(
                "Kubernetes API connection failed; check cluster connectivity and retry"
            ),
            id="client-error",
        ),
    ],
)
async def test_discover_resources_skips_broken_group(error: Exception) -> None:
    """A broken aggregated API group is skipped without hiding core failures."""
    client = KubeClient()

    async def fake_request(path: str) -> dict[str, Any]:
        if path == "/api/v1":
            return _CORE
        if path == "/apis":
            return {"groups": [{"name": "broken.io", "preferredVersion": {"version": "v1"}}]}
        raise error

    with patch.object(client, "_request_json", side_effect=fake_request):
        metas = await client.discover_resources()
    assert any(m.plural == "pods" for m in metas)


async def test_discover_resources_skips_malformed_entries() -> None:
    """Entries missing name/kind/namespaced are skipped instead of raising KeyError."""
    client = KubeClient()
    core: dict[str, Any] = {
        "resources": [
            {"kind": "Broken", "namespaced": True, "verbs": ["list", "watch"]},  # no name
            {"name": "broken2", "namespaced": True, "verbs": ["list", "watch"]},  # no kind
            {"name": "broken3", "kind": "Broken3", "verbs": ["list", "watch"]},  # no namespaced
            {"name": "pods", "kind": "Pod", "namespaced": True, "verbs": ["list", "watch"]},
        ]
    }

    async def fake_request(path: str) -> dict[str, Any]:
        if path == "/api/v1":
            return core
        return {"groups": []}

    with patch.object(client, "_request_json", side_effect=fake_request):
        metas = await client.discover_resources()
    assert [m.plural for m in metas] == ["pods"]


async def test_discover_resources_skips_group_without_name() -> None:
    """A group with a preferredVersion but no valid name is skipped, not fatal."""
    client = KubeClient()

    async def fake_request(path: str) -> dict[str, Any]:
        if path == "/api/v1":
            return _CORE
        if path == "/apis":
            return {
                "groups": [
                    {"preferredVersion": {"version": "v1"}},  # no name
                    {"name": 42, "preferredVersion": {"version": "v1"}},  # non-str name
                ]
            }
        raise AssertionError(f"unexpected request: {path}")

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


@pytest.mark.parametrize(
    ("error", "message"),
    [
        pytest.param(
            ClientConnectionError("https://user:token@cluster/private"),
            "Kubernetes API connection failed; check cluster connectivity and retry",
            id="aiohttp",
        ),
        pytest.param(
            ssl.SSLError("certificate contains private details"),
            "Kubernetes API TLS validation failed; check cluster certificates and retry",
            id="ssl",
        ),
        pytest.param(
            TimeoutError("https://user:token@cluster/private"),
            "Kubernetes API request timed out; check cluster connectivity and retry",
            id="timeout",
        ),
        pytest.param(
            ConnectionResetError("response body contained a credential"),
            "Kubernetes API connection failed; check cluster connectivity and retry",
            id="os",
        ),
    ],
)
async def test_request_json_normalizes_transport_failures_without_leaking_details(
    error: Exception, message: str
) -> None:
    client = KubeClient()
    fake_api = AsyncMock()
    fake_api.call_api.side_effect = error
    client._api = fake_api

    with pytest.raises(KubeClientError) as excinfo:
        await client._request_json("/api/v1")

    assert str(excinfo.value) == message


async def test_request_json_normalizes_invalid_json_without_leaking_the_body() -> None:
    client = KubeClient()
    response = AsyncMock()
    response.status = 200
    response.reason = "OK"
    response.read.return_value = b'{"token":"secret"'
    fake_api = AsyncMock()
    fake_api.call_api.return_value = response
    client._api = fake_api

    with pytest.raises(KubeClientError) as excinfo:
        await client._request_json("/api/v1")

    assert str(excinfo.value) == (
        "Kubernetes API returned malformed JSON; retry, then check the API server"
    )


async def test_request_json_rejects_a_valid_non_object_response() -> None:
    client = KubeClient()
    response = AsyncMock()
    response.status = 200
    response.reason = "OK"
    response.read.return_value = b'["credential", "secret"]'
    fake_api = AsyncMock()
    fake_api.call_api.return_value = response
    client._api = fake_api

    with pytest.raises(KubeClientError) as excinfo:
        await client._request_json("/api/v1")

    assert str(excinfo.value) == (
        "Kubernetes API returned a malformed response; retry, then check the API server"
    )


async def test_request_json_preserves_http_status_before_decoding() -> None:
    client = KubeClient()
    response = AsyncMock()
    response.status = 503
    response.reason = "Service Unavailable"
    response.read.return_value = b"not-json"
    fake_api = AsyncMock()
    fake_api.call_api.return_value = response
    client._api = fake_api

    with pytest.raises(ApiStatusError, match="API 503: Service Unavailable") as excinfo:
        await client._request_json("/api/v1")

    assert excinfo.value.body == "not-json"


async def test_request_json_propagates_cancellation() -> None:
    client = KubeClient()
    fake_api = AsyncMock()
    fake_api.call_api.side_effect = asyncio.CancelledError()
    client._api = fake_api

    with pytest.raises(asyncio.CancelledError):
        await client._request_json("/api/v1")


async def test_request_json_raises_runtime_error_when_not_connected() -> None:
    """_request_json raises RuntimeError if connect() was not called."""
    client = KubeClient()
    with pytest.raises(RuntimeError, match="connect"):
        await client._request_json("/api/v1")
