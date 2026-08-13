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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from korvid.obs.connector import ConnectorError, QueryLimits, mask_in
from korvid.obs.credentials import resolve_token_async


def endpoint_host(url: str) -> str:
    """The host of `url`, for messages that must not leak a credential.

    A userinfo component (`https://user:pass@host`) is dropped with the
    rest of the authority, so a URL someone pasted with a password in it
    cannot travel into a tool result. The fallback for an unparsable URL
    drops anything before an `@` for the same reason - config rejects
    hostname-less URLs, so this is belt to that braces.
    """
    host = urlsplit(url).hostname
    return host or url.rsplit("@", 1)[-1]


@dataclass(frozen=True, slots=True)
class Answer:
    """One parsed backend answer, plus what must not survive it.

    The payload is deliberately **unscrubbed**: a secret is content, and
    the envelope is structure. A one-character bearer token is legal, and
    replacing it across the whole body rewrote `"status": "success"` into
    nonsense, turning every successful request into a backend failure
    (round-7 review). Structural fields are therefore read exactly as the
    backend sent them, and scrubbing is applied where content becomes
    text: label names and values, log lines, and the backend's own error
    message.
    """

    payload: Any
    secrets: tuple[str, ...] = ()

    def scrub(self, text: str) -> str:
        """`text` with every secret replaced."""
        return mask_in(text, self.secrets)

    def scrub_labels(self, labels: Mapping[str, str]) -> dict[str, str]:
        """`labels` with every secret replaced, in names and values alike."""
        if not self.secrets:
            return dict(labels)
        return {self.scrub(key): self.scrub(value) for key, value in labels.items()}


def _require_origin(url: str, source: str) -> None:
    """Refuse a base URL that is not a usable origin.

    Everything a URL can be wrong about is refused here as one actionable
    `config` error rather than surfacing later as a `network` failure or a
    raw `ValueError`. In particular the API path is appended to this, so
    `https://host/base?x=1` puts `/api/v1/query` inside the query string
    and every request quietly targets the wrong endpoint — the worst kind
    of failure, because such a request can still answer (round-8 review).

    Raises:
        ConnectorError: `config` for an unparsable URL, a scheme other
            than http(s), a missing host, an unusable port, or a query
            string, fragment or path parameters.
    """
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ConnectorError("config", f"{source}: the configured url is unusable: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ConnectorError("config", f"{source}: the configured url must use http:// or https://")
    if not host:
        raise ConnectorError("config", f"{source}: the configured url names no host")
    if port is not None and not 0 < port < 65536:
        raise ConnectorError("config", f"{source}: the configured url has an unusable port")
    if parsed.query or parsed.fragment or "?" in url or "#" in url:
        raise ConnectorError(
            "config",
            f"{source}: the configured url must be an origin with an optional base path,"
            f" not a query string or fragment",
        )


def _userinfo(url: str) -> str:
    """The `user:pass` part of `url`, or "" when there is none."""
    authority = urlsplit(url).netloc
    return authority.rsplit("@", 1)[0] if "@" in authority else ""


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
        _require_origin(url, source)
        if _userinfo(url):
            # httpx turns `https://user:pw@host` into a Basic
            # `Authorization` header, so this is an inline credential
            # wearing a URL's clothes. Config rejects it; refused here too
            # because a connector can be built directly, and the message
            # names the supported settings rather than the value.
            raise ConnectorError(
                "config",
                f"{source}: the configured url must not carry a username or password"
                f" — use `token_env` (environment variable name) or `token_file` (path)",
            )
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

    async def _headers(self) -> tuple[dict[str, str], str | None]:
        headers = {"accept": "application/json", **self._extra_headers}
        token = await resolve_token_async(
            token_env=self._token_env, token_file=self._token_file, source=self.source
        )
        if token:
            headers["authorization"] = f"Bearer {token}"
        return headers, token

    async def get_json(
        self, path: str, params: Mapping[str, str], *, secrets: Sequence[str] = ()
    ) -> Answer:
        """GET `path`, bounded by the configured timeout and byte cap.

        Args:
            path: API path appended to the configured base URL.
            params: Query parameters.
            secrets: Values the caller has declared sensitive (a masked
                scope value and its escaped form). They are scrubbed out
                of the response body and out of every message this call
                can raise, together with the bearer token.

        Returns:
            The parsed body and the secrets to keep out of anything
            derived from it. The payload itself is not rewritten — see
            `Answer`.

        Raises:
            ConnectorError: `config` for an unusable credential, `auth`,
                `permission`, `timeout`, `network`, `limit` for an
                oversized body, or `backend` for anything the backend
                itself refused or malformed.
        """
        # One budget for the whole call. httpx's timeout starts when the
        # request does, so it bounds neither the wait for a concurrency
        # slot nor the total elapsed time of a response that trickles in
        # just fast enough never to look idle (round-1 review).
        scrub: tuple[str, ...] = tuple(secrets)
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                headers, token = await self._headers()
                # The token is held only for this call: a backend that
                # echoes an opaque token in an error, a label or a log line
                # defeats pattern redaction, and only an exact match can
                # find it (round-5 review).
                scrub = (*scrub, token) if token else scrub
                async with self._gate:
                    body = await self._read(path, params, headers, scrub)
        except TimeoutError as exc:
            raise ConnectorError(
                "timeout",
                f"{self.endpoint} did not answer within"
                f" {self.limits.timeout_seconds:g}s (including time spent waiting for a"
                f" free request slot) — raise the timeout or narrow the window",
            ) from exc
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            # The exception quotes the body, so *this* text is scrubbed:
            # it is a message, not a structure, and nothing downstream
            # parses it.
            raise ConnectorError(
                "backend",
                f"{self.endpoint} returned a body that is not JSON: {mask_in(str(exc), scrub)}",
            ) from exc
        return Answer(payload=parsed, secrets=scrub)

    async def _read(
        self,
        path: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        secrets: Sequence[str] = (),
    ) -> str:
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
            raise ConnectorError(
                "network",
                f"{self.endpoint} is unreachable: {mask_in(str(exc), secrets)}",
            ) from exc

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

    def require_success(self, answer: Answer) -> Mapping[str, Any]:
        """The `data` block of a Prometheus-style envelope.

        Both backends speak the same envelope, so both get the same
        refusal when the backend reports a query-level failure.

        Raises:
            ConnectorError: `backend` for a non-mapping payload or a
                reported error.
        """
        payload = answer.payload
        if not isinstance(payload, Mapping):
            raise ConnectorError("backend", f"{self.endpoint} returned an unexpected payload")
        if payload.get("status") != "success":
            reason = payload.get("error") or payload.get("status") or "unknown error"
            # The backend's own words, so they are scrubbed before they
            # travel — the *decision* above read the field untouched.
            raise ConnectorError(
                "backend", f"{self.endpoint} refused the query: {answer.scrub(str(reason))}"
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ConnectorError("backend", f"{self.endpoint} returned no result data")
        return data

    def require_result_type(self, data: Mapping[str, Any], expected: str) -> None:
        """Refuse a successful answer of the wrong shape.

        A `matrix` or `scalar` also has a list-shaped `result`, so without
        this a wrong endpoint (or an API change) renders as "nothing
        matched" — a wrong answer presented as a valid one, which is worse
        than an error (PR #280 review).

        Raises:
            ConnectorError: `backend` when the type is not `expected`.
        """
        actual = data.get("resultType")
        if actual != expected:
            raise ConnectorError(
                "backend",
                f"{self.endpoint} answered with resultType {actual!r},"
                f" expected {expected!r} — check the configured url",
            )
