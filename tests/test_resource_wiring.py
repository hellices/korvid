import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from korvid.__main__ import _discover_in_background, _make_get_manifest, _make_watch_source
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map, canonical_resource_alias
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META, HelmReleaseSummary
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.watch_events import WatchEvent, WatchProgress
from tests.k8s.test_client import _FakeWatch
from tests.k8s.test_helm import _secret
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
    ) -> AsyncGenerator[WatchEvent[PodSummary | GenericSummary], None]:
        self.watched.append((meta, namespace))
        yield (
            "SNAPSHOT",
            GenericSummary(name="web", namespace="default", kind=meta.kind, created=""),
        )
        yield WatchProgress.LIVE_EVENT

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
    progress: list[WatchProgress] = []
    for meta in metas:
        key = canonical_resource_alias(aliases, meta)
        async for item in source(key, "*"):
            if isinstance(item, WatchProgress):
                progress.append(item)
                continue
            event, summary = item
            store.apply_event(key, "*", event, summary)
    assert client.watched == [(meta, None) for meta in metas]
    assert progress == [WatchProgress.LIVE_EVENT] * len(metas)
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


@pytest.mark.parametrize("revisions", [(1, 2), (2, 1)])
async def test_helm_snapshot_keeps_latest_revision_in_store(revisions: tuple[int, int]) -> None:
    client = KubeClient()
    snapshot = {
        "metadata": {"resourceVersion": "10"},
        "items": [_secret("web", revision) for revision in revisions],
    }
    store = ResourceStore()
    manager = WatchManager(store, _make_watch_source(client, build_alias_map([HELM_RELEASES_META])))
    latest_seen = asyncio.Event()

    def observe(kind: str, scope: str, event_type: str, summary: Summary) -> None:
        if isinstance(summary, HelmReleaseSummary) and summary.revision == 2:
            latest_seen.set()

    manager.on_event = observe
    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=snapshot)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=_FakeWatch([])),
    ):
        await manager.start("helmreleases", "default")
        try:
            await asyncio.wait_for(latest_seen.wait(), timeout=2.0)
            rows = store.get("helmreleases", "default")
            assert len(rows) == 1
            assert isinstance(rows[0], HelmReleaseSummary)
            assert rows[0].revision == 2
        finally:
            await manager.stop_all()
