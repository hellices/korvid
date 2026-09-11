from collections.abc import AsyncGenerator
from contextlib import aclosing
from types import AsyncGeneratorType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.errors import ApiStatusError, KubeClientError
from korvid.k8s.models import GenericSummary
from korvid.k8s.telemetry import ReadTelemetryEvent


def _pod(name: str) -> dict[str, Any]:
    return {"metadata": {"name": name, "namespace": "prod"}, "spec": {}, "status": {}}


async def test_iter_objects_follows_bounded_pages_and_records_telemetry() -> None:
    telemetry: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=telemetry.append)
    request = AsyncMock(
        side_effect=[
            {"metadata": {"continue": "next-page"}, "items": [_pod("first")]},
            {"metadata": {}, "items": [_pod("second")]},
        ]
    )

    with patch.object(client, "_api", MagicMock()), patch.object(client, "_request_json", request):
        async with aclosing(client.iter_objects(PODS_META, "prod")) as summaries:
            result = [summary async for summary in summaries]

    assert [summary.name for summary in result] == ["first", "second"]
    assert request.await_args_list == [
        call("/api/v1/namespaces/prod/pods", query_params=[("limit", "100")]),
        call(
            "/api/v1/namespaces/prod/pods",
            query_params=[("limit", "100"), ("continue", "next-page")],
        ),
    ]
    assert [event.object_count for event in telemetry] == [1, 1]
    assert all(event.operation == "list" and event.decoded_bytes > 0 for event in telemetry)


async def test_iter_objects_projects_only_consumed_rows_and_does_not_prefetch() -> None:
    client = KubeClient()
    request = AsyncMock(
        return_value={
            "metadata": {"continue": "unneeded"},
            "items": [_pod(f"pod-{number}") for number in range(100)],
        }
    )

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request),
        patch.object(client, "_object_summary", wraps=client._object_summary) as project,
    ):
        iterator: AsyncGenerator[GenericSummary, None] = client.iter_objects(PODS_META, None)
        async with aclosing(iterator):
            summary = await anext(iterator)

    assert summary.name == "pod-0"
    assert request.await_count == 1
    assert project.call_count == 1
    assert isinstance(iterator, AsyncGeneratorType)
    assert iterator.ag_frame is None
    assert request.await_args == call("/api/v1/pods", query_params=[("limit", "100")])


@pytest.mark.parametrize(
    ("meta", "namespace", "path"),
    [
        (PODS_META, None, "/api/v1/pods"),
        (PODS_META, "prod", "/api/v1/namespaces/prod/pods"),
        (ResourceMeta("Node", "nodes", "", "v1", False), None, "/api/v1/nodes"),
        (
            ResourceMeta("Deployment", "deployments", "apps", "v1", True),
            "prod",
            "/apis/apps/v1/namespaces/prod/deployments",
        ),
    ],
)
async def test_iter_objects_preserves_resource_scope(
    meta: ResourceMeta, namespace: str | None, path: str
) -> None:
    client = KubeClient()
    request = AsyncMock(return_value={"items": []})

    with patch.object(client, "_api", MagicMock()), patch.object(client, "_request_json", request):
        result = [summary async for summary in client.iter_objects(meta, namespace)]

    assert result == []
    assert request.await_args == call(path, query_params=[("limit", "100")])


async def test_iter_objects_propagates_and_records_api_errors() -> None:
    telemetry: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=telemetry.append)
    request = AsyncMock(side_effect=ApiStatusError(403, "Forbidden"))

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request),
        pytest.raises(ApiStatusError, match="Forbidden"),
    ):
        await anext(client.iter_objects(PODS_META, None))

    assert len(telemetry) == 1
    assert telemetry[0].status == 403


async def test_iter_objects_propagates_and_records_normalized_errors() -> None:
    telemetry: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=telemetry.append)
    request = AsyncMock(side_effect=KubeClientError("malformed response"))

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request),
        pytest.raises(KubeClientError, match="malformed"),
    ):
        await anext(client.iter_objects(PODS_META, None))

    assert len(telemetry) == 1
    assert telemetry[0].operation == "error"
    assert telemetry[0].status is None


async def test_iter_objects_rejects_non_advancing_continuation() -> None:
    client = KubeClient()
    request = AsyncMock(return_value={"items": [], "metadata": {"continue": "same"}})

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request),
        pytest.raises(KubeClientError, match="continuation"),
    ):
        await anext(client.iter_objects(PODS_META, None))

    assert request.await_count == 2


@pytest.mark.parametrize(
    "tokens",
    [("first", "second", "first"), ("first", "second", "third", "second")],
)
@pytest.mark.parametrize("with_items", [False, True])
async def test_iter_objects_rejects_cyclic_continuation(
    tokens: tuple[str, ...], with_items: bool
) -> None:
    telemetry: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=telemetry.append)
    pages = [
        {
            "items": [_pod(f"pod-{page_number}")] if with_items else [],
            "metadata": {"continue": token},
        }
        for page_number, token in enumerate(tokens)
    ]
    request = AsyncMock(
        side_effect=[*pages, AssertionError("Repeated continuation token was followed")]
    )

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request),
        pytest.raises(KubeClientError, match="continuation"),
    ):
        async with aclosing(client.iter_objects(PODS_META, None)) as summaries:
            _ = [summary async for summary in summaries]

    assert request.await_count == len(tokens)
    assert len(telemetry) == len(tokens) + 1
    assert telemetry[-1].operation == "error"
    assert telemetry[-1].status is None


@pytest.mark.parametrize(
    "page",
    [
        pytest.param({}, id="missing-items"),
        pytest.param({"items": None}, id="null-items"),
        pytest.param({"items": {}}, id="empty-mapping-items"),
        pytest.param({"items": {"name": "pod"}}, id="mapping-items"),
        pytest.param({"items": ""}, id="empty-string-items"),
        pytest.param({"items": "pod"}, id="string-items"),
        pytest.param({"items": 1}, id="number-items"),
        pytest.param({"items": False}, id="boolean-items"),
        pytest.param({"items": [None]}, id="null-item"),
        pytest.param({"items": ["pod"]}, id="string-item"),
        pytest.param({"items": [1]}, id="number-item"),
        pytest.param({"items": [[]]}, id="list-item"),
        pytest.param({"items": [_pod("valid"), None]}, id="mixed-items"),
        pytest.param({"items": [], "metadata": None}, id="null-metadata"),
        pytest.param({"items": [], "metadata": []}, id="list-metadata"),
        pytest.param({"items": [], "metadata": "token"}, id="string-metadata"),
        pytest.param({"items": [], "metadata": {"continue": None}}, id="null-token"),
        pytest.param({"items": [], "metadata": {"continue": 0}}, id="number-token"),
        pytest.param({"items": [], "metadata": {"continue": False}}, id="boolean-token"),
        pytest.param({"items": [], "metadata": {"continue": []}}, id="list-token"),
        pytest.param({"items": [], "metadata": {"continue": {}}}, id="mapping-token"),
    ],
)
async def test_iter_objects_rejects_malformed_page(page: dict[str, Any]) -> None:
    telemetry: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=telemetry.append)
    request = AsyncMock(return_value=page)

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request),
        pytest.raises(KubeClientError, match="malformed"),
    ):
        async with aclosing(client.iter_objects(PODS_META, None)) as summaries:
            _ = [summary async for summary in summaries]

    assert request.await_count == 1
    assert len(telemetry) == 1
    assert telemetry[0].operation == "error"
    assert telemetry[0].status is None
