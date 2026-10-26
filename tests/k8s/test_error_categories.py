"""Each raise site on the shared request path names its failure category (#409).

The evidence classifier maps a `KubeClientError` by its `category` and never
parses messages, so every site that knows what went wrong has to say so.
"""

from __future__ import annotations

import ssl
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientConnectionError, InvalidURL

from korvid.k8s.client import KubeClient
from korvid.k8s.deployment_outcomes import iter_raw_object_pages, parse_list_page
from korvid.k8s.discovery import PODS_META
from korvid.k8s.errors import (
    KubeClientError,
    KubeErrorCategory,
    KubeMalformedError,
    KubeTimeoutError,
    KubeTooLargeError,
    KubeTransportError,
)


@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (KubeClientError, None),
        (KubeTimeoutError, "timeout"),
        (KubeTransportError, "transport"),
        (KubeMalformedError, "malformed"),
        (KubeTooLargeError, "too_large"),
    ],
)
def test_each_error_class_names_one_category(
    error_type: type[KubeClientError], category: KubeErrorCategory | None
) -> None:
    error = error_type("Kubernetes API request failed")

    assert isinstance(error, KubeClientError)
    assert error.category == category
    assert str(error) == "Kubernetes API request failed"


def _client(*, failure: BaseException | None = None, body: bytes = b"{}") -> KubeClient:
    response = AsyncMock()
    response.status = 200
    response.reason = "OK"
    response.read.return_value = body
    api = AsyncMock()
    api.call_api.return_value = response
    api.call_api.side_effect = failure
    client = KubeClient()
    client._api = api
    return client


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        pytest.param(TimeoutError(), "timeout", id="timeout"),
        pytest.param(ssl.SSLError("certificate"), "transport", id="tls"),
        pytest.param(ClientConnectionError("refused"), "transport", id="aiohttp"),
        pytest.param(ConnectionResetError("reset"), "transport", id="socket"),
        pytest.param(InvalidURL("https://cluster.invalid"), "transport", id="invalid-url"),
        pytest.param(ValueError("header"), None, id="client-configuration"),
    ],
)
async def test_request_failures_name_their_category(
    failure: BaseException, category: KubeErrorCategory | None
) -> None:
    with pytest.raises(KubeClientError, match="Kubernetes API") as excinfo:
        await _client(failure=failure)._request_json("/api/v1")

    assert excinfo.value.category == category


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"token":"secret"', id="invalid-json"),
        pytest.param(b'["not", "an", "object"]', id="not-an-object"),
    ],
)
async def test_an_unusable_response_is_malformed(body: bytes) -> None:
    with pytest.raises(KubeMalformedError, match="malformed"):
        await _client(body=body)._request_json("/api/v1")


async def test_a_discovery_timeout_is_a_timeout() -> None:
    client = KubeClient()

    with (
        patch.object(client, "_request_json", AsyncMock(side_effect=TimeoutError())),
        pytest.raises(KubeTimeoutError, match="discovery timed out"),
    ):
        await client.discover_resources()


@pytest.mark.parametrize(
    ("documents", "message"),
    [
        pytest.param(
            {
                "/api": {"versions": ["v1"]},
                "/api/v1": {"resources": []},
                "/apis": {"groups": "invalid"},
            },
            "invalid group list",
            id="group-list",
        ),
        pytest.param(
            {
                "/api": {"versions": ["v1"]},
                "/api/v1": {"resources": "invalid"},
                "/apis": {"groups": []},
            },
            "invalid resource list",
            id="resource-list",
        ),
    ],
)
async def test_an_invalid_discovery_document_is_malformed(
    documents: dict[str, dict[str, Any]], message: str
) -> None:
    client = KubeClient()

    async def answer(path: str) -> dict[str, Any]:
        return documents[path]

    with (
        patch.object(client, "_request_json", AsyncMock(side_effect=answer)),
        pytest.raises(KubeMalformedError, match=message),
    ):
        await client.discover_resources()


def test_an_invalid_list_page_is_malformed() -> None:
    with pytest.raises(KubeMalformedError, match="malformed response"):
        parse_list_page({"items": "invalid"})


async def test_a_repeated_continuation_is_malformed() -> None:
    request = AsyncMock(return_value={"items": [], "metadata": {"continue": "same"}})
    pages = iter_raw_object_pages(
        PODS_META,
        "default",
        page_size=5,
        list_path=lambda meta, namespace: "/api/v1/namespaces/default/pods",
        request_json=request,
        observe=lambda *args, **kwargs: None,
        observe_error=lambda path, error: None,
    )

    with pytest.raises(KubeMalformedError, match="continuation did not advance"):
        await anext(pages)

    assert [call.kwargs["query_params"] for call in request.await_args_list] == [
        [("limit", "5")],
        [("limit", "5"), ("continue", "same")],
    ]
