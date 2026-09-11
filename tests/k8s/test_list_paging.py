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
