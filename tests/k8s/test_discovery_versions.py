import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from korvid.k8s import client as client_module
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import build_alias_map
from korvid.k8s.errors import ApiStatusError, KubeClientError


def _resources(*plurals: str) -> dict[str, Any]:
    return {
        "resources": [
            {"name": plural, "kind": plural.title(), "namespaced": True, "verbs": ["list"]}
            for plural in plurals
        ]
    }


def _group(name: str, *versions: str, preferred: str = "v1") -> dict[str, Any]:
    return {
        "name": name,
        "preferredVersion": {"version": preferred, "groupVersion": f"{name}/{preferred}"},
        "versions": [
            {"version": version, "groupVersion": f"{name}/{version}"} for version in versions
        ],
    }


async def test_discovery_keeps_kinds_from_non_preferred_versions() -> None:
    client = KubeClient()
    responses = {
        "/api/v1": _resources("pods"),
        "/apis": {"groups": [_group("example.io", "v1beta1", "v1", "v1beta1")]},
        "/apis/example.io/v1": _resources("widgets"),
        "/apis/example.io/v1beta1": _resources("widgets", "gadgets"),
    }

    async def request(path: str) -> dict[str, Any]:
        if path == "/apis/example.io/v1":
            await asyncio.sleep(0)
        return responses[path]

    with patch.object(client, "_request_json", AsyncMock(side_effect=request)) as fetch:
        metas = await client.discover_resources()

    assert [(meta.plural, meta.version) for meta in metas] == [
        ("pods", "v1"),
        ("widgets", "v1"),
        ("gadgets", "v1beta1"),
    ]
    assert fetch.await_count == 4


@pytest.mark.parametrize("preferred", [None, {}, "broken", {"version": ""}])
async def test_valid_versions_survive_an_unusable_preferred_version(preferred: Any) -> None:
    client = KubeClient()
    group = _group("example.io", "v1beta1")
    group["preferredVersion"] = preferred
    responses = {
        "/api/v1": _resources(),
        "/apis": {"groups": [group]},
        "/apis/example.io/v1beta1": _resources("gadgets"),
    }
    with patch.object(client, "_request_json", AsyncMock(side_effect=responses.__getitem__)):
        metas = await client.discover_resources()

    assert [(meta.plural, meta.version) for meta in metas] == [("gadgets", "v1beta1")]


async def test_discovery_skips_malformed_and_duplicate_version_advertisements() -> None:
    client = KubeClient()
    group = _group("example.io", "v1", "v1beta1")
    group["versions"].extend(
        [
            None,
            "v2",
            {},
            {"version": 42},
            {"version": "../v2"},
            {"version": "v2", "groupVersion": "another.io/v2"},
            {"version": "v1beta1"},
        ]
    )
    responses = {
        "/api/v1": _resources(),
        "/apis": {"groups": [None, "bad", _group("../bad", "v1"), group, group]},
        "/apis/example.io/v1": _resources("widgets"),
        "/apis/example.io/v1beta1": _resources("gadgets"),
    }
    with patch.object(
        client, "_request_json", AsyncMock(side_effect=responses.__getitem__)
    ) as fetch:
        metas = await client.discover_resources()

    assert [meta.plural for meta in metas] == ["widgets", "gadgets"]
    assert fetch.await_count == 4


async def test_a_broken_preferred_version_does_not_hide_other_versions() -> None:
    client = KubeClient()
    responses = {
        "/api/v1": _resources("pods"),
        "/apis": {"groups": [_group("example.io", "v1", "v1beta1")]},
        "/apis/example.io/v1beta1": _resources("gadgets"),
    }

    async def request(path: str) -> dict[str, Any]:
        if path == "/apis/example.io/v1":
            raise ApiStatusError(503, "Service Unavailable")
        return responses[path]

    with patch.object(client, "_request_json", AsyncMock(side_effect=request)):
        metas = await client.discover_resources()

    assert [meta.plural for meta in metas] == ["pods", "gadgets"]


async def test_discovery_bounds_concurrent_version_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_DISCOVERY_CONCURRENCY", 3, raising=False)
    client = KubeClient()
    active = 0
    peak = 0

    async def request(path: str) -> dict[str, Any]:
        nonlocal active, peak
        if path == "/api/v1":
            return _resources()
        if path == "/apis":
            return {"groups": [_group(f"group-{number}.io", "v1") for number in range(12)]}
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return _resources("widgets")

    with patch.object(client, "_request_json", AsyncMock(side_effect=request)):
        metas = await client.discover_resources()

    assert len(metas) == 12
    assert 1 < peak <= 3
    assert active == 0


async def test_a_stalled_version_times_out_without_losing_healthy_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_DISCOVERY_TIMEOUT_SECONDS", 0.01, raising=False)
    client = KubeClient()
    cancelled = asyncio.Event()
    responses = {
        "/api/v1": _resources("pods"),
        "/apis": {"groups": [_group("example.io", "v1", "v1beta1")]},
        "/apis/example.io/v1beta1": _resources("gadgets"),
    }

    async def request(path: str) -> dict[str, Any]:
        if path != "/apis/example.io/v1":
            return responses[path]
        try:
            await asyncio.Future[None]()
        finally:
            cancelled.set()
        return _resources()

    with patch.object(client, "_request_json", AsyncMock(side_effect=request)):
        metas = await asyncio.wait_for(client.discover_resources(), timeout=1)

    assert [meta.plural for meta in metas] == ["pods", "gadgets"]
    assert cancelled.is_set()


@pytest.mark.parametrize("broken_resources", [None, "bad", [None, "bad"]])
async def test_an_unusable_version_document_does_not_break_discovery(broken_resources: Any) -> None:
    client = KubeClient()
    responses = {
        "/api/v1": _resources("pods"),
        "/apis": {"groups": [_group("example.io", "v1", "v1beta1")]},
        "/apis/example.io/v1": {"resources": broken_resources},
        "/apis/example.io/v1beta1": _resources("gadgets"),
    }

    with patch.object(client, "_request_json", AsyncMock(side_effect=responses.__getitem__)):
        metas = await client.discover_resources()

    assert [meta.plural for meta in metas] == ["pods", "gadgets"]


@pytest.mark.parametrize(
    ("short_names", "expected"),
    [(7, ()), ([7], ()), ("widget", ()), ([None, "widget", 7], ("widget",))],
)
async def test_malformed_short_names_do_not_break_discovery_or_aliases(
    short_names: Any, expected: tuple[str, ...]
) -> None:
    client = KubeClient()
    version_resources = _resources("widgets", "gadgets")
    version_resources["resources"][0]["shortNames"] = short_names
    responses = {
        "/api/v1": _resources("pods"),
        "/apis": {"groups": [_group("example.io", "v1")]},
        "/apis/example.io/v1": version_resources,
    }

    with patch.object(client, "_request_json", AsyncMock(side_effect=responses.__getitem__)):
        metas = await client.discover_resources()

    aliases = build_alias_map(metas)
    assert [meta.plural for meta in metas] == ["pods", "widgets", "gadgets"]
    assert aliases["widgets"].shortnames == expected
    assert aliases["gadgets"].plural == "gadgets"


@pytest.mark.parametrize("stalled_path", ["/api/v1", "/apis"])
async def test_bootstrap_discovery_requests_have_a_deadline(
    stalled_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_DISCOVERY_TIMEOUT_SECONDS", 0.01)
    client = KubeClient()
    cancelled = asyncio.Event()

    async def request(path: str) -> dict[str, Any]:
        if path == stalled_path:
            try:
                await asyncio.Future[None]()
            finally:
                cancelled.set()
        return _resources("pods") if path == "/api/v1" else {"groups": []}

    with (
        patch.object(client, "_request_json", AsyncMock(side_effect=request)) as fetch,
        pytest.raises(KubeClientError, match="discovery timed out"),
    ):
        await asyncio.wait_for(client.discover_resources(), timeout=1)

    assert cancelled.is_set()
    assert fetch.await_args is not None
    assert fetch.await_args.args == (stalled_path,)
