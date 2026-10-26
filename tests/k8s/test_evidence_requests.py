"""What evidence reads put on the wire (#409, slice S3)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from korvid.k8s import bounded_transport
from korvid.k8s.bounded_transport import ERROR_BODY_LIMIT
from korvid.k8s.client import KubeClient
from korvid.k8s.errors import ApiStatusError, KubeTooLargeError
from korvid.k8s.evidence_requests import (
    METADATA_LIST,
    METADATA_OBJECT,
    EvidencePage,
    EvidenceReader,
    EvidenceTarget,
)
from tests.k8s.fake_http import Response, fake_api, sent

DEPLOYMENTS = EvidenceTarget("apps", "v1", "deployments", "team-a")
CONFIGMAPS = EvidenceTarget("", "v1", "configmaps", "team-a", metadata_only=True)


@pytest.fixture(autouse=True)
def _shared_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bounded_transport, "bounded_session", lambda shared: shared)


def _client(response: Response) -> tuple[KubeClient, Any]:
    client = KubeClient()
    api = fake_api(response)
    client._api = api
    return client, api


def _url(api: Any) -> tuple[str, list[tuple[str, str]]]:
    url = urlsplit(str(sent(api)["url"]))
    return url.path, parse_qsl(url.query)


def _accept(api: Any) -> str:
    headers = sent(api)["headers"]
    assert isinstance(headers, dict)
    return str(headers["Accept"])


def test_the_evidence_reader_can_only_read() -> None:
    assert EvidenceReader.__abstractmethods__ == frozenset(
        {"list_evidence_page", "get_evidence_object"}
    )
    assert isinstance(KubeClient(), EvidenceReader)


async def test_a_list_page_reads_the_collection_within_its_budgets() -> None:
    document = {"items": [{"metadata": {"name": "web"}}], "metadata": {"continue": "next"}}
    client, api = _client(Response(json.dumps(document).encode()))

    page = await client.list_evidence_page(
        DEPLOYMENTS, limit=50, continuation="token?&", max_bytes=4096
    )

    assert page == EvidencePage(({"metadata": {"name": "web"}},), "next")
    assert _url(api) == (
        "/apis/apps/v1/namespaces/team-a/deployments",
        [("limit", "50"), ("continue", "token?&")],
    )
    assert _accept(api) == "application/json"


async def test_a_first_page_sends_no_continue_token() -> None:
    client, api = _client(Response(b'{"items": []}'))

    page = await client.list_evidence_page(DEPLOYMENTS, limit=1, continuation="", max_bytes=64)

    assert page == EvidencePage(())
    assert _url(api)[1] == [("limit", "1")]


async def test_an_object_read_names_its_object() -> None:
    client, api = _client(Response(b'{"metadata": {"name": "web"}}'))

    item = await client.get_evidence_object(DEPLOYMENTS, "web", max_bytes=64)

    assert item == {"metadata": {"name": "web"}}
    assert _url(api) == ("/apis/apps/v1/namespaces/team-a/deployments/web", [])


async def test_a_metadata_list_asks_for_partial_object_metadata() -> None:
    client, api = _client(Response(b'{"items": []}'))

    await client.list_evidence_page(CONFIGMAPS, limit=10, continuation="", max_bytes=64)

    assert _accept(api) == METADATA_LIST


async def test_a_metadata_object_read_asks_for_partial_object_metadata() -> None:
    client, api = _client(Response(b'{"metadata": {"name": "settings"}}'))

    await client.get_evidence_object(CONFIGMAPS, "settings", max_bytes=64)

    assert _accept(api) == METADATA_OBJECT
    assert _url(api)[0] == "/api/v1/namespaces/team-a/configmaps/settings"


async def test_an_object_read_needs_a_name() -> None:
    client, api = _client(Response(b"{}"))

    with pytest.raises(ValueError, match="names its object"):
        await client.get_evidence_object(DEPLOYMENTS, "", max_bytes=64)

    assert api.rest_client.pool_manager.request.await_count == 0


@pytest.mark.parametrize("limit", [0, -1, True])
async def test_a_page_size_is_a_positive_integer(limit: Any) -> None:
    client, api = _client(Response(b'{"items": []}'))

    with pytest.raises(ValueError, match="page size"):
        await client.list_evidence_page(DEPLOYMENTS, limit=limit, continuation="", max_bytes=64)

    assert api.rest_client.pool_manager.request.await_count == 0


async def test_a_page_with_more_items_than_requested_is_over_budget() -> None:
    client, _api = _client(Response(b'{"items": [{}, {}, {}]}'))

    with pytest.raises(KubeTooLargeError, match="object cap"):
        await client.list_evidence_page(DEPLOYMENTS, limit=2, continuation="", max_bytes=64)


async def test_a_list_failure_keeps_the_status_for_the_classifier() -> None:
    status = {"kind": "Status", "reason": "Expired"}
    client, _api = _client(Response(json.dumps(status).encode(), 410))

    with pytest.raises(ApiStatusError, match="API 410") as failure:
        await client.list_evidence_page(
            DEPLOYMENTS, limit=10, continuation="old", max_bytes=ERROR_BODY_LIMIT
        )

    assert json.loads(failure.value.body) == status


async def test_an_object_failure_keeps_the_status_for_the_classifier() -> None:
    status = {"kind": "Status", "reason": "NotFound", "details": {"name": "web"}}
    client, _api = _client(Response(json.dumps(status).encode(), 404))

    with pytest.raises(ApiStatusError, match="API 404") as failure:
        await client.get_evidence_object(DEPLOYMENTS, "web", max_bytes=64)

    assert json.loads(failure.value.body) == status
