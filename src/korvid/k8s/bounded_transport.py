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
