"""One bounded GET, shared by Pulse and evidence reads (#409, slice S3)."""

from __future__ import annotations

import gzip
import hashlib
import json
from typing import Any

import pytest
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config import ConfigException

from korvid.k8s import bounded_transport
from korvid.k8s.bounded_transport import (
    ERROR_BODY_LIMIT,
    api_path,
    decode_list_page,
    read_bounded_json,
    retry_after,
)
from korvid.k8s.errors import ApiStatusError, KubeClientError, KubeTooLargeError
from tests.k8s.fake_http import Response, Stream, fake_api, sent


@pytest.fixture(autouse=True)
def _shared_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bounded_transport, "bounded_session", lambda shared: shared)


async def _read(response: Response, *, max_bytes: int = 64, **options: Any) -> dict[str, Any]:
    return await read_bounded_json(
        fake_api(response), "/api/v1/pods", [], max_bytes=max_bytes, **options
    )


async def test_a_body_within_its_budget_is_one_json_object() -> None:
    response = Response(b'{"kind": "PodList"}')

    assert await _read(response) == {"kind": "PodList"}
    assert response.closed


class LateEndStream(Stream):
    """A body whose end is only seen when one more byte is asked for, as chunked bodies are."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.reads: list[bytes] = []

    async def read(self, size: int) -> bytes:
        chunk = await super().read(size)
        self.reads.append(chunk)
        return chunk

    def at_eof(self) -> bool:
        return False


async def test_a_body_of_exactly_the_budget_is_accepted_before_its_end_arrives() -> None:
    payload = b'{"kind": "PodList"}'
    response = Response(payload)
    stream = LateEndStream(payload)
    response.content = stream

    assert await _read(response, max_bytes=len(payload)) == {"kind": "PodList"}
    assert stream.reads[-1] == b""


async def test_a_body_one_byte_past_the_budget_is_over_it_before_its_end_arrives() -> None:
    payload = b'{"kind": "PodList"}'
    response = Response(payload)
    response.content = LateEndStream(payload)

    with pytest.raises(KubeTooLargeError, match="byte cap"):
        await _read(response, max_bytes=len(payload) - 1)


@pytest.mark.parametrize("kind", ["repetitive", "incompressible"])
async def test_gzip_that_decodes_to_exactly_the_budget_is_accepted(kind: str) -> None:
    filler = " " * 4000 if kind == "repetitive" else hashlib.shake_256(b"korvid").hexdigest(2000)
    payload = json.dumps({"pad": filler}).encode()
    response = Response(gzip.compress(payload, mtime=0), headers={"Content-Encoding": "gzip"})

    assert await _read(response, max_bytes=len(payload)) == {"pad": filler}


async def test_the_requested_media_type_wins_over_the_client_default() -> None:
    api = fake_api(Response(b"{}"))
    await read_bounded_json(api, "/api/v1/pods", [], max_bytes=64, accept="application/x-test")
    headers = sent(api)["headers"]

    assert isinstance(headers, dict)
    assert headers["Accept"] == "application/x-test"
    assert headers["Accept-Encoding"] == "identity"
    assert headers["User-Agent"] == "korvid-test"


async def test_an_error_keeps_a_bounded_body_and_its_retry_after() -> None:
    status = {"kind": "Status", "reason": "NotFound"}
    response = Response(json.dumps(status).encode(), 404, {"Retry-After": "3"})

    with pytest.raises(ApiStatusError, match="API 404") as failure:
        await _read(response, error_body_limit=ERROR_BODY_LIMIT)

    assert json.loads(failure.value.body) == status
    assert failure.value.retry_after_seconds == 3.0
    assert response.closed


async def test_an_error_body_over_its_limit_is_dropped() -> None:
    response = Response(b"x" * (ERROR_BODY_LIMIT + 1), 403)

    with pytest.raises(ApiStatusError, match="API 403") as failure:
        await _read(response, error_body_limit=ERROR_BODY_LIMIT)

    assert failure.value.body == ""
    assert response.content.position <= ERROR_BODY_LIMIT + 1


async def test_an_error_body_is_not_read_without_a_budget_for_it() -> None:
    response = Response(b"token=top-secret", 401)

    with pytest.raises(ApiStatusError, match="API 401") as failure:
        await _read(response)

    assert failure.value.body == ""
    assert response.content.position == 0


async def test_an_unreadable_error_body_keeps_the_status() -> None:
    response = Response(b"{}", 503)
    response.content.failure = OSError("connection reset: token=secret")

    with pytest.raises(ApiStatusError, match="API 503") as failure:
        await _read(response, error_body_limit=ERROR_BODY_LIMIT)

    assert failure.value.body == ""
    assert "secret" not in str(failure.value)
    assert response.closed


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("2", 2.0),
        ("1.5", 1.5),
        (" 4 ", 4.0),
        ("0", 0.0),
        ("-1", None),
        ("nan", None),
        ("inf", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        (None, None),
    ],
)
def test_retry_after_is_a_finite_number_of_seconds(
    value: str | None, seconds: float | None
) -> None:
    assert retry_after(value) == seconds


@pytest.mark.parametrize(
    ("response", "category"),
    [
        pytest.param(Response(b'{"items": [' + b" " * 100), "too_large", id="byte-cap"),
        pytest.param(
            Response(gzip.compress(b"{}" + b" " * 4096), headers={"Content-Encoding": "gzip"}),
            "too_large",
            id="gzip-expansion",
        ),
        pytest.param(Response(b"not json"), "malformed", id="invalid-json"),
        pytest.param(Response(b"[]"), "malformed", id="not-an-object"),
        pytest.param(Response(b'{"value": NaN}'), "malformed", id="nan"),
        pytest.param(Response(b'{"value": Infinity}'), "malformed", id="infinity"),
        pytest.param(Response(b'{"value": -Infinity}'), "malformed", id="negative-infinity"),
        pytest.param(
            Response(b"{}", headers={"Content-Encoding": "br"}),
            "malformed",
            id="unsupported-encoding",
        ),
        pytest.param(
            Response(b"not gzip", headers={"Content-Encoding": "gzip"}),
            "malformed",
            id="invalid-gzip",
        ),
    ],
)
async def test_a_bad_body_is_categorized_and_closed(response: Response, category: str) -> None:
    with pytest.raises(KubeClientError, match="Kubernetes API") as failure:
        await _read(response)

    assert failure.value.category == category
    assert response.closed
    assert response.content.position <= 64 + 1


@pytest.mark.parametrize(
    ("failure", "category"),
    [(TimeoutError(), "timeout"), (OSError("token=secret"), "transport")],
)
async def test_a_failed_stream_is_categorized_without_its_message(
    failure: BaseException, category: str
) -> None:
    response = Response(b"{}")
    response.content.failure = failure

    with pytest.raises(KubeClientError, match="Kubernetes API") as raised:
        await _read(response)

    assert raised.value.category == category
    assert "secret" not in str(raised.value)
    assert response.closed


async def test_an_api_exception_keeps_only_its_status() -> None:
    api = fake_api(Response(b""))
    failure_with_body = ApiException(status=403, reason="token=secret")
    failure_with_body.body = '{"message": "token=secret"}'
    api.rest_client.pool_manager.request.side_effect = failure_with_body

    with pytest.raises(ApiStatusError, match="API 403") as failure:
        await read_bounded_json(api, "/api/v1/pods", [], max_bytes=64)

    assert "secret" not in str(failure.value)
    assert failure.value.body == ""


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ConfigException("exec: process returned 1: token=secret"), id="exec-failed"),
        pytest.param(RuntimeError("exec: token=secret"), id="exec-unreadable"),
    ],
)
async def test_a_credential_refresh_that_fails_is_a_client_failure(failure: Exception) -> None:
    api = fake_api(Response(b"{}"))
    api.update_params_for_auth.side_effect = failure

    with pytest.raises(KubeClientError, match="credentials") as raised:
        await read_bounded_json(api, "/api/v1/pods", [], max_bytes=64)

    assert raised.value.category is None
    assert "secret" not in str(raised.value)
    assert api.rest_client.pool_manager.request.await_count == 0


@pytest.mark.parametrize("max_bytes", [0, -1, True])
async def test_a_byte_budget_is_a_positive_integer(max_bytes: Any) -> None:
    api = fake_api(Response(b"{}"))

    with pytest.raises(ValueError, match="byte budget"):
        await read_bounded_json(api, "/api/v1/pods", [], max_bytes=max_bytes)

    assert api.rest_client.pool_manager.request.await_count == 0


async def test_a_client_that_never_connected_is_not_categorized() -> None:
    with pytest.raises(KubeClientError, match="not connected") as failure:
        await read_bounded_json(None, "/api/v1/pods", [], max_bytes=64)

    assert failure.value.category is None


def test_a_page_keeps_its_items_and_continue_token() -> None:
    document = {"items": [{"metadata": {"name": "a"}}], "metadata": {"continue": "next"}}

    assert decode_list_page(document, 1) == (({"metadata": {"name": "a"}},), "next")


def test_a_page_with_more_items_than_requested_is_over_budget() -> None:
    with pytest.raises(KubeTooLargeError, match="object cap"):
        decode_list_page({"items": [{}, {}, {}]}, 2)


@pytest.mark.parametrize(
    "document",
    [
        {"items": None},
        {"items": [3]},
        {"items": [], "metadata": {"continue": 4}},
        {"items": [], "metadata": None},
    ],
)
def test_a_malformed_page_is_never_an_empty_one(document: dict[str, Any]) -> None:
    with pytest.raises(KubeClientError, match="invalid") as failure:
        decode_list_page(document, 10)

    assert failure.value.category == "malformed"


@pytest.mark.parametrize(
    ("arguments", "path"),
    [
        (("", "v1", "pods"), "/api/v1/pods"),
        (("", "v1", "pods", "team/a"), "/api/v1/namespaces/team%2Fa/pods"),
        (
            ("apps", "v1", "deployments", "team-a", "web"),
            "/apis/apps/v1/namespaces/team-a/deployments/web",
        ),
        (("", "v1", "nodes", None, "node-1"), "/api/v1/nodes/node-1"),
    ],
)
def test_a_path_names_the_collection_or_one_object(arguments: tuple[Any, ...], path: str) -> None:
    assert api_path(*arguments) == path


def test_a_path_never_traverses() -> None:
    with pytest.raises(ValueError, match="path segment"):
        api_path("", "v1", "pods", None, "..")
