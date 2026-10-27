# Bounded, Metadata-Safe Evidence Reads (#409 S3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give diagnostics two reads, `read_list` and `read_object`. Each is bounded in pages, bytes and time and returns the data with one `Observation`, and neither can hand a diagnostic a ConfigMap's or Secret's data (#487, slice S3 of #409).

**Architecture:**
- **`k8s`:** Pulse's bounded transport moves to a shared `bounded_transport` module, and its errors gain categories. A new `evidence_requests` module uses it for one LIST page or one GET, optionally as `PartialObjectMetadata`. Its `EvidenceReader` ABC has only those two methods, and `KubeClient` implements it.
- **`core`:** a new `evidence_reads` module checks that each source was resolved for its verb, then pages through the read under a deadline and retries one 429. It reduces the metadata, and classifies every outcome with the S1 classifier.

**Tech Stack:** Python 3.11+ standard library (`asyncio`, `zlib`, `json`), `kubernetes_asyncio` and its aiohttp transport, pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md`, sections "Evidence reads", "Outcome classification" and "Security and privacy". Where this plan departs from it, the section below says why.
- **Layers:**
  - `k8s` imports no other korvid layer;
  - `core` may import `k8s`;
  - nothing new imports Textual or an optional extra.
- **Limits, from the design:**
  - pages of 100 (`LIST_PAGE_SIZE`), at most 2 pages, a 5-second deadline and 256 KiB a response by default;
  - hard maxima of 10 pages, 15 seconds and 2 MiB;
  - an error body is kept to 16 KiB.
- **Completeness:** a LIST is `AVAILABLE` only when the server returned an empty continue token.
- **Retries:** none, except one for 429, honouring `Retry-After` only within the deadline.
- **Payloads:**
  - the reader refuses the `object` projection for `configmaps` and `secrets`;
  - every read drops `managedFields` and the `kubectl.kubernetes.io/last-applied-configuration` annotation;
  - ConfigMap and Secret metadata keeps only name, namespace, UID, resource version, creation and deletion timestamps, labels and owner references.
- **`client.py` size:** `scripts/check_source_size.py` caps `src/korvid/k8s/client.py` at 1,944 lines, and it has 1,887. Do not raise the cap.
- **Code rules:**
  - no `assert` in `src/`;
  - every `pytest.raises` takes `match=`;
  - maximum complexity 10;
  - Google-style docstrings.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **Pulse's transport becomes the shared `k8s/bounded_transport.py`.** Pulse and evidence reads stream through one byte budget, on the wire and after decompression, and send each request once. Its messages now say "Kubernetes API" rather than "Pulse".
- **`PulseLimitError` is gone.** `KubeTooLargeError` is the one over-budget error, so the Pulse collector reports `capped` for it.
  - Pulse's request-attempt cap is now a `KubeTransportError`, as #487 records, so Pulse shows `failed` rather than `capped` when a redirect or a retry would have sent a second request.
  - Pulse still never reads an error body. Evidence reads keep up to 16 KiB of one, for the classifier.
- **The reader ABC lives in `k8s/evidence_requests.py`, beside `PulseReader`,** because `KubeClient` implements it and `k8s` cannot import `core`. It speaks in `EvidenceTarget`s, and `core` turns a resolved `ApiSource` into one.
- **A metadata read keeps only each item's `metadata`,** whatever else the server sent. A server that ignored the requested media type therefore still hands a diagnostic nothing else.
- **The payload rules apply to `configmaps` and `secrets` in any group.** Only the core group serves them today, and a name match fails safe.
- **Evidence reads refuse subresources, and node or provider scopes.** No consumer needs them yet, and a node-scoped API read would need a field selector that `ApiSource` cannot express.
- **A repeated continue token is `MALFORMED`.** That is the design's "cyclic continuation". The page that repeated it is not kept.
- **`read_object` checks that the object it got is the one it asked for.** A missing UID, or another name, is `MALFORMED`.
- **A namespaced read accepts only objects from its namespace.** An item from another namespace, or with none, is `MALFORMED`, so it is never attributed to the namespace read.
- **A 429 with no usable `Retry-After` waits one second.** An HTTP-date counts as no `Retry-After`.
- **Embedded metadata is reduced too.** A pod template's metadata, or any other that an object embeds, loses `managedFields` and `last-applied-configuration` as the object's own does. An object nested too deeply to walk is `MALFORMED`.
- **A result read or processed after the deadline is not accepted.** A timeout interrupts a request only where it waits, so the reads check the deadline once a page or an object has been processed. A failure reported after the deadline is the deadline as well: a late `NotFound` must not establish absence. A 429 retry whose wait ends after the deadline is never sent.
- **A budget is exact at its boundary.** The transport asks for one byte past a full budget, on the wire and after decompression, rather than trusting that the end of a chunked body has already arrived. JSON's non-standard `NaN` and `Infinity` are `MALFORMED`.
- **A credential refresh that fails is a client failure.** An exec credential plugin that fails while refreshing a token raises `ConfigException` or `RuntimeError`, with its output in the message. The transport raises an uncategorized `KubeClientError` instead, without that output, so the reads classify it rather than leaking it.
- **Both reads take `max_bytes`, `token` and `clock` besides the design's parameters.** `token` is recorded on the observation. S4 (#488) adds the run generation that makes it useful.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/k8s/bounded_transport.py` | create | one bounded GET: path building, byte budgets, a single attempt, categorized errors, a bounded error body and `Retry-After` |
| `src/korvid/k8s/pulse.py` | modify | Pulse's sources and pages, read through the shared transport |
| `src/korvid/core/pulse_collector.py` | modify | `capped` comes from `KubeTooLargeError` |
| `src/korvid/k8s/evidence_requests.py` | create | `EvidenceTarget`, `EvidencePage`, the `EvidenceReader` ABC, and the LIST page and GET it is built on |
| `src/korvid/k8s/client.py` | modify | `KubeClient` implements `EvidenceReader`; `path_segment` comes from the transport |
| `src/korvid/core/evidence_reads.py` | create | `read_list`, `read_object`, budgets, guards, metadata reduction |
| `src/korvid/core/capability_classifier.py` | modify | `STATUS_BODY_LIMIT` is the transport's `ERROR_BODY_LIMIT` |
| `tests/k8s/fake_http.py` | create | a fake aiohttp response and API client for transport tests |
| `tests/k8s/test_bounded_transport.py` | create | the shared transport |
| `tests/k8s/test_evidence_requests.py` | create | what evidence requests put on the wire |
| `tests/core/test_evidence_reads.py` | create | the reads, their budgets, guards and reductions |
| `tests/k8s/test_pulse.py`, `tests/k8s/test_pulse_transport.py`, `tests/core/test_pulse_collector.py`, `tests/core/test_capability_pulse_mapping.py`, `tests/k8s/test_client.py` | modify | the moved transport, generic messages, `KubeTooLargeError` |
| `tests/test_optional_extras.py` | modify | the new modules import without extras |

---

### Task 1: The shared bounded transport, with categorized errors

**Files:**
- Create: `src/korvid/k8s/bounded_transport.py`
- Modify: `src/korvid/k8s/pulse.py`, `src/korvid/core/pulse_collector.py`, `src/korvid/k8s/client.py` (one import)
- Create: `tests/k8s/fake_http.py`, `tests/k8s/test_bounded_transport.py`
- Modify: `tests/k8s/test_pulse.py`, `tests/k8s/test_pulse_transport.py`, `tests/core/test_pulse_collector.py`, `tests/core/test_capability_pulse_mapping.py`, `tests/k8s/test_client.py`

**Interfaces:**
- Produces in `korvid.k8s.bounded_transport`:
  - `READ_BUFFER_BYTES = 16384`, `ERROR_BODY_LIMIT = 16 * 1024`, `JSON = "application/json"`;
  - `path_segment(value: str) -> str`;
  - `api_path(group: str, version: str, resource: str, namespace: str | None = None, name: str = "") -> str`;
  - `retry_after(value: str | None) -> float | None`;
  - `decode_list_page(document: dict[str, Any], limit: int) -> tuple[tuple[dict[str, Any], ...], str]`;
  - `async read_bounded_json(api: ApiClient | None, path: str, query: list[tuple[str, str]], *, max_bytes: int, accept: str = JSON, error_body_limit: int = 0) -> dict[str, Any]`;
  - `bounded_session(shared: Any) -> Any`, which tests replace.
- Removes `korvid.k8s.pulse.PulseLimitError` and `korvid.k8s.pulse.path_segment`.

- [ ] **Step 1: Write the failing transport tests**

Create `tests/k8s/fake_http.py`:

```python
"""A fake aiohttp response and API client for bounded-transport tests."""

from __future__ import annotations

from unittest.mock import AsyncMock


class Stream:
    """A response body that hands out at most 17 bytes a read."""

    def __init__(self, payload: bytes, failure: BaseException | None = None) -> None:
        self.payload = payload
        self.position = 0
        self.failure = failure

    async def read(self, size: int) -> bytes:
        if self.failure is not None:
            raise self.failure
        chunk = self.payload[self.position : self.position + min(size, 17)]
        self.position += len(chunk)
        return chunk

    def at_eof(self) -> bool:
        return self.position == len(self.payload)


class Response:
    """One HTTP response, which records whether it was closed."""

    def __init__(
        self, payload: bytes, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.content = Stream(payload)
        self.headers = dict(headers or {})
        self.closed = False

    def close(self) -> None:
        self.closed = True


def fake_api(response: Response) -> AsyncMock:
    """An `ApiClient` stand-in whose session answers every request with `response`."""
    api = AsyncMock()
    api.configuration.host = "https://cluster.test"
    api.default_headers = {"User-Agent": "korvid-test", "Accept": "text/plain"}
    api.cookie = None
    api.rest_client.proxy = None
    api.rest_client.proxy_headers = None
    api.rest_client.server_hostname = None
    api.rest_client.pool_manager.request.return_value = response
    api.rest_client.pool_manager.__aenter__.return_value = api.rest_client.pool_manager
    api.rest_client.pool_manager.__aexit__.return_value = False
    return api


def sent(api: AsyncMock) -> dict[str, object]:
    """The keyword arguments of the request `api`'s session sent."""
    kwargs: dict[str, object] = api.rest_client.pool_manager.request.await_args.kwargs
    return kwargs
```

Create `tests/k8s/test_bounded_transport.py`:

```python
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

    def at_eof(self) -> bool:
        return False


async def test_a_body_of_exactly_the_budget_is_accepted_before_its_end_arrives() -> None:
    payload = b'{"kind": "PodList"}'
    response = Response(payload)
    response.content = LateEndStream(payload)

    assert await _read(response, max_bytes=len(payload)) == {"kind": "PodList"}


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
    response.content.failure = OSError("connection reset")

    with pytest.raises(ApiStatusError, match="API 503") as failure:
        await _read(response, error_body_limit=ERROR_BODY_LIMIT)

    assert failure.value.body == ""


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
    api.rest_client.pool_manager.request.side_effect = ApiException(
        status=403, reason="token=secret"
    )

    with pytest.raises(ApiStatusError, match="API 403") as failure:
        await read_bounded_json(api, "/api/v1/pods", [], max_bytes=64)

    assert "secret" not in str(failure.value)


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_bounded_transport.py -q`
Expected: a collection error, `ImportError: cannot import name 'bounded_transport' from 'korvid.k8s'`.

- [ ] **Step 3: Write the shared transport**

Create `src/korvid/k8s/bounded_transport.py`:

```python
"""One bounded GET against the Kubernetes API (#409).

Pulse and diagnostic evidence read through it, so that neither can hold more
than its budget. A response is streamed through a byte budget before any JSON
is parsed, and the budget applies again after decompression. The request is
sent once: no redirect or connection retry can repeat it. Every failure is an
`ApiStatusError` or a `KubeClientError` whose `category` says what went wrong.
"""

from __future__ import annotations

import json
import math
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import quote, urlencode

from kubernetes_asyncio.client import ApiClient, ApiException, rest
from kubernetes_asyncio.config import ConfigException

from korvid.k8s.errors import (
    ApiStatusError,
    KubeClientError,
    KubeMalformedError,
    KubeTimeoutError,
    KubeTooLargeError,
    KubeTransportError,
)

_HTTP = cast(Any, rest).aiohttp
_CLIENT_ERROR = _HTTP.ClientError
READ_BUFFER_BYTES = 16384
#: The most of an error body a reader keeps: enough for a Kubernetes
#: `Status`, which `korvid.core.capability_classifier` reads no more of.
ERROR_BODY_LIMIT = 16 * 1024
JSON = "application/json"


def path_segment(value: str) -> str:
    """Encode one URL segment, rejecting empty and traversal segments."""
    if value in ("", ".", ".."):
        raise ValueError(f"invalid URL path segment: {value!r}")
    return quote(value, safe="")


def api_path(
    group: str, version: str, resource: str, namespace: str | None = None, name: str = ""
) -> str:
    """The path of a resource collection, or of the object `name` in it."""
    prefix = (
        f"/apis/{path_segment(group)}/{path_segment(version)}"
        if group
        else f"/api/{path_segment(version)}"
    )
    scope = f"/namespaces/{path_segment(namespace)}" if namespace is not None else ""
    item = f"/{path_segment(name)}" if name else ""
    return f"{prefix}{scope}/{path_segment(resource)}{item}"


def retry_after(value: str | None) -> float | None:
    """A `Retry-After` header as seconds; `None` unless it is a finite, non-negative number."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


async def read_body(response: Any, max_bytes: int) -> bytes:
    """Read at most `max_bytes` of a body; a longer one is over budget.

    Once the budget is full, one more byte is asked for: the end of a chunked
    body can arrive after its last byte, so only data past the budget proves
    the body is too long.
    """
    body = bytearray()
    while len(body) < max_bytes:
        chunk = await response.content.read(min(READ_BUFFER_BYTES, max_bytes - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
    if await response.content.read(1):
        raise KubeTooLargeError("Kubernetes API response byte cap reached")
    return bytes(body)


def decode_body(body: bytes, encoding: str, max_bytes: int) -> bytes:
    """Undo a content encoding, holding the decoded body to `max_bytes` as well."""
    if encoding in ("", "identity"):
        return body
    if encoding != "gzip":
        raise KubeMalformedError("Kubernetes API returned an unsupported content encoding")
    decompressor = zlib.decompressobj(wbits=31)
    try:
        # One byte past the budget is what shows the body is too long.
        decoded = decompressor.decompress(body, max_length=max_bytes + 1)
    except zlib.error:
        raise KubeMalformedError("Kubernetes API returned invalid compressed data") from None
    if len(decoded) > max_bytes:
        raise KubeTooLargeError("Kubernetes API decoded response byte cap reached")
    if not decompressor.eof or decompressor.unused_data:
        raise KubeMalformedError("Kubernetes API returned incomplete or trailing compressed data")
    return decoded


def bounded_session(shared: Any) -> Any:
    """Borrow the connector and reserve one API send before headers reach the wire."""
    if shared.closed or shared.connector is None:
        raise KubeClientError("Kubernetes API transport is closed")
    sent = False

    async def check_budget(_session: Any, _context: Any, _parameters: Any) -> None:
        if sent:
            raise KubeTransportError("Kubernetes API request attempt cap reached")

    async def reserve_send(session: Any, context: Any, parameters: Any) -> None:
        nonlocal sent
        await check_budget(session, context, parameters)
        sent = True

    budget = _HTTP.TraceConfig()
    budget.on_request_start.append(check_budget)
    budget.on_request_headers_sent.append(reserve_send)
    return _HTTP.ClientSession(
        connector=shared.connector,
        connector_owner=False,
        headers=shared.headers,
        cookie_jar=shared.cookie_jar,
        auth=shared.auth,
        version=shared.version,
        trust_env=shared.trust_env,
        skip_auto_headers=shared.skip_auto_headers,
        trace_configs=[budget, *shared.trace_configs],
        auto_decompress=False,
        read_bufsize=READ_BUFFER_BYTES,
    )


@asynccontextmanager
async def request_once(
    api: ApiClient, path: str, query: list[tuple[str, str]], accept: str
) -> AsyncIterator[Any]:
    """Send one GET with the client's credentials, and close its response afterwards."""
    host = api.configuration.host
    if not host:
        raise KubeClientError("Kubernetes API host is not configured")
    headers = {"Content-Type": JSON, **api.default_headers}
    if api.cookie:
        headers["Cookie"] = api.cookie
    headers.update({"Accept": accept, "Accept-Encoding": "identity"})
    try:
        await api.update_params_for_auth(headers, query, ["BearerToken"])
    except (ConfigException, RuntimeError):
        # An exec credential plugin that fails while refreshing a token
        # raises these, with the plugin's output in the message.
        raise KubeClientError("Kubernetes API credentials could not be refreshed") from None
    transport = api.rest_client
    options: dict[str, Any] = {
        "method": "GET",
        "url": host + path + "?" + urlencode(query),
        "headers": headers,
        "auto_decompress": False,
        "allow_redirects": False,
        "read_bufsize": READ_BUFFER_BYTES,
        "timeout": _HTTP.ClientTimeout(),
    }
    for name in ("proxy", "proxy_headers", "server_hostname"):
        value = getattr(transport, name, None)
        if value:
            options[name] = value
    async with bounded_session(transport.pool_manager) as session:
        response = await session.request(**options)
        try:
            yield response
        finally:
            response.close()


async def read_bounded_json(
    api: ApiClient | None,
    path: str,
    query: list[tuple[str, str]],
    *,
    max_bytes: int,
    accept: str = JSON,
    error_body_limit: int = 0,
) -> dict[str, Any]:
    """GET `path` once and parse its body, read within `max_bytes`, as one JSON object.

    A non-2xx status raises `ApiStatusError` carrying at most
    `error_body_limit` bytes of the body, none by default, and the
    `Retry-After` the server sent.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("a byte budget is a positive integer")
    if api is None:
        raise KubeClientError("Kubernetes API is not connected")
    try:
        async with request_once(api, path, query, accept) as response:
            if not 200 <= response.status < 300:
                raise await _status_error(response, error_body_limit)
            encoding = response.headers.get("Content-Encoding", "").strip().lower()
            body = decode_body(await read_body(response, max_bytes), encoding, max_bytes)
    except ApiException as failure:
        raise ApiStatusError(failure.status or 0, "Kubernetes API request failed") from None
    except TimeoutError:
        raise KubeTimeoutError("Kubernetes API request timed out") from None
    except (OSError, _CLIENT_ERROR):
        raise KubeTransportError("Kubernetes API connection failed") from None
    return _json_object(body)


async def _status_error(response: Any, body_limit: int) -> ApiStatusError:
    body = await _error_body(response, body_limit) if body_limit else ""
    return ApiStatusError(
        response.status,
        "Kubernetes API request failed",
        body,
        retry_after_seconds=retry_after(response.headers.get("Retry-After")),
    )


async def _error_body(response: Any, limit: int) -> str:
    """At most `limit` bytes of an error body; none when it is longer or unreadable."""
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    try:
        raw = decode_body(await read_body(response, limit), encoding, limit)
    except (KubeClientError, OSError, _CLIENT_ERROR):
        return ""
    return raw.decode("utf-8", "replace")


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        document = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise KubeMalformedError("Kubernetes API returned invalid JSON") from None
    if not isinstance(document, dict):
        raise KubeMalformedError("Kubernetes API returned an invalid response document")
    return document


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def decode_list_page(
    document: dict[str, Any], limit: int
) -> tuple[tuple[dict[str, Any], ...], str]:
    """A LIST page's items and continue token. More items than `limit` is over budget."""
    items = document.get("items")
    if not isinstance(items, list):
        raise KubeMalformedError("Kubernetes API returned an invalid resource list")
    if len(items) > limit:
        raise KubeTooLargeError("Kubernetes API response object cap reached")
    if any(not isinstance(item, dict) for item in items):
        raise KubeMalformedError("Kubernetes API returned an invalid list item")
    metadata = document.get("metadata", {})
    if not isinstance(metadata, dict) or not isinstance(metadata.get("continue", ""), str):
        raise KubeMalformedError("Kubernetes API returned invalid pagination metadata")
    return tuple(items), metadata.get("continue", "")
```

- [ ] **Step 4: Read Pulse through it**

Replace the contents of `src/korvid/k8s/pulse.py`:

```python
"""Bounded, read-only snapshot inputs for the attention surface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from kubernetes_asyncio.client import ApiClient

from korvid.k8s.bounded_transport import api_path, decode_list_page, read_bounded_json


@dataclass(frozen=True, slots=True)
class PulseSource:
    """One explicitly registered namespaced LIST input."""

    key: str
    group: str
    version: str
    plural: str
    field_selector: str = ""


@dataclass(frozen=True, slots=True)
class PulsePage:
    """A fully decoded page that passed byte and object bounds."""

    items: tuple[dict[str, Any], ...]
    continuation: str = ""


class PulseReader(ABC):
    """Read a bounded page without extending the general resource reader."""

    @abstractmethod
    async def read_pulse_page(
        self,
        source: PulseSource,
        namespace: str | None,
        continuation: str,
        limit: int,
        max_bytes: int,
    ) -> PulsePage:
        """Return a page or a normalized failure, closing every response."""


async def read_pulse_page(
    api: ApiClient | None,
    source: PulseSource,
    namespace: str | None,
    continuation: str,
    limit: int,
    max_bytes: int,
) -> PulsePage:
    """Stream at most the decoded-byte budget before parsing a LIST response.

    An error body is never read: Pulse classifies a failure by its status.
    """
    for budget in (limit, max_bytes):
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ValueError("Pulse budgets must be a positive integer")
    query = [("limit", str(limit))]
    if continuation:
        query.append(("continue", continuation))
    if source.field_selector:
        query.append(("fieldSelector", source.field_selector))
    path = api_path(source.group, source.version, source.plural, namespace)
    document = await read_bounded_json(api, path, query, max_bytes=max_bytes)
    items, token = decode_list_page(document, limit)
    return PulsePage(items, token)
```

In `src/korvid/core/pulse_collector.py`, replace the import `from korvid.k8s.pulse import PulseLimitError, PulseReader, PulseSource` with `from korvid.k8s.pulse import PulseReader, PulseSource`, add `KubeTooLargeError` to the `korvid.k8s.errors` import, and make the first check of `_failure_state`:

```python
    if isinstance(failure, KubeTooLargeError):
        return "capped", "Response byte or object cap reached"
```

In `src/korvid/k8s/client.py`, replace `from korvid.k8s.pulse import path_segment as _path_segment` with `from korvid.k8s.bounded_transport import path_segment as _path_segment`.

- [ ] **Step 5: Move the existing tests onto the shared transport**

- `tests/k8s/test_pulse.py`:
  - import `KubeTooLargeError` from `korvid.k8s.errors`;
  - in the `pulse` fixture, patch `importlib.import_module("korvid.k8s.bounded_transport")` rather than `module`, setting `"bounded_session"` rather than `"_bounded_session"`;
  - in `test_page_byte_cap_stops_stream_before_parsing_and_closes` and `test_server_ignoring_row_limit_is_explicitly_capped`, expect `KubeTooLargeError` rather than `pulse.PulseLimitError`;
  - in `test_interrupted_body_always_closes`, match `"Kubernetes API"` rather than `"Pulse"`;
  - in `test_invalid_compression_fails_closed_and_closes_response`, match `"Kubernetes API returned"` rather than `"Pulse returned"`.
- `tests/k8s/test_pulse_transport.py`:
  - import `bounded_session` from `korvid.k8s.bounded_transport` and `KubeTooLargeError` from `korvid.k8s.errors`, rather than `PulseLimitError` and `_bounded_session` from `korvid.k8s.pulse`;
  - expect `KubeTooLargeError` in `test_gzip_expansion_is_bounded_before_sdk_response_buffering`, and call `bounded_session` in `test_closed_transport_cannot_create_an_unconfigured_connection`;
  - in `_check_attempt_budget`, assert `all(result.coverage.state == "failed" for result in results)`. Neither a redirect nor a refused second attempt is a size cap.
- `tests/core/test_pulse_collector.py` and `tests/core/test_capability_pulse_mapping.py`: import `KubeTooLargeError` from `korvid.k8s.errors` and use it everywhere `PulseLimitError` was used.
- `tests/k8s/test_client.py`: in `test_path_segment_rejects_traversal_segments`, import `path_segment` from `korvid.k8s.bounded_transport`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_bounded_transport.py tests/k8s/test_pulse.py tests/k8s/test_pulse_transport.py tests/core/test_pulse_collector.py tests/core/test_capability_pulse_mapping.py tests/k8s/test_client.py -q`
Expected: all pass.

Run: `uv run --frozen ruff check src/korvid/k8s src/korvid/core tests/k8s tests/core && uv run --frozen mypy src/korvid/k8s src/korvid/core/pulse_collector.py tests/k8s/test_bounded_transport.py tests/k8s/fake_http.py`
Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/k8s src/korvid/core/pulse_collector.py tests/k8s tests/core
git commit -m "k8s: share Pulse's bounded transport and categorize its errors"
```

### Task 2: Evidence requests on the wire, and a reader that can only read

**Files:**
- Create: `src/korvid/k8s/evidence_requests.py`
- Modify: `src/korvid/k8s/client.py` (the class bases and two methods before `_request_json`)
- Create: `tests/k8s/test_evidence_requests.py`

**Interfaces:**
- Consumes: from Task 1, `ERROR_BODY_LIMIT`, `JSON`, `api_path`, `decode_list_page` and `read_bounded_json`.
- Produces in `korvid.k8s.evidence_requests`:
  - `METADATA_LIST` and `METADATA_OBJECT`, the `PartialObjectMetadata` media types;
  - `EvidenceTarget(group, version, resource, namespace=None, metadata_only=False)`;
  - `EvidencePage(items, continuation="")`;
  - the ABC `EvidenceReader` with exactly two abstract methods, `async list_evidence_page(target, *, limit, continuation, max_bytes) -> EvidencePage` and `async get_evidence_object(target, name, *, max_bytes) -> dict[str, Any]`;
  - `async read_evidence_page(api, target, *, limit, continuation, max_bytes) -> EvidencePage` and `async read_evidence_object(api, target, name, *, max_bytes) -> dict[str, Any]`.
- `KubeClient` becomes an `EvidenceReader`.

- [ ] **Step 1: Write the failing test**

Create `tests/k8s/test_evidence_requests.py`:

```python
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
    return url.path, parse_qsl(url.query, keep_blank_values=True)


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
    assert _accept(api) == "application/json"


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_evidence_requests.py -q`
Expected: a collection error, `ModuleNotFoundError: No module named 'korvid.k8s.evidence_requests'`.

- [ ] **Step 3: Write the requests**

Create `src/korvid/k8s/evidence_requests.py`:

```python
"""Bounded LIST pages and GETs for diagnostic evidence (#409, slice S3).

`korvid.core.evidence_reads` decides what to read and what an outcome means.
This module only reads, through `korvid.k8s.bounded_transport`. A metadata
read asks the server for `PartialObjectMetadata`. A server that refuses
answers 406, and nothing falls back to reading the object.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from kubernetes_asyncio.client import ApiClient

from korvid.k8s.bounded_transport import (
    ERROR_BODY_LIMIT,
    JSON,
    api_path,
    decode_list_page,
    read_bounded_json,
)

METADATA_LIST = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
METADATA_OBJECT = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"


@dataclass(frozen=True, slots=True)
class EvidenceTarget:
    """The collection an evidence read addresses, and whether it reads only metadata.

    `namespace` is `None` for a cluster-wide read.
    """

    group: str
    version: str
    resource: str
    namespace: str | None = None
    metadata_only: bool = False


@dataclass(frozen=True, slots=True)
class EvidencePage:
    """One LIST page that passed its byte and object budgets."""

    items: tuple[dict[str, Any], ...]
    continuation: str = ""


class EvidenceReader(ABC):
    """Reads evidence and nothing else: a LIST page and a GET are its only operations."""

    @abstractmethod
    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        """One page of at most `limit` items, read within `max_bytes`."""

    @abstractmethod
    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        """The object `name`, read within `max_bytes`."""


async def read_evidence_page(
    api: ApiClient | None,
    target: EvidenceTarget,
    *,
    limit: int,
    continuation: str,
    max_bytes: int,
) -> EvidencePage:
    """LIST one page of `target`, keeping up to `ERROR_BODY_LIMIT` of an error body."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("a page size is a positive integer")
    query = [("limit", str(limit))]
    if continuation:
        query.append(("continue", continuation))
    document = await read_bounded_json(
        api,
        api_path(target.group, target.version, target.resource, target.namespace),
        query,
        max_bytes=max_bytes,
        accept=METADATA_LIST if target.metadata_only else JSON,
        error_body_limit=ERROR_BODY_LIMIT,
    )
    items, token = decode_list_page(document, limit)
    return EvidencePage(items, token)


async def read_evidence_object(
    api: ApiClient | None, target: EvidenceTarget, name: str, *, max_bytes: int
) -> dict[str, Any]:
    """GET the object `name` of `target`, keeping up to `ERROR_BODY_LIMIT` of an error body."""
    if not name:
        raise ValueError("an object read names its object")
    return await read_bounded_json(
        api,
        api_path(target.group, target.version, target.resource, target.namespace, name),
        [],
        max_bytes=max_bytes,
        accept=METADATA_OBJECT if target.metadata_only else JSON,
        error_body_limit=ERROR_BODY_LIMIT,
    )
```

In `src/korvid/k8s/client.py`:

1. Add `evidence_requests` to the `from korvid.k8s import pulse` import: `from korvid.k8s import evidence_requests, pulse`.
2. Make `KubeClient` an evidence reader: `class KubeClient(ReadOps, WriteOps, pulse.PulseReader, evidence_requests.EvidenceReader):`.
3. After `read_pulse_page`, add:

```python
    async def list_evidence_page(
        self,
        target: evidence_requests.EvidenceTarget,
        *,
        limit: int,
        continuation: str,
        max_bytes: int,
    ) -> evidence_requests.EvidencePage:
        return await evidence_requests.read_evidence_page(
            self._api, target, limit=limit, continuation=continuation, max_bytes=max_bytes
        )

    async def get_evidence_object(
        self, target: evidence_requests.EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        return await evidence_requests.read_evidence_object(
            self._api, target, name, max_bytes=max_bytes
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_evidence_requests.py tests/k8s/test_client.py -q`
Expected: all pass.

Run: `uv run --frozen python scripts/check_source_size.py && uv run --frozen mypy src/korvid/k8s tests/k8s/test_evidence_requests.py && uv run --frozen tach check`
Expected: exit 0 with `client.py` under 1,944 lines, no mypy findings, and `All modules validated!`.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/k8s/evidence_requests.py src/korvid/k8s/client.py tests/k8s/test_evidence_requests.py
git commit -m "k8s: bounded evidence requests, read-only by construction"
```

### Task 3: `read_list` and `read_object`

**Files:**
- Create: `src/korvid/core/evidence_reads.py`
- Modify: `src/korvid/core/capability_classifier.py` (`STATUS_BODY_LIMIT`)
- Create: `tests/core/test_evidence_reads.py`
- Modify: `tests/test_optional_extras.py`

**Interfaces:**
- Consumes:
  - from Task 2, `EvidenceReader`, `EvidenceTarget` and `EvidencePage`;
  - from Task 1, `ERROR_BODY_LIMIT`;
  - from S1, `ApiSource`, `Observation`, `RunToken`, `classify`, `ReadRequest`, `ReadKind` and `Classification`.
- Produces in `korvid.core.evidence_reads`:
  - `LIST_PAGE_SIZE`, `DEFAULT_MAX_PAGES`, `MAX_PAGES`, `DEFAULT_DEADLINE`, `MAX_DEADLINE`, `DEFAULT_MAX_BYTES`, `MAX_BYTES`, `DEFAULT_RETRY_AFTER`;
  - `PAYLOAD_RESOURCES`, `PAYLOAD_METADATA_FIELDS`, `LAST_APPLIED_ANNOTATION`;
  - `ListEvidence(items, observation)` and `ObjectEvidence(item, observation)`;
  - `async read_list(reader, source, *, page_size=LIST_PAGE_SIZE, max_pages=DEFAULT_MAX_PAGES, deadline=DEFAULT_DEADLINE, max_bytes=DEFAULT_MAX_BYTES, token=None, clock=...) -> ListEvidence`;
  - `async read_object(reader, source, *, deadline=DEFAULT_DEADLINE, max_bytes=DEFAULT_MAX_BYTES, token=None, clock=...) -> ObjectEvidence`.

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_evidence_reads.py`:

```python
"""Bounded, read-only evidence reads (#409, slice S3)."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core import evidence_reads, pulse_collector
from korvid.core.capability import (
    DEFAULT_POLICY,
    ApiSource,
    Basis,
    EvidenceState,
    Observation,
    Projection,
    RunToken,
    SourceScope,
    Verb,
    conclusive,
)
from korvid.core.capability_classifier import STATUS_BODY_LIMIT
from korvid.core.evidence_reads import (
    DEFAULT_MAX_BYTES,
    LAST_APPLIED_ANNOTATION,
    LIST_PAGE_SIZE,
    read_list,
    read_object,
)
from korvid.k8s.bounded_transport import ERROR_BODY_LIMIT
from korvid.k8s.errors import ApiStatusError, KubeMalformedError, KubeTooLargeError
from korvid.k8s.evidence_requests import EvidencePage, EvidenceReader, EvidenceTarget

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
PODS = ApiSource("", "pods", TEAM, version="v1", verb=Verb.LIST)
POD = ApiSource("", "pods", TEAM, version="v1", verb=Verb.GET, name="web-0")
METADATA_PODS = dataclasses.replace(PODS, projection=Projection.METADATA)
METADATA_POD = dataclasses.replace(POD, projection=Projection.METADATA)
POD_TARGET = EvidenceTarget("", "v1", "pods", "team-a")
#: Stands for a request the server never answers.
STALL = object()

PAYLOAD_METADATA = {
    "name": "settings",
    "namespace": "team-a",
    "uid": "uid-settings",
    "resourceVersion": "42",
    "creationTimestamp": "2026-10-26T08:00:00Z",
    "deletionTimestamp": "2026-10-26T08:30:00Z",
    "labels": {"app": "web"},
    "ownerReferences": [{"kind": "Deployment", "name": "web"}],
    "annotations": {
        LAST_APPLIED_ANNOTATION: '{"data": {"password": "hunter2"}}',
        "note": "hunter2",
    },
    "managedFields": [{"manager": "kubectl", "fieldsV1": {"f:data": {"f:password": {}}}}],
    "finalizers": ["example.io/keep"],
    "generateName": "settings-",
}
KEPT_PAYLOAD_METADATA = {
    key: PAYLOAD_METADATA[key]
    for key in (
        "name",
        "namespace",
        "uid",
        "resourceVersion",
        "creationTimestamp",
        "deletionTimestamp",
        "labels",
        "ownerReferences",
    )
}
PAYLOAD_ITEM = {"metadata": PAYLOAD_METADATA, "data": {"password": "hunter2"}, "type": "Opaque"}


def _clock() -> datetime:
    return NOW


def _pod(name: str, uid: str = "") -> dict[str, Any]:
    metadata = {"name": name, "namespace": "team-a", "uid": uid or f"uid-{name}"}
    return {"metadata": metadata, "spec": {"nodeName": "n1"}}


def _status(
    code: int, reason: str, *, name: str = "", kind: str = "", retry_after: float | None = None
) -> ApiStatusError:
    details = {"name": name, "group": "", "kind": kind}
    body = json.dumps({"kind": "Status", "reason": reason, "details": details})
    return ApiStatusError(code, reason, body, retry_after_seconds=retry_after)


def _state(observation: Observation) -> tuple[EvidenceState, Basis]:
    return observation.state, observation.basis


def _names(items: tuple[dict[str, Any], ...]) -> list[str]:
    return [item["metadata"]["name"] for item in items]


@dataclasses.dataclass(frozen=True)
class Late:
    """An answer that arrives `seconds` later, past any deadline the tests set."""

    result: object
    seconds: float = 0.1


class FakeReader(EvidenceReader):
    """Answers each request with the next scripted outcome, and records the requests."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.lists: list[tuple[EvidenceTarget, int, str, int]] = []
        self.gets: list[tuple[EvidenceTarget, str, int]] = []

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.lists.append((target, limit, continuation, max_bytes))
        page = await self._next()
        assert isinstance(page, EvidencePage)
        return page

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.gets.append((target, name, max_bytes))
        document = await self._next()
        assert isinstance(document, dict)
        return document

    async def _next(self) -> object:
        outcome = self.outcomes.pop(0)
        if outcome is STALL:
            await asyncio.Event().wait()
        if isinstance(outcome, Late):
            # Blocks without yielding, so no timeout can interrupt it.
            time.sleep(outcome.seconds)
            outcome = outcome.result
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_the_default_budgets_are_pulses() -> None:
    assert (
        evidence_reads.LIST_PAGE_SIZE,
        evidence_reads.DEFAULT_MAX_PAGES,
        evidence_reads.DEFAULT_DEADLINE,
        evidence_reads.DEFAULT_MAX_BYTES,
    ) == (
        pulse_collector.PAGE_SIZE,
        pulse_collector.MAX_PAGES,
        pulse_collector.SOURCE_DEADLINE,
        pulse_collector.PAGE_BYTES,
    )


def test_the_classifier_reads_no_more_of_a_status_than_a_reader_keeps() -> None:
    assert STATUS_BODY_LIMIT == ERROR_BODY_LIMIT


async def test_a_list_that_ends_is_available() -> None:
    reader = FakeReader(EvidencePage((_pod("a"),), "next"), EvidencePage((_pod("b"),)))

    result = await read_list(reader, PODS, clock=_clock)

    assert _names(result.items) == ["a", "b"]
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert (result.observation.items, result.observation.observed_at) == (2, NOW)
    assert result.observation.source == PODS
    assert reader.lists == [
        (POD_TARGET, LIST_PAGE_SIZE, "", DEFAULT_MAX_BYTES),
        (POD_TARGET, LIST_PAGE_SIZE, "next", DEFAULT_MAX_BYTES),
    ]


async def test_an_empty_collection_is_available() -> None:
    result = await read_list(FakeReader(EvidencePage(())), PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert result.items == ()


async def test_a_cluster_wide_read_names_no_namespace() -> None:
    nodes = ApiSource("", "nodes", SourceScope.cluster(), version="v1", verb=Verb.LIST)
    reader = FakeReader(EvidencePage(()))

    await read_list(reader, nodes, page_size=10, max_bytes=4096)

    assert reader.lists == [(EvidenceTarget("", "v1", "nodes"), 10, "", 4096)]


@pytest.mark.parametrize(
    "items", [pytest.param((_pod("a"),), id="with-items"), pytest.param((), id="empty-pages")]
)
async def test_the_page_cap_with_a_token_pending_is_truncated(
    items: tuple[dict[str, Any], ...],
) -> None:
    reader = FakeReader(EvidencePage(items, "p2"), EvidencePage(items, "p3"))

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.TRUNCATED)
    assert result.observation.items == 2 * len(items)
    assert len(reader.lists) == 2


async def test_more_pages_are_read_up_to_a_raised_cap() -> None:
    pages = [EvidencePage((_pod(str(index)),), f"p{index + 1}") for index in range(9)]
    reader = FakeReader(*pages, EvidencePage((_pod("9"),)))

    result = await read_list(reader, PODS, max_pages=10, clock=_clock)

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert len(result.items) == 10


@pytest.mark.parametrize(
    ("failure", "basis"),
    [
        pytest.param(_status(405, "MethodNotAllowed"), Basis.DISCOVERY_STALE, id="405"),
        pytest.param(_status(410, "Expired"), Basis.CONTINUATION_EXPIRED, id="expired"),
        pytest.param(KubeTooLargeError("byte cap"), Basis.TOO_LARGE, id="too-large"),
        pytest.param(ApiStatusError(503, "Service Unavailable"), Basis.SERVER_ERROR, id="503"),
        pytest.param(ApiStatusError(403, "Forbidden"), Basis.FORBIDDEN, id="403"),
    ],
)
@pytest.mark.parametrize(
    "items", [pytest.param((_pod("a"),), id="with-items"), pytest.param((), id="empty-page")]
)
async def test_a_failure_after_an_accepted_page_keeps_it(
    failure: Exception, basis: Basis, items: tuple[dict[str, Any], ...]
) -> None:
    reader = FakeReader(EvidencePage(items, "next"), failure)

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, basis)
    assert len(result.items) == len(items)


async def test_a_refused_metadata_continuation_keeps_the_pages() -> None:
    reader = FakeReader(EvidencePage((_pod("a"),), "next"), ApiStatusError(406, "Not Acceptable"))

    result = await read_list(reader, METADATA_PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.PROJECTION_UNSUPPORTED)
    assert _names(result.items) == ["a"]


@pytest.mark.parametrize(
    ("failure", "state", "basis"),
    [
        pytest.param(
            ApiStatusError(401, "Unauthorized"),
            EvidenceState.DENIED,
            Basis.UNAUTHENTICATED,
            id="401",
        ),
        pytest.param(
            ApiStatusError(403, "Forbidden"), EvidenceState.DENIED, Basis.FORBIDDEN, id="403"
        ),
        pytest.param(
            _status(404, "NotFound"), EvidenceState.UNAVAILABLE, Basis.DISCOVERY_STALE, id="404"
        ),
        pytest.param(
            KubeTooLargeError("byte cap"),
            EvidenceState.UNAVAILABLE,
            Basis.TOO_LARGE,
            id="too-large",
        ),
        pytest.param(
            KubeMalformedError("bad"), EvidenceState.UNAVAILABLE, Basis.MALFORMED, id="malformed"
        ),
    ],
)
async def test_a_failure_before_any_page_reads_nothing(
    failure: Exception, state: EvidenceState, basis: Basis
) -> None:
    result = await read_list(FakeReader(failure), PODS, clock=_clock)

    assert _state(result.observation) == (state, basis)
    assert (result.items, result.observation.items) == ((), 0)


async def test_a_failure_detail_never_carries_the_response_body() -> None:
    failure = ApiStatusError(403, "Forbidden", '{"message": "token secret-token denied"}')

    result = await read_list(FakeReader(failure), PODS, clock=_clock)

    assert result.observation.detail == "HTTP 403"


async def test_a_list_whose_metadata_projection_is_refused_is_unsupported() -> None:
    reader = FakeReader(ApiStatusError(406, "Not Acceptable"))

    result = await read_list(reader, METADATA_PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNSUPPORTED, Basis.PROJECTION_UNSUPPORTED)
    assert [request[0] for request in reader.lists] == [
        dataclasses.replace(POD_TARGET, metadata_only=True)
    ]


async def test_an_object_whose_metadata_projection_is_refused_is_unsupported() -> None:
    reader = FakeReader(ApiStatusError(406, "Not Acceptable"))

    result = await read_object(reader, METADATA_POD, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNSUPPORTED, Basis.PROJECTION_UNSUPPORTED)
    assert [request[0] for request in reader.gets] == [
        dataclasses.replace(POD_TARGET, metadata_only=True)
    ]


async def test_a_429_is_retried_once_within_the_deadline() -> None:
    throttled = _status(429, "TooManyRequests", retry_after=0.0)
    reader = FakeReader(throttled, EvidencePage((_pod("a"),)))

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert len(reader.lists) == 2


async def test_a_second_429_is_throttled() -> None:
    reader = FakeReader(
        _status(429, "TooManyRequests", retry_after=0.0),
        _status(429, "TooManyRequests", retry_after=0.0),
    )

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.THROTTLED)
    assert len(reader.lists) == 2


async def test_a_429_whose_wait_outlasts_the_deadline_is_not_retried() -> None:
    reader = FakeReader(_status(429, "TooManyRequests", retry_after=60.0))

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.THROTTLED)
    assert len(reader.lists) == 1


async def test_a_retry_that_wakes_after_the_deadline_is_never_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def oversleep(_delay: float) -> None:
        time.sleep(0.1)

    monkeypatch.setattr(evidence_reads, "_pause", oversleep)
    reader = FakeReader(_status(429, "TooManyRequests", retry_after=0.0), EvidencePage(()))

    result = await read_list(reader, PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)
    assert len(reader.lists) == 1


async def test_a_read_retries_only_one_429() -> None:
    reader = FakeReader(
        EvidencePage((_pod("a"),), "p2"),
        _status(429, "TooManyRequests", retry_after=0.0),
        EvidencePage((_pod("b"),), "p3"),
        _status(429, "TooManyRequests", retry_after=0.0),
    )

    result = await read_list(reader, PODS, max_pages=3, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.THROTTLED)
    assert _names(result.items) == ["a", "b"]
    assert len(reader.lists) == 4


@pytest.mark.parametrize(("default", "requests"), [(0.0, 2), (60.0, 1)])
async def test_a_429_without_retry_after_waits_the_default(
    monkeypatch: pytest.MonkeyPatch, default: float, requests: int
) -> None:
    monkeypatch.setattr(evidence_reads, "DEFAULT_RETRY_AFTER", default)
    reader = FakeReader(_status(429, "TooManyRequests"), EvidencePage(()))

    await read_list(reader, PODS, clock=_clock)

    assert len(reader.lists) == requests


async def test_the_deadline_before_any_page_reads_nothing() -> None:
    result = await read_list(FakeReader(STALL), PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)
    assert result.items == ()


async def test_the_deadline_after_a_page_keeps_it() -> None:
    reader = FakeReader(EvidencePage((_pod("a"),), "next"), STALL)

    result = await read_list(reader, PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.DEADLINE)
    assert _names(result.items) == ["a"]


async def test_a_page_that_arrives_after_the_deadline_is_not_accepted() -> None:
    reader = FakeReader(Late(EvidencePage((_pod("a"),), "next")), EvidencePage((_pod("b"),)))

    result = await read_list(reader, PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)
    assert result.items == ()
    assert len(reader.lists) == 1


async def test_a_later_page_that_arrives_after_the_deadline_is_not_accepted() -> None:
    reader = FakeReader(EvidencePage((_pod("a"),), "next"), Late(EvidencePage((_pod("b"),))))

    result = await read_list(reader, PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.DEADLINE)
    assert _names(result.items) == ["a"]


async def test_an_object_that_arrives_after_the_deadline_is_not_accepted() -> None:
    result = await read_object(FakeReader(Late(_pod("web-0"))), POD, deadline=0.05, clock=_clock)

    assert result.item is None
    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)


async def test_an_absence_reported_after_the_deadline_proves_nothing() -> None:
    late_absence = Late(_status(404, "NotFound", name="web-0", kind="pods"))

    result = await read_object(FakeReader(late_absence), POD, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)


async def test_a_failure_reported_after_the_deadline_is_the_deadline() -> None:
    late_refusal = Late(ApiStatusError(403, "Forbidden"))

    result = await read_list(FakeReader(late_refusal), PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)


async def test_cancelling_a_read_cancels_it() -> None:
    reader = FakeReader(STALL)
    task = asyncio.create_task(read_list(reader, PODS))
    await asyncio.sleep(0)
    assert len(reader.lists) == 1

    task.cancel()

    with pytest.raises(asyncio.CancelledError, match=r"^$"):
        await task


async def test_a_cancelled_request_is_never_classified() -> None:
    with pytest.raises(asyncio.CancelledError, match=r"^$"):
        await read_list(FakeReader(asyncio.CancelledError()), PODS)


async def test_a_timeout_that_is_not_the_deadline_is_never_classified() -> None:
    with pytest.raises(TimeoutError, match="reader timed out"):
        await read_list(FakeReader(TimeoutError("reader timed out")), PODS)


async def test_a_repeated_continue_token_is_malformed() -> None:
    reader = FakeReader(EvidencePage((_pod("a"),), "same"), EvidencePage((_pod("b"),), "same"))

    result = await read_list(reader, PODS, max_pages=3, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.MALFORMED)
    assert _names(result.items) == ["a"]


async def test_a_page_with_an_item_without_metadata_is_never_accepted() -> None:
    reader = FakeReader(EvidencePage((_pod("a"), {"spec": {}})))

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.MALFORMED)
    assert result.items == ()


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"name": "a", "namespace": "team-b", "uid": "uid-a"}, id="another-namespace"),
        pytest.param({"name": "a", "uid": "uid-a"}, id="no-namespace"),
    ],
)
async def test_an_item_from_outside_the_namespace_read_is_malformed(
    metadata: dict[str, Any],
) -> None:
    reader = FakeReader(EvidencePage((_pod("b"), {"metadata": metadata})))

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.MALFORMED)
    assert result.items == ()


async def test_a_cluster_wide_read_keeps_items_from_every_namespace() -> None:
    every_namespace = ApiSource("", "pods", SourceScope.cluster(), version="v1", verb=Verb.LIST)
    other = {"metadata": {"name": "b", "namespace": "team-b", "uid": "uid-b"}}

    result = await read_list(FakeReader(EvidencePage((_pod("a"), other))), every_namespace)

    assert _names(result.items) == ["a", "b"]


async def test_a_metadata_read_returns_only_metadata() -> None:
    result = await read_list(FakeReader(EvidencePage((_pod("a"),))), METADATA_PODS, clock=_clock)

    assert result.items == ({"metadata": _pod("a")["metadata"]},)


@pytest.mark.parametrize("resource", ["configmaps", "secrets"])
async def test_no_configuration_payload_reaches_a_diagnostic_through_a_list(resource: str) -> None:
    source = ApiSource(
        "", resource, TEAM, version="v1", verb=Verb.LIST, projection=Projection.METADATA
    )

    payload = copy.deepcopy(PAYLOAD_ITEM)

    result = await read_list(FakeReader(EvidencePage((payload,))), source, clock=_clock)

    assert result.items == ({"metadata": KEPT_PAYLOAD_METADATA},)
    assert "hunter2" not in json.dumps(result.items)
    assert payload == PAYLOAD_ITEM


@pytest.mark.parametrize("resource", ["configmaps", "secrets"])
async def test_no_configuration_payload_reaches_a_diagnostic_through_a_get(resource: str) -> None:
    source = ApiSource(
        "",
        resource,
        TEAM,
        version="v1",
        verb=Verb.GET,
        name="settings",
        projection=Projection.METADATA,
    )

    payload = copy.deepcopy(PAYLOAD_ITEM)

    result = await read_object(FakeReader(payload), source, clock=_clock)

    assert result.item == {"metadata": KEPT_PAYLOAD_METADATA}
    assert "hunter2" not in json.dumps(result.item)
    assert payload == PAYLOAD_ITEM


async def test_no_metadata_keeps_the_last_applied_configuration_or_managed_fields() -> None:
    metadata = {
        **_pod("web-0")["metadata"],
        "annotations": {LAST_APPLIED_ANNOTATION: "{}", "team": "a"},
        "managedFields": [{"manager": "kubectl"}],
    }
    reader = FakeReader({"metadata": metadata, "spec": {"nodeName": "n1"}})

    result = await read_object(reader, POD, clock=_clock)

    assert result.item == {
        "metadata": {**_pod("web-0")["metadata"], "annotations": {"team": "a"}},
        "spec": {"nodeName": "n1"},
    }


TEMPLATE_METADATA = {
    "labels": {"app": "web"},
    "annotations": {LAST_APPLIED_ANNOTATION: '{"password": "hunter2"}', "team": "a"},
    "managedFields": [{"manager": "kubectl"}],
}
DEPLOYMENT = {
    "metadata": {"name": "web", "namespace": "team-a", "uid": "uid-web"},
    "spec": {"template": {"metadata": TEMPLATE_METADATA, "spec": {"containers": []}}},
}
SCRUBBED_TEMPLATE = {"labels": {"app": "web"}, "annotations": {"team": "a"}}


async def test_no_embedded_metadata_keeps_the_last_applied_configuration_through_a_list() -> None:
    source = ApiSource("apps", "deployments", TEAM, version="v1", verb=Verb.LIST)

    deployment = copy.deepcopy(DEPLOYMENT)

    result = await read_list(FakeReader(EvidencePage((deployment,))), source, clock=_clock)

    (item,) = result.items
    assert item["spec"]["template"]["metadata"] == SCRUBBED_TEMPLATE
    assert "hunter2" not in json.dumps(result.items)
    assert deployment == DEPLOYMENT


async def test_no_embedded_metadata_keeps_the_last_applied_configuration_through_a_get() -> None:
    source = ApiSource("apps", "deployments", TEAM, version="v1", verb=Verb.GET, name="web")

    deployment = copy.deepcopy(DEPLOYMENT)

    result = await read_object(FakeReader(deployment), source, clock=_clock)

    assert result.item is not None
    assert result.item["spec"]["template"]["metadata"] == SCRUBBED_TEMPLATE
    assert result.item["spec"]["template"]["spec"] == {"containers": []}
    assert deployment == DEPLOYMENT


async def test_an_object_too_deep_to_scrub_is_malformed() -> None:
    nested: dict[str, Any] = {}
    for _ in range(5000):
        nested = {"nested": nested}

    result = await read_list(FakeReader(EvidencePage(({**_pod("a"), "spec": nested},))), PODS)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.MALFORMED)


@pytest.mark.parametrize(
    "annotations",
    [
        pytest.param({LAST_APPLIED_ANNOTATION: "{}"}, id="emptied"),
        pytest.param("not a map", id="unreadable"),
    ],
)
async def test_annotations_with_nothing_left_to_keep_are_dropped(annotations: object) -> None:
    item = {"metadata": {**_pod("a")["metadata"], "annotations": annotations}}

    result = await read_list(FakeReader(EvidencePage((item,))), PODS, clock=_clock)

    assert result.items == ({"metadata": _pod("a")["metadata"]},)


async def test_an_unresolved_source_is_never_read() -> None:
    reader = FakeReader()

    with pytest.raises(ValueError, match="resolved for list"):
        await read_list(reader, ApiSource("", "pods", TEAM))
    with pytest.raises(ValueError, match="resolved for get"):
        await read_object(reader, ApiSource("", "pods", TEAM, name="web-0"))

    assert (reader.lists, reader.gets) == ([], [])


async def test_each_read_refuses_a_source_resolved_for_the_other_verb() -> None:
    reader = FakeReader()

    with pytest.raises(ValueError, match="resolved for list"):
        await read_list(reader, POD)
    with pytest.raises(ValueError, match="resolved for get"):
        await read_object(reader, PODS)

    assert (reader.lists, reader.gets) == ([], [])


@pytest.mark.parametrize("resource", ["configmaps", "secrets"])
async def test_a_configuration_payload_is_never_read_as_an_object(resource: str) -> None:
    reader = FakeReader()
    listing = ApiSource("", resource, TEAM, version="v1", verb=Verb.LIST)
    getting = ApiSource("", resource, TEAM, version="v1", verb=Verb.GET, name="settings")

    with pytest.raises(ValueError, match="metadata only"):
        await read_list(reader, listing)
    with pytest.raises(ValueError, match="metadata only"):
        await read_object(reader, getting)

    assert (reader.lists, reader.gets) == ([], [])


async def test_a_subresource_is_not_an_evidence_read() -> None:
    source = dataclasses.replace(POD, subresource="status")

    with pytest.raises(ValueError, match="subresources"):
        await read_object(FakeReader(), source)


@pytest.mark.parametrize("scope", [SourceScope.node("node-1"), SourceScope.provider("prometheus")])
async def test_an_api_read_is_cluster_or_namespace_scoped(scope: SourceScope) -> None:
    source = ApiSource("", "pods", scope, version="v1", verb=Verb.LIST)

    with pytest.raises(ValueError, match="cluster- or namespace-scoped"):
        await read_list(FakeReader(), source)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"page_size": 0}, "page_size"),
        ({"page_size": 101}, "page_size"),
        ({"page_size": True}, "page_size"),
        ({"max_pages": 0}, "max_pages"),
        ({"max_pages": 11}, "max_pages"),
        ({"deadline": 0}, "deadline"),
        ({"deadline": 15.5}, "deadline"),
        ({"deadline": float("nan")}, "deadline"),
        ({"max_bytes": 0}, "max_bytes"),
        ({"max_bytes": 2 * 1024 * 1024 + 1}, "max_bytes"),
    ],
)
async def test_a_list_budget_out_of_range_is_refused(options: dict[str, Any], message: str) -> None:
    reader = FakeReader()

    with pytest.raises(ValueError, match=message):
        await read_list(reader, PODS, **options)

    assert reader.lists == []


@pytest.mark.parametrize(
    ("options", "message"), [({"deadline": 16}, "deadline"), ({"max_bytes": 0}, "max_bytes")]
)
async def test_an_object_budget_out_of_range_is_refused(
    options: dict[str, Any], message: str
) -> None:
    reader = FakeReader()

    with pytest.raises(ValueError, match=message):
        await read_object(reader, POD, **options)

    assert reader.gets == []


async def test_an_object_read_records_the_uid_it_saw() -> None:
    source = dataclasses.replace(POD, expected_uid="uid-web-0")
    reader = FakeReader(_pod("web-0"))

    result = await read_object(reader, source, clock=_clock)
    observation = result.observation

    assert result.item == _pod("web-0")
    assert _state(observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert (observation.items, observation.observed_uid, observation.recreated) == (
        1,
        "uid-web-0",
        False,
    )
    assert conclusive(observation, DEFAULT_POLICY, NOW)
    assert reader.gets == [(POD_TARGET, "web-0", DEFAULT_MAX_BYTES)]


async def test_a_recreated_object_is_never_conclusive() -> None:
    source = dataclasses.replace(POD, expected_uid="uid-old")

    result = await read_object(FakeReader(_pod("web-0", "uid-new")), source, clock=_clock)

    assert result.observation.observed_uid == "uid-new"
    assert result.observation.recreated
    assert not conclusive(result.observation, DEFAULT_POLICY, NOW)


async def test_an_absent_object_is_available_and_absent() -> None:
    reader = FakeReader(_status(404, "NotFound", name="web-0", kind="pods"))

    result = await read_object(reader, POD, clock=_clock)

    assert result.item is None
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBJECT_ABSENT)
    assert result.observation.items == 0


async def test_a_404_that_names_another_object_suggests_stale_discovery() -> None:
    reader = FakeReader(_status(404, "NotFound", name="other", kind="pods"))

    result = await read_object(reader, POD, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DISCOVERY_STALE)


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"spec": {}}, id="no-metadata"),
        pytest.param({"metadata": {"name": "web-0", "namespace": "team-a"}}, id="no-uid"),
        pytest.param(
            {"metadata": {"name": "web-0", "namespace": "team-a", "uid": ""}}, id="empty-uid"
        ),
        pytest.param(_pod("web-1"), id="another-object"),
        pytest.param(
            {"metadata": {"name": "web-0", "namespace": "team-b", "uid": "uid-web-0"}},
            id="another-namespace",
        ),
        pytest.param({"metadata": {"name": "web-0", "uid": "uid-web-0"}}, id="no-namespace"),
    ],
)
async def test_an_object_that_is_not_the_one_asked_for_is_malformed(
    document: dict[str, Any],
) -> None:
    result = await read_object(FakeReader(document), POD, clock=_clock)

    assert result.item is None
    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.MALFORMED)


async def test_an_object_read_stops_at_its_deadline() -> None:
    result = await read_object(FakeReader(STALL), POD, deadline=0.05, clock=_clock)

    assert result.item is None
    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)


async def test_an_object_read_retries_one_429() -> None:
    reader = FakeReader(_status(429, "TooManyRequests", retry_after=0.0), _pod("web-0"))

    result = await read_object(reader, POD, clock=_clock)

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert len(reader.gets) == 2


async def test_both_reads_record_the_run_token() -> None:
    token = RunToken(1, 2, 3)

    listed = await read_list(FakeReader(EvidencePage(())), PODS, token=token, clock=_clock)
    got = await read_object(FakeReader(_pod("web-0")), POD, token=token, clock=_clock)

    assert listed.observation.token == got.observation.token == token
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest -p no:tach tests/core/test_evidence_reads.py -q`
Expected: a collection error, `ImportError: cannot import name 'evidence_reads' from 'korvid.core'`.

- [ ] **Step 3: Tie the classifier's Status budget to the reader's**

In `src/korvid/core/capability_classifier.py`, add `from korvid.k8s.bounded_transport import ERROR_BODY_LIMIT` and define the constant from it:

```python
#: Readers keep at most this many bytes of an error body, and the classifier
#: parses no more.
STATUS_BODY_LIMIT = ERROR_BODY_LIMIT
```

- [ ] **Step 4: Write the reads**

Create `src/korvid/core/evidence_reads.py`:

```python
"""Bounded, read-only evidence reads for capability-aware diagnostics (#409, slice S3).

`read_list` and `read_object` read one `ApiSource` that discovery resolved,
and return what they read with one `Observation` of it. Each read is bounded
in pages, bytes and time. It lets cancellation through, and retries nothing
but one 429 whose wait fits its deadline. `korvid.core.capability_classifier`
classifies every failure.

What a diagnostic receives is not what crossed the wire:

- a ConfigMap or Secret is never read as an object, so its data never is;
- its metadata keeps only `PAYLOAD_METADATA_FIELDS`;
- no metadata keeps `managedFields` or the `LAST_APPLIED_ANNOTATION`, which
  embeds the whole object: neither an object's own, nor any it embeds, such
  as a pod template's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from korvid.core.capability import (
    ApiSource,
    Basis,
    EvidenceState,
    Observation,
    Projection,
    RunToken,
    ScopeKind,
    Verb,
)
from korvid.core.capability_classifier import Classification, ReadKind, ReadRequest, classify
from korvid.k8s.errors import ApiStatusError, KubeClientError, KubeMalformedError
from korvid.k8s.evidence_requests import EvidencePage, EvidenceReader, EvidenceTarget

_T = TypeVar("_T")

#: The default budgets are Pulse's: pages of 100 objects, two pages, five
#: seconds and 256 KiB a response. A diagnostic may raise them to the maxima.
LIST_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 2
MAX_PAGES = 10
DEFAULT_DEADLINE = 5.0
MAX_DEADLINE = 15.0
DEFAULT_MAX_BYTES = 256 * 1024
MAX_BYTES = 2 * 1024 * 1024
#: How long to wait before retrying a 429 that sent no usable `Retry-After`.
DEFAULT_RETRY_AFTER = 1.0

#: Resources whose objects carry configuration payloads. They are read as
#: metadata only, and only `PAYLOAD_METADATA_FIELDS` of that is kept: any
#: annotation can carry configuration data.
PAYLOAD_RESOURCES = frozenset({"configmaps", "secrets"})
PAYLOAD_METADATA_FIELDS = (
    "name",
    "namespace",
    "uid",
    "resourceVersion",
    "creationTimestamp",
    "deletionTimestamp",
    "labels",
    "ownerReferences",
)
#: Embeds the whole object as it was last applied, data included.
LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"
_DEADLINE = "deadline reached"
#: How a read waits out a 429 before retrying it.
_pause = asyncio.sleep


@dataclass(frozen=True, slots=True)
class ListEvidence:
    """The objects a LIST read, and what the read established."""

    items: tuple[dict[str, Any], ...]
    observation: Observation


@dataclass(frozen=True, slots=True)
class ObjectEvidence:
    """The object a GET read, if it read one, and what the read established."""

    item: dict[str, Any] | None
    observation: Observation


def _now() -> datetime:
    return datetime.now(UTC)


async def read_list(
    reader: EvidenceReader,
    source: ApiSource,
    *,
    page_size: int = LIST_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    deadline: float = DEFAULT_DEADLINE,
    max_bytes: int = DEFAULT_MAX_BYTES,
    token: RunToken | None = None,
    clock: Callable[[], datetime] = _now,
) -> ListEvidence:
    """LIST `source` a page at a time, within its budgets.

    The read is `AVAILABLE` only when the last page carried no continue token:
    a page with no items and a token pending proves nothing. After an accepted
    page, it keeps what it read and is `PARTIAL`, `TRUNCATED` at the page cap,
    `DEADLINE` at the deadline, and with a failure's own basis otherwise.

    Raises:
        ValueError: if `source` was not resolved for `list`, is a ConfigMap or
            Secret read as objects, or a budget is out of range.
    """
    target = _target(source, Verb.LIST)
    _check_count("page_size", page_size, LIST_PAGE_SIZE)
    _check_count("max_pages", max_pages, MAX_PAGES)
    _check_budgets(deadline, max_bytes)
    attempts = _Attempts(_deadline_at(deadline))
    run = _ListRun(reader, source, target, page_size, max_pages, max_bytes, attempts)
    try:
        await run.read()
    except _DeadlineReachedError:
        outcome = run.at_deadline()
    except (ApiStatusError, KubeClientError) as failure:
        outcome = classify(failure, ReadRequest(ReadKind.LIST, source, continuation=run.pages > 0))
    else:
        outcome = run.finished()
    items = tuple(run.items)
    return ListEvidence(items, _observe(source, outcome, clock(), len(items), token))


async def read_object(
    reader: EvidenceReader,
    source: ApiSource,
    *,
    deadline: float = DEFAULT_DEADLINE,
    max_bytes: int = DEFAULT_MAX_BYTES,
    token: RunToken | None = None,
    clock: Callable[[], datetime] = _now,
) -> ObjectEvidence:
    """GET the object `source` names, and record the UID it has.

    A `NotFound` that names the object is `AVAILABLE`/`OBJECT_ABSENT`. An
    object whose UID differs from the one `source` expects is still returned,
    and its observation is `recreated`.

    Raises:
        ValueError: if `source` was not resolved for `get`, is a ConfigMap or
            Secret read as an object, or a budget is out of range.
    """
    target = _target(source, Verb.GET)
    _check_budgets(deadline, max_bytes)
    attempts = _Attempts(_deadline_at(deadline))
    try:
        document = await _call(
            attempts,
            lambda: reader.get_evidence_object(target, source.name, max_bytes=max_bytes),
        )
        item, uid = _named_object(source, document)
        attempts.check()
    except _DeadlineReachedError:
        outcome = Classification(EvidenceState.UNAVAILABLE, Basis.DEADLINE, _DEADLINE)
    except (ApiStatusError, KubeClientError) as failure:
        outcome = classify(failure, ReadRequest(ReadKind.GET, source))
    else:
        observed = Classification(EvidenceState.AVAILABLE, Basis.OBSERVED, "")
        return ObjectEvidence(item, _observe(source, observed, clock(), 1, token, uid))
    return ObjectEvidence(None, _observe(source, outcome, clock(), 0, token))


class _DeadlineReachedError(Exception):
    """The read's own deadline passed before it finished."""


@dataclass(slots=True)
class _Attempts:
    """When a read must finish, and whether it has spent its one 429 retry."""

    deadline_at: float
    retried: bool = False

    def retry_delay(self, failure: ApiStatusError) -> float | None:
        """How long to wait before retrying `failure`, or `None` not to retry it."""
        if failure.status != 429 or self.retried:
            return None
        seconds = failure.retry_after_seconds
        delay = DEFAULT_RETRY_AFTER if seconds is None else seconds
        if asyncio.get_running_loop().time() + delay >= self.deadline_at:
            return None
        return delay

    def check(self) -> None:
        """Refuse a result read or processed after the deadline.

        A timeout interrupts a request only where it waits, so a result can
        still be ready after the deadline.
        """
        _refuse_late(self.deadline_at)


@dataclass(slots=True)
class _ListRun:
    """One LIST in progress: the pages it accepted, and where it stopped."""

    reader: EvidenceReader
    source: ApiSource
    target: EvidenceTarget
    page_size: int
    max_pages: int
    max_bytes: int
    attempts: _Attempts
    items: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    continuation: str = ""

    async def read(self) -> None:
        """Accept pages until the last one, the page cap, or a failure."""
        seen: set[str] = set()
        while True:
            page = await _call(self.attempts, self._request)
            if page.continuation in seen:
                raise KubeMalformedError("Kubernetes API repeated a continue token")
            items = [_reduce_item(self.source, item) for item in page.items]
            self.attempts.check()
            self.items.extend(items)
            self.pages += 1
            self.continuation = page.continuation
            if not page.continuation or self.pages == self.max_pages:
                return
            seen.add(page.continuation)

    def finished(self) -> Classification:
        """The outcome of a read that stopped without a failure."""
        if self.continuation:
            detail = f"stopped after {self.pages} pages with more pending"
            return Classification(EvidenceState.PARTIAL, Basis.TRUNCATED, detail)
        return Classification(EvidenceState.AVAILABLE, Basis.OBSERVED, "")

    def at_deadline(self) -> Classification:
        """The outcome of a read its deadline stopped."""
        state = EvidenceState.PARTIAL if self.pages else EvidenceState.UNAVAILABLE
        return Classification(state, Basis.DEADLINE, _DEADLINE)

    def _request(self) -> Awaitable[EvidencePage]:
        return self.reader.list_evidence_page(
            self.target,
            limit=self.page_size,
            continuation=self.continuation,
            max_bytes=self.max_bytes,
        )


def _deadline_at(deadline: float) -> float:
    return asyncio.get_running_loop().time() + deadline


async def _call(attempts: _Attempts, request: Callable[[], Awaitable[_T]]) -> _T:
    """Make `request` before the deadline, retrying one 429 whose wait fits in it."""
    while True:
        try:
            return await _before_deadline(attempts.deadline_at, request())
        except ApiStatusError as failure:
            delay = attempts.retry_delay(failure)
            if delay is None:
                raise
            attempts.retried = True
            await _pause(delay)
            # The wait can end later than asked: never send the retry late.
            _refuse_late(attempts.deadline_at)


async def _before_deadline(deadline_at: float, request: Awaitable[_T]) -> _T:
    timeout = asyncio.timeout_at(deadline_at)
    try:
        async with timeout:
            return await request
    except TimeoutError:
        if timeout.expired():
            raise _DeadlineReachedError from None
        raise
    except (ApiStatusError, KubeClientError):
        # A failure reported after the deadline proves no more than a late
        # success would: a late `NotFound` must not establish absence.
        _refuse_late(deadline_at)
        raise


def _refuse_late(deadline_at: float) -> None:
    if asyncio.get_running_loop().time() >= deadline_at:
        raise _DeadlineReachedError


def _target(source: ApiSource, verb: Verb) -> EvidenceTarget:
    """Check that `source` may be read with `verb`, and address it."""
    if source.version is None or source.verb is not verb:
        raise ValueError(f"this read needs a source discovery resolved for {verb}")
    if source.subresource:
        raise ValueError("evidence reads do not read subresources")
    metadata_only = source.projection is Projection.METADATA
    if source.resource in PAYLOAD_RESOURCES and not metadata_only:
        raise ValueError(f"{source.resource} are read as metadata only, never as objects")
    namespace = _namespace(source)
    return EvidenceTarget(source.group, source.version, source.resource, namespace, metadata_only)


def _namespace(source: ApiSource) -> str | None:
    if source.scope.kind is ScopeKind.CLUSTER:
        return None
    if source.scope.kind is ScopeKind.NAMESPACE:
        return source.scope.name
    raise ValueError("an API read is cluster- or namespace-scoped")


def _check_count(name: str, value: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} is a whole number from 1 to {maximum}")


def _check_budgets(deadline: float, max_bytes: int) -> None:
    _check_count("max_bytes", max_bytes, MAX_BYTES)
    is_number = isinstance(deadline, int | float) and not isinstance(deadline, bool)
    if not is_number or not 0 < deadline <= MAX_DEADLINE:
        raise ValueError(f"deadline is more than 0 and at most {MAX_DEADLINE:g} seconds")


def _reduce_item(source: ApiSource, item: Mapping[str, Any]) -> dict[str, Any]:
    """What a diagnostic may receive of `item`: only reduced metadata, for a metadata read."""
    metadata = item.get("metadata")
    if not isinstance(metadata, Mapping):
        raise KubeMalformedError("Kubernetes API returned an object without metadata")
    scope = source.scope
    if scope.kind is ScopeKind.NAMESPACE and metadata.get("namespace") != scope.name:
        raise KubeMalformedError("Kubernetes API returned an object from another namespace")
    reduced = _reduce_metadata(source.resource, metadata)
    if source.projection is Projection.METADATA:
        return {"metadata": reduced}
    try:
        scrubbed = _scrub(item)
    except RecursionError:
        raise KubeMalformedError("Kubernetes API returned an object nested too deeply") from None
    return {**scrubbed, "metadata": reduced}


def _scrub(value: Any) -> Any:
    """`value` with every metadata it embeds reduced, as any resource's is."""
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    scrubbed = {key: _scrub(item) for key, item in value.items()}
    metadata = scrubbed.get("metadata")
    if isinstance(metadata, Mapping):
        scrubbed["metadata"] = _reduce_metadata("", metadata)
    return scrubbed


def _reduce_metadata(resource: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    if resource in PAYLOAD_RESOURCES:
        return {key: metadata[key] for key in PAYLOAD_METADATA_FIELDS if key in metadata}
    reduced = {key: value for key, value in metadata.items() if key != "managedFields"}
    annotations = reduced.pop("annotations", None)
    if isinstance(annotations, Mapping):
        kept = {key: value for key, value in annotations.items() if key != LAST_APPLIED_ANNOTATION}
        if kept:
            reduced["annotations"] = kept
    return reduced


def _named_object(source: ApiSource, document: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """The reduced object and its UID, if it is the object `source` names."""
    item = _reduce_item(source, document)
    metadata = item["metadata"]
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise KubeMalformedError("Kubernetes API returned an object without a UID")
    if metadata.get("name") != source.name:
        raise KubeMalformedError("Kubernetes API returned another object")
    return item, uid


def _observe(
    source: ApiSource,
    outcome: Classification,
    at: datetime,
    items: int,
    token: RunToken | None,
    observed_uid: str | None = None,
) -> Observation:
    return Observation(
        source,
        outcome.state,
        outcome.basis,
        at,
        items=items,
        observed_uid=observed_uid,
        detail=outcome.detail,
        token=token,
    )
```

In `tests/test_optional_extras.py`, add `"korvid.core.evidence_reads"`, `"korvid.k8s.bounded_transport"` and `"korvid.k8s.evidence_requests"` after `"korvid.k8s.discovery_snapshot"` in the parametrize list of `test_base_import_does_not_require_optional_extras`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/core/test_evidence_reads.py tests/core/test_capability_classifier.py "tests/test_optional_extras.py::test_base_import_does_not_require_optional_extras" -q`
Expected: all pass.

Run: `uv run --frozen ruff check src/korvid/core tests/core/test_evidence_reads.py && uv run --frozen mypy src/korvid/core tests/core/test_evidence_reads.py && uv run --frozen tach check`
Expected: no findings, and `All modules validated!`.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/core/evidence_reads.py src/korvid/core/capability_classifier.py tests/core/test_evidence_reads.py tests/test_optional_extras.py
git commit -m "core: bounded, metadata-safe evidence reads"
```

### Task 4: Run the full gate

- [ ] **Step 1: Run every check that `make check` runs**

```bash
uv run --frozen python scripts/check_source_size.py
uv run --frozen ruff check src/ tests/
uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy
uv run --frozen tach check
uv run --frozen pytest -x -q
```

Expected: every command exits 0. Plain `mypy` checks `tests/` as well as `src/`.

- [ ] **Step 2: Check #487's acceptance criteria against tests**

| Criterion | Tests |
| --- | --- |
| Empty pages with a continue token pending | `test_the_page_cap_with_a_token_pending_is_truncated[empty-pages]`, `test_a_failure_after_an_accepted_page_keeps_it[*-empty-page]` |
| Oversized responses and gzip expansion | `test_a_bad_body_is_categorized_and_closed[byte-cap]` and `[gzip-expansion]`, `test_a_page_with_more_items_than_requested_is_over_budget`, the `too-large` cases of the read tests |
| A 429 retried once | `test_a_429_is_retried_once_within_the_deadline`, `test_a_second_429_is_throttled`, `test_a_429_whose_wait_outlasts_the_deadline_is_not_retried`, `test_a_read_retries_only_one_429` |
| An expired continuation | `test_a_failure_after_an_accepted_page_keeps_it[*-expired]` |
| Cancellation | `test_cancelling_a_read_cancels_it`, `test_a_cancelled_request_is_never_classified` |
| UID mismatch | `test_a_recreated_object_is_never_conclusive` |
| No ConfigMap or Secret data, and no `last-applied-configuration`, reaches a diagnostic | `test_no_configuration_payload_reaches_a_diagnostic_through_a_list`, `..._through_a_get`, `test_a_configuration_payload_is_never_read_as_an_object`, `test_no_metadata_keeps_the_last_applied_configuration_or_managed_fields` |
| Resolved sources and verbs only; a reader that can only read | `test_an_unresolved_source_is_never_read`, `test_each_read_refuses_a_source_resolved_for_the_other_verb`, `test_the_evidence_reader_can_only_read` |
| Bounded-transport error categories, Pulse's included | `test_a_bad_body_is_categorized_and_closed`, `test_a_failed_stream_is_categorized_without_its_message`, `test_implicit_http_attempts_cannot_exceed_refresh_budget` |
