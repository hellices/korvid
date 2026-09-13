"""Bounded, read-only snapshot inputs for the attention surface."""

from __future__ import annotations

import json
import zlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote, urlencode

from kubernetes_asyncio.client import ApiClient, ApiException, rest

from korvid.k8s.errors import ApiStatusError, KubeClientError

_HTTP = cast(Any, rest).aiohttp
_CLIENT_ERROR = _HTTP.ClientError
_READ_BUFFER_BYTES = 16384


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


class PulseLimitError(KubeClientError):
    """A response exceeded an explicit collection budget."""


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


def path_segment(value: str) -> str:
    """Encode one URL segment, rejecting empty and traversal segments."""
    if value in ("", ".", ".."):
        raise ValueError(f"invalid URL path segment: {value!r}")
    return quote(value, safe="")


def _source_path(source: PulseSource, namespace: str | None) -> str:
    prefix = (
        f"/apis/{path_segment(source.group)}/{path_segment(source.version)}"
        if source.group
        else f"/api/{path_segment(source.version)}"
    )
    scope = f"/namespaces/{path_segment(namespace)}" if namespace is not None else ""
    return f"{prefix}{scope}/{path_segment(source.plural)}"


async def _read_body(response: Any, max_bytes: int) -> bytes:
    body = bytearray()
    while len(body) < max_bytes:
        chunk = await response.content.read(min(_READ_BUFFER_BYTES, max_bytes - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
    if not response.content.at_eof():
        raise PulseLimitError("Pulse response byte cap reached")
    return bytes(body)


def _decode_body(body: bytes, encoding: str, max_bytes: int) -> bytes:
    if encoding in ("", "identity"):
        return body
    if encoding != "gzip":
        raise KubeClientError("Pulse returned an unsupported content encoding")
    decompressor = zlib.decompressobj(wbits=31)
    try:
        decoded = decompressor.decompress(body, max_length=max_bytes)
    except zlib.error:
        raise KubeClientError("Pulse returned invalid compressed data") from None
    if not decompressor.eof and len(decoded) == max_bytes:
        raise PulseLimitError("Pulse decoded response byte cap reached")
    if not decompressor.eof or decompressor.unused_data:
        raise KubeClientError("Pulse returned incomplete or trailing compressed data")
    return decoded


def _bounded_session(shared: Any) -> Any:
    """Borrow the connector and reserve one API send before headers reach the wire."""
    if shared.closed or shared.connector is None:
        raise KubeClientError("Pulse API transport is closed")
    sent = False

    async def check_budget(_session: Any, _context: Any, _parameters: Any) -> None:
        if sent:
            raise PulseLimitError("Pulse API request attempt cap reached")

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
        read_bufsize=_READ_BUFFER_BYTES,
    )


@asynccontextmanager
async def _request_page(
    api: ApiClient, path: str, query: list[tuple[str, str]]
) -> AsyncIterator[Any]:
    host = api.configuration.host
    if not host:
        raise KubeClientError("Pulse API host is not configured")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(api.default_headers)
    if api.cookie:
        headers["Cookie"] = api.cookie
    headers["Accept-Encoding"] = "identity"
    await api.update_params_for_auth(headers, query, ["BearerToken"])
    transport = api.rest_client
    options: dict[str, Any] = {
        "method": "GET",
        "url": host + path + "?" + urlencode(query),
        "headers": headers,
        "auto_decompress": False,
        "allow_redirects": False,
        "read_bufsize": _READ_BUFFER_BYTES,
        "timeout": _HTTP.ClientTimeout(),
    }
    for name in ("proxy", "proxy_headers", "server_hostname"):
        value = getattr(transport, name, None)
        if value:
            options[name] = value
    async with _bounded_session(transport.pool_manager) as session:
        response = await session.request(**options)
        try:
            yield response
        finally:
            response.close()


def _decode_page(body: bytes, limit: int) -> PulsePage:
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise KubeClientError("Pulse returned invalid JSON") from None
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise KubeClientError("Pulse returned an invalid resource list")
    items = document["items"]
    if len(items) > limit:
        raise PulseLimitError("Pulse response object cap reached")
    if any(not isinstance(item, dict) for item in items):
        raise KubeClientError("Pulse returned an invalid list item")
    metadata = document.get("metadata", {})
    if not isinstance(metadata, dict) or not isinstance(metadata.get("continue", ""), str):
        raise KubeClientError("Pulse returned invalid pagination metadata")
    return PulsePage(tuple(items), metadata.get("continue", ""))


async def read_pulse_page(
    api: ApiClient | None,
    source: PulseSource,
    namespace: str | None,
    continuation: str,
    limit: int,
    max_bytes: int,
) -> PulsePage:
    """Stream at most the decoded-byte budget before parsing a LIST response."""
    for budget in (limit, max_bytes):
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ValueError("Pulse budgets must be a positive integer")
    if api is None:
        raise KubeClientError("Pulse API is not connected")
    query = [("limit", str(limit))]
    if continuation:
        query.append(("continue", continuation))
    if source.field_selector:
        query.append(("fieldSelector", source.field_selector))
    try:
        async with _request_page(api, _source_path(source, namespace), query) as response:
            if not 200 <= response.status < 300:
                raise ApiStatusError(response.status, "Pulse snapshot request failed")
            encoding = response.headers.get("Content-Encoding", "").strip().lower()
            body = _decode_body(await _read_body(response, max_bytes), encoding, max_bytes)
            return _decode_page(body, limit)
    except ApiException as failure:
        raise ApiStatusError(failure.status or 0, "Pulse snapshot request failed") from None
    except (OSError, _CLIENT_ERROR):
        raise KubeClientError("Pulse snapshot transport failed") from None
