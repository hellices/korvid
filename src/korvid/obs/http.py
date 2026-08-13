"""The HTTP half of the connector boundary (issue #193).

One place decides how a backend's answer is bounded and how its failures
are classified, so Prometheus and Loki cannot drift apart on either.

The client is *injected*, never constructed here. That is the point: the
composition root builds it from `network.ca_bundle` with the same builder
the agent providers use, so this module has no way to express "do not
verify" even by mistake.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from korvid.obs.connector import ConnectorError, QueryLimits
from korvid.obs.credentials import resolve_token


def endpoint_host(url: str) -> str:
    """The host of `url`, for messages that must not leak a credential.

    A userinfo component (`https://user:pass@host`) is dropped with the
    rest of the authority, so a URL someone pasted with a password in it
    cannot travel into a tool result.
    """
    host = urlsplit(url).hostname
    return host or url


class HttpBackend:
    """Shared transport for a bounded read-only observability backend."""

    def __init__(
        self,
        url: str,
        *,
        source: str,
        client: httpx.AsyncClient,
        limits: QueryLimits,
        token_env: str | None = None,
        token_file: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.source = source
        self.url = url.rstrip("/")
        self.endpoint = endpoint_host(url)
        self.limits = limits
        self._client = client
        self._token_env = token_env
        self._token_file = token_file
        self._extra_headers = dict(headers or {})
        self._gate = asyncio.Semaphore(limits.max_concurrency)

    @property
    def max_concurrency(self) -> int:
        """The number of requests this connector will have in flight."""
        return self.limits.max_concurrency

    async def aclose(self) -> None:
        """Close the injected client."""
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", **self._extra_headers}
        token = resolve_token(
            token_env=self._token_env, token_file=self._token_file, source=self.source
        )
        if token:
            headers["authorization"] = f"Bearer {token}"
        return headers

    async def get_json(self, path: str, params: Mapping[str, str]) -> Any:
        """GET `path`, bounded by the configured timeout and byte cap.

        Raises:
            ConnectorError: `config` for an unusable credential, `auth`,
                `permission`, `timeout`, `network`, `limit` for an
                oversized body, or `backend` for anything the backend
                itself refused or malformed.
        """
        headers = self._headers()
        async with self._gate:
            body = await self._read(path, params, headers)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ConnectorError(
                "backend", f"{self.endpoint} returned a body that is not JSON: {exc}"
            ) from exc

    async def _read(self, path: str, params: Mapping[str, str], headers: Mapping[str, str]) -> str:
        cap = self.limits.max_response_bytes
        try:
            async with self._client.stream(
                "GET",
                f"{self.url}{path}",
                params=dict(params),
                headers=dict(headers),
                timeout=self.limits.timeout_seconds,
            ) as response:
                self._raise_for_status(response)
                self._raise_for_declared_size(response, cap)
                return await self._collect(response, cap)
        except httpx.TimeoutException as exc:
            raise ConnectorError(
                "timeout",
                f"{self.endpoint} did not answer within"
                f" {self.limits.timeout_seconds:g}s — raise the timeout or narrow the window",
            ) from exc
        except httpx.HTTPError as exc:
            raise ConnectorError("network", f"{self.endpoint} is unreachable: {exc}") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        if status == 401:
            raise ConnectorError(
                "auth",
                f"{self.endpoint} rejected the credential (HTTP 401) —"
                f" check the configured token source",
            )
        if status == 403:
            raise ConnectorError(
                "permission",
                f"{self.endpoint} refused the query (HTTP 403) —"
                f" the credential lacks permission for this scope",
            )
        if status == 404:
            raise ConnectorError(
                "config",
                f"{self.endpoint} has no such API path (HTTP 404) — check the configured url",
            )
        raise ConnectorError("backend", f"{self.endpoint} returned HTTP {status}")

    def _raise_for_declared_size(self, response: httpx.Response, cap: int) -> None:
        declared = response.headers.get("content-length")
        if declared is None:
            return
        try:
            size = int(declared)
        except ValueError:
            return
        if size > cap:
            raise ConnectorError(
                "limit",
                f"{self.endpoint} answered with {size} bytes, over the"
                f" {cap}-byte cap — narrow the window or lower the result limit",
            )

    async def _collect(self, response: httpx.Response, cap: int) -> str:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > cap:
                raise ConnectorError(
                    "limit",
                    f"{self.endpoint} answered with more than the {cap}-byte"
                    f" cap — narrow the window or lower the result limit",
                )
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    def require_success(self, payload: Any) -> Mapping[str, Any]:
        """The `data` block of a Prometheus-style envelope.

        Both backends speak the same envelope, so both get the same
        refusal when the backend reports a query-level failure.

        Raises:
            ConnectorError: `backend` for a non-mapping payload or a
                reported error.
        """
        if not isinstance(payload, Mapping):
            raise ConnectorError("backend", f"{self.endpoint} returned an unexpected payload")
        if payload.get("status") != "success":
            reason = payload.get("error") or payload.get("status") or "unknown error"
            raise ConnectorError("backend", f"{self.endpoint} refused the query: {reason}")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ConnectorError("backend", f"{self.endpoint} returned no result data")
        return data
