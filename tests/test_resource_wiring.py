from collections.abc import AsyncGenerator
from typing import Any

from korvid.__main__ import _discover_in_background, _make_get_manifest, _make_watch_source
from korvid.core.store import ResourceStore
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map, canonical_resource_alias
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META
from korvid.k8s.models import GenericSummary, PodSummary
from tests.ui.test_app import make_app

_FLUX = ResourceMeta("HelmRelease", "helmreleases", "helm.toolkit.fluxcd.io", "v2", True, ("hr",))


class ResourceClient(KubeClient):
    def __init__(self) -> None:
        super().__init__()
        self.watched: list[tuple[ResourceMeta, str | None]] = []
        self.fetched: list[ResourceMeta] = []

    async def discover_resources(self) -> list[ResourceMeta]:
        return [_FLUX]

    async def watch_resources(
        self, meta: ResourceMeta, namespace: str | None
    ) -> AsyncGenerator[tuple[str, PodSummary | GenericSummary], None]:
        self.watched.append((meta, namespace))
        yield (
            "SNAPSHOT",
            GenericSummary(name="web", namespace="default", kind=meta.kind, created=""),
        )

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        self.fetched.append(meta)
        return {"kind": meta.kind, "apiVersion": f"{meta.group}/{meta.version}"}


async def test_discovery_keeps_flux_alongside_synthetic_helm() -> None:
    client = ResourceClient()
    aliases = build_alias_map([PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META])
    await _discover_in_background(client, aliases, make_app([]))
    assert aliases["helmreleases"] is HELM_RELEASES_META
    assert aliases["helmreleases.helm.toolkit.fluxcd.io"] is _FLUX
    assert aliases["hr"] is _FLUX


async def test_one_watch_entrypoint_preserves_each_resource_identity() -> None:
    client = ResourceClient()
    metas = [PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META, _FLUX]
    aliases = build_alias_map(metas)
    source = _make_watch_source(client, aliases)
    store = ResourceStore()
    for meta in metas:
        key = canonical_resource_alias(aliases, meta)
        async for event, summary in source(key, "*"):
            store.apply_event(key, "*", event, summary)
    assert client.watched == [(meta, None) for meta in metas]
    assert len(store.get("helmreleases", "*")) == 1
    assert len(store.get("helmreleases.helm.toolkit.fluxcd.io", "*")) == 1
    store.clear("helmreleases", "*")
    assert len(store.get("helmreleases.helm.toolkit.fluxcd.io", "*")) == 1


async def test_manifest_dispatch_uses_metadata_not_helm_plural() -> None:
    client = ResourceClient()
    get_manifest = _make_get_manifest(client, build_alias_map([_FLUX]))
    result = await get_manifest("helmreleases", "default", "web")
    assert client.fetched == [_FLUX]
    assert result["apiVersion"] == "helm.toolkit.fluxcd.io/v2"
