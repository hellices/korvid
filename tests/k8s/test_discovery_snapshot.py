"""The raw discovery snapshot of one session (#409, slice S2)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.k8s import discovery_snapshot
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
    fetch_discovery,
    project_resources,
)
from korvid.k8s.errors import ApiStatusError, KubeMalformedError, KubeTimeoutError

APPS_GROUP = {
    "name": "apps",
    "preferredVersion": {"groupVersion": "apps/v1", "version": "v1"},
    "versions": [{"groupVersion": "apps/v1", "version": "v1"}],
}
APPS_V1BETA1 = {"groupVersion": "apps/v1beta1", "version": "v1beta1"}
DOCUMENTS: dict[str, dict[str, Any]] = {
    "/api": {"versions": ["v1"]},
    "/api/v1": {
        "resources": [
            {"name": "pods", "kind": "Pod", "namespaced": True, "verbs": ["get", "list", "watch"]},
            {"name": "pods/log", "kind": "Pod", "namespaced": True, "verbs": ["get"]},
            {"name": "bindings", "kind": "Binding", "namespaced": True, "verbs": ["create"]},
        ]
    },
    "/apis": {"groups": [APPS_GROUP]},
    "/apis/apps/v1": {
        "resources": [
            {"name": "deployments", "kind": "Deployment", "namespaced": True, "verbs": ["list"]}
        ]
    },
}


def _answering(
    documents: dict[str, dict[str, Any]], failures: dict[str, Exception] | None = None
) -> Callable[[str], Awaitable[dict[str, Any]]]:
    async def request(path: str) -> dict[str, Any]:
        if failures is not None and path in failures:
            raise failures[path]
        return documents[path]

    return request


async def _fetch(
    documents: dict[str, dict[str, Any]] = DOCUMENTS, failures: dict[str, Exception] | None = None
) -> RawDiscovery:
    request = _answering(documents, failures)
    return await fetch_discovery(request, generation=3, timeout=1.0, concurrency=4)


async def test_both_roots_and_every_advertised_version_are_kept() -> None:
    apps = {**APPS_GROUP, "versions": [*APPS_GROUP["versions"], APPS_V1BETA1]}
    raw = await _fetch(
        {**DOCUMENTS, "/apis": {"groups": [apps]}, "/apis/apps/v1beta1": {"resources": []}}
    )

    assert raw.generation == 3
    assert raw.core_root == RootOutcome("/api", (("", "v1"),))
    assert raw.group_root == RootOutcome("/apis", (("apps", "v1"), ("apps", "v1beta1")))
    assert [(outcome.group, outcome.version) for outcome in raw.group_versions] == [
        ("", "v1"),
        ("apps", "v1"),
        ("apps", "v1beta1"),
    ]


async def test_every_served_resource_keeps_its_verbs() -> None:
    core = (await _fetch()).group_versions[0]

    assert core.resources == (
        ServedResource("pods", frozenset({"get", "list", "watch"})),
        ServedResource("pods/log", frozenset({"get"})),
        ServedResource("bindings", frozenset({"create"})),
    )
    assert core.complete
    assert [meta.plural for meta in core.metas] == ["pods"]


@pytest.mark.parametrize("failed", ["/api", "/apis"])
async def test_a_failed_root_is_kept_without_affecting_the_other(failed: str) -> None:
    error = ApiStatusError(403, "Forbidden")
    raw = await _fetch(failures={failed: error})
    roots = {"/api": raw.core_root, "/apis": raw.group_root}

    assert roots[failed] == RootOutcome(failed, error=error)
    assert all(root.error is None for path, root in roots.items() if path != failed)


async def test_core_v1_is_read_even_when_api_fails() -> None:
    raw = await _fetch(failures={"/api": ApiStatusError(403, "Forbidden")})

    assert raw.group_versions[0].error is None
    assert [meta.plural for meta in project_resources(raw)] == ["pods", "deployments"]


async def test_a_failed_group_version_is_kept_and_skipped() -> None:
    error = ApiStatusError(503, "Service Unavailable")
    raw = await _fetch(failures={"/apis/apps/v1": error})

    assert raw.group_versions[1] == GroupVersionOutcome("apps", "v1", error=error)
    assert [meta.plural for meta in project_resources(raw)] == ["pods"]


@pytest.mark.parametrize("path", ["/api/v1", "/apis"])
async def test_a_failed_core_version_or_group_root_still_fails_projection(path: str) -> None:
    raw = await _fetch(failures={path: ApiStatusError(503, "Service Unavailable")})

    with pytest.raises(ApiStatusError, match="503"):
        project_resources(raw)


async def test_a_stalled_request_is_kept_as_a_timeout() -> None:
    async def request(path: str) -> dict[str, Any]:
        if path == "/apis/apps/v1":
            await asyncio.Future[None]()
        return DOCUMENTS[path]

    raw = await fetch_discovery(request, generation=1, timeout=0.01, concurrency=4)

    assert isinstance(raw.group_versions[1].error, KubeTimeoutError)


async def test_the_roots_and_core_v1_are_requested_together() -> None:
    roots = {"/api", "/api/v1", "/apis"}
    started: set[str] = set()
    all_started = asyncio.Event()

    async def request(path: str) -> dict[str, Any]:
        started.add(path)
        if roots <= started:
            all_started.set()
        if path in roots:
            await all_started.wait()
        return DOCUMENTS[path]

    fetch = fetch_discovery(request, generation=1, timeout=5.0, concurrency=4)
    raw = await asyncio.wait_for(fetch, timeout=2)

    assert raw.group_root.error is None


@pytest.mark.parametrize(
    "groups",
    [
        pytest.param([None, APPS_GROUP], id="not-a-group"),
        pytest.param([{"name": "../bad", "versions": []}, APPS_GROUP], id="invalid-name"),
        pytest.param([{"name": "apps..example", "versions": []}, APPS_GROUP], id="empty-label"),
        pytest.param([{"name": "apps.-x", "versions": []}, APPS_GROUP], id="hyphen-led-label"),
        pytest.param([{"name": "a" * 254, "versions": []}, APPS_GROUP], id="name-too-long"),
        pytest.param(
            [{**APPS_GROUP, "versions": [*APPS_GROUP["versions"], {"version": 42}]}],
            id="version-entry",
        ),
        pytest.param([{**APPS_GROUP, "versions": None}], id="no-version-list"),
    ],
)
async def test_an_unreadable_group_root_entry_makes_the_root_incomplete(groups: list[Any]) -> None:
    raw = await _fetch({**DOCUMENTS, "/apis": {"groups": groups}})

    assert not raw.group_root.complete
    assert raw.group_root.versions("apps") == ("v1",)


async def test_an_unusable_preferred_version_only_loses_its_place() -> None:
    group = {**APPS_GROUP, "preferredVersion": "broken"}
    raw = await _fetch({**DOCUMENTS, "/apis": {"groups": [group]}})

    assert raw.group_root == RootOutcome("/apis", (("apps", "v1"),))


async def test_the_preferred_version_is_read_first_wherever_it_is_listed() -> None:
    group = {**APPS_GROUP, "versions": [APPS_V1BETA1, *APPS_GROUP["versions"]]}
    raw = await _fetch(
        {**DOCUMENTS, "/apis": {"groups": [group]}, "/apis/apps/v1beta1": {"resources": []}}
    )

    assert raw.group_root == RootOutcome("/apis", (("apps", "v1"), ("apps", "v1beta1")))


async def test_a_preferred_version_a_full_version_list_omits_is_never_read() -> None:
    group = {**APPS_GROUP, "preferredVersion": {"groupVersion": "apps/v2", "version": "v2"}}
    raw = await _fetch(
        {**DOCUMENTS, "/apis": {"groups": [group]}, "/apis/apps/v2": {"resources": []}}
    )

    assert raw.group_root == RootOutcome("/apis", (("apps", "v1"),))
    assert [(outcome.group, outcome.version) for outcome in raw.group_versions] == [
        ("", "v1"),
        ("apps", "v1"),
    ]


@pytest.mark.parametrize("versions", [["v1", 7], ["v1", "../v2"]])
async def test_an_unreadable_core_version_makes_the_core_root_incomplete(
    versions: list[Any],
) -> None:
    raw = await _fetch({**DOCUMENTS, "/api": {"versions": versions}})

    assert raw.core_root == RootOutcome("/api", (("", "v1"),), complete=False)


async def test_a_core_root_without_a_version_list_is_malformed() -> None:
    raw = await _fetch({**DOCUMENTS, "/api": {"groups": []}})

    assert isinstance(raw.core_root.error, KubeMalformedError)
    assert [meta.plural for meta in project_resources(raw)] == ["pods", "deployments"]


async def test_a_group_root_without_a_group_list_is_incomplete() -> None:
    raw = await _fetch({**DOCUMENTS, "/apis": {}})

    assert raw.group_root == RootOutcome("/apis", complete=False)
    assert [meta.plural for meta in project_resources(raw)] == ["pods"]


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(None, id="not-a-resource"),
        pytest.param({"kind": "Pod", "namespaced": True, "verbs": ["list"]}, id="no-name"),
        pytest.param({"name": "pods", "namespaced": True, "verbs": ["list"]}, id="no-kind"),
        pytest.param({"name": "pods", "kind": "Pod", "verbs": ["list"]}, id="no-scope"),
        pytest.param({"name": "pods", "kind": "Pod", "namespaced": True}, id="no-verbs"),
        pytest.param(
            {"name": "pods", "kind": "Pod", "namespaced": True, "verbs": "list"}, id="verbs-text"
        ),
        pytest.param(
            {"name": "pods", "kind": "Pod", "namespaced": True, "verbs": ["list", 7]},
            id="verb-not-text",
        ),
    ],
)
async def test_an_unreadable_resource_entry_makes_its_version_incomplete(entry: Any) -> None:
    resources = [entry, *DOCUMENTS["/apis/apps/v1"]["resources"]]
    raw = await _fetch({**DOCUMENTS, "/apis/apps/v1": {"resources": resources}})

    assert not raw.group_versions[1].complete
    assert "deployments" in [meta.plural for meta in raw.group_versions[1].metas]


@pytest.mark.parametrize("scope", ["false", 0, 1])
async def test_a_resource_whose_scope_is_not_a_boolean_is_unreadable(scope: object) -> None:
    entry = {"name": "widgets", "kind": "Widget", "namespaced": scope, "verbs": ["list"]}
    resources = [entry, *DOCUMENTS["/apis/apps/v1"]["resources"]]
    raw = await _fetch({**DOCUMENTS, "/apis/apps/v1": {"resources": resources}})
    apps = raw.group_versions[1]

    assert not apps.complete
    assert [resource.name for resource in apps.resources] == ["deployments"]
    assert [meta.plural for meta in apps.metas] == ["deployments"]


@pytest.mark.parametrize(
    ("path", "index", "projected"),
    [
        pytest.param("/api/v1", 0, ["deployments"], id="core"),
        pytest.param("/apis/apps/v1", 1, ["pods"], id="named"),
    ],
)
async def test_a_version_without_a_resource_list_is_incomplete(
    path: str, index: int, projected: list[str]
) -> None:
    raw = await _fetch({**DOCUMENTS, path: {}})
    outcome = raw.group_versions[index]

    assert (outcome.resources, outcome.complete, outcome.error) == ((), False, None)
    assert [meta.plural for meta in project_resources(raw)] == projected


async def test_a_snapshot_expires_after_five_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(discovery_snapshot, "monotonic", lambda: clock[0])
    raw = await _fetch()

    clock[0] += discovery_snapshot.DISCOVERY_TTL_SECONDS
    assert not raw.expired()
    clock[0] += 1
    assert raw.expired()


def test_a_snapshot_starts_with_core_v1() -> None:
    now = datetime(2026, 10, 26, tzinfo=UTC)

    with pytest.raises(ValueError, match="starts with core v1"):
        RawDiscovery(1, now, 0.0, RootOutcome("/api"), RootOutcome("/apis"), ())
