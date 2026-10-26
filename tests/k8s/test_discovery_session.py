"""KubeClient keeps one discovery snapshot per session generation (#409, slice S2)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from kubernetes_asyncio import client as k8s_client

from korvid.k8s import client as client_module
from korvid.k8s import discovery_snapshot
from korvid.k8s.client import KubeClient

DOCUMENTS: dict[str, dict[str, Any]] = {
    "/api": {"versions": ["v1"]},
    "/api/v1": {
        "resources": [{"name": "pods", "kind": "Pod", "namespaced": True, "verbs": ["list"]}]
    },
    "/apis": {"groups": []},
}


async def _answer(path: str) -> dict[str, Any]:
    return DOCUMENTS[path]


class _Api:
    def __init__(self, configuration: Any) -> None:
        self.configuration = configuration

    async def close(self) -> None:
        return None


@pytest.fixture
def offline_kubeconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "load_refreshable_kube_config", AsyncMock())
    monkeypatch.setattr(k8s_client, "ApiClient", _Api)
    monkeypatch.setattr(k8s_client, "CoreV1Api", lambda api: api)
    monkeypatch.setattr(k8s_client.Configuration, "set_default", lambda configuration: None)


@pytest.mark.usefixtures("offline_kubeconfig")
async def test_connect_and_every_context_switch_start_a_new_session() -> None:
    client = KubeClient()
    assert client.session_generation == 0

    await client.connect("ctx-a")
    assert client.session_generation == 1
    await client.switch_context("ctx-b")
    assert client.session_generation == 2
    await client.switch_context(None)
    assert client.session_generation == 3


async def test_a_snapshot_is_reused_within_its_session_and_age() -> None:
    client = KubeClient()
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)) as fetch:
        first = await client.discovery_snapshot()
        second = await client.discovery_snapshot()

    assert second is first
    assert fetch.await_count == 3


async def test_concurrent_snapshot_requests_share_one_read() -> None:
    client = KubeClient()
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)) as fetch:
        first, second = await asyncio.gather(
            client.discovery_snapshot(), client.discovery_snapshot()
        )

    assert second is first
    assert fetch.await_count == 3


async def test_an_expired_snapshot_is_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(discovery_snapshot, "monotonic", lambda: clock[0])
    client = KubeClient()
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)):
        first = await client.discovery_snapshot()
        clock[0] += discovery_snapshot.DISCOVERY_TTL_SECONDS + 1
        second = await client.discovery_snapshot()

    assert second is not first


async def test_invalidated_discovery_is_read_again() -> None:
    client = KubeClient()
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)):
        first = await client.discovery_snapshot()
        client.invalidate_discovery()
        second = await client.discovery_snapshot()

    assert second is not first


@pytest.mark.usefixtures("offline_kubeconfig")
async def test_a_snapshot_from_an_earlier_session_is_never_reused() -> None:
    client = KubeClient()
    await client.connect("ctx-a")
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)):
        first = await client.discovery_snapshot()
        await client.switch_context("ctx-b")
        second = await client.discovery_snapshot()

    assert (first.generation, second.generation) == (1, 2)


async def test_a_snapshot_read_across_a_session_change_is_not_kept() -> None:
    client = KubeClient()

    async def answer(path: str) -> dict[str, Any]:
        if path == "/api":
            client._new_session()
        return DOCUMENTS[path]

    with patch.object(client, "_request_json", AsyncMock(side_effect=answer)) as fetch:
        stale = await client.discovery_snapshot()
        fetch.side_effect = _answer
        fresh = await client.discovery_snapshot()

    assert (stale.generation, fresh.generation) == (0, 1)


async def test_discover_resources_reads_afresh_and_keeps_the_snapshot() -> None:
    client = KubeClient()
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)) as fetch:
        await client.discovery_snapshot()
        metas = await client.discover_resources()
        snapshot = await client.discovery_snapshot()

    assert [meta.plural for meta in metas] == ["pods"]
    assert fetch.await_count == 6
    assert snapshot.generation == client.session_generation
