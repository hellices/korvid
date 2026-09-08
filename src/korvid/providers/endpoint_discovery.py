"""Live endpoint model discovery — best-effort OpenAI/Ollama model listing.

Tries `GET {base}/v1/models` (OpenAI-compat) then `GET {base}/api/tags`
(Ollama-native), takes the first that parses, and returns `()` on any
failure. One 5 s deadline covers both attempts, their reads and the
parse; a 2 MiB ceiling, `application/json` only, redirects refused, at
most 500 entries kept.

Two invariants this module exists to hold:

- **The credential goes where the operator sent it, and nowhere else.**
  Both attempts are built on the endpoint's own origin, no redirect is
  ever followed, and failures are logged with the key scrubbed out — it
  is borrowed for the call and never stored.
- **Trust is `network.ca_bundle`, through the same `net.make_client` the
  live providers use.** There is no insecure mode: an unreadable bundle
  or an untrusted certificate is an empty listing, never a retry without
  verification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Callable
from functools import partial
from typing import Any, Final

import httpx

from korvid.agent.model_profiles import ModelEntry, ModelEntrySource
from korvid.providers import net

logger = logging.getLogger(__name__)

_MAX_BYTES: int = 2 * 1024 * 1024  # 2 MiB

#: Whole-operation budget: both attempts, every read and the parse
#: together, not per socket operation. Nobody waits on the setup screen
#: for a listing they can type by hand.
_TIMEOUT_SECONDS: float = 5.0

_MAX_ENTRIES: int = 500

#: What a scrubbed credential looks like in a debug log.
_REDACTED: Final[str] = "***"

#: An endpoint korvid will build a request against. Anything else is not
#: an endpoint, and is refused before a client exists to send a key with.
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

_OPENAI_PATH: Final[str] = "/v1/models"
_OLLAMA_PATH: Final[str] = "/api/tags"


def _default_client_factory(ca_bundle: str | None = None) -> httpx.AsyncClient:
    """The production client: korvid's own trust, nothing else.

    Built through `net.make_client`, so `network.ca_bundle` reaches
    discovery exactly as it reaches the live providers, the wizard's probe
    and the models.dev refresh — a deployment behind a TLS-inspecting
    proxy either trusts all of them or none of them. There is no insecure
    mode to fall back to: an unreadable or malformed bundle raises, and
    the listing comes back empty rather than being retried unverified.
    """
    return net.make_client(ca_bundle, timeout=_TIMEOUT_SECONDS)


def _redacted(text: str, secret: str | None) -> str:
    """*text* with the operator's credential replaced, if it appears."""
    if not secret:
        return text
    return text.replace(secret, _REDACTED)


def _log_failure(what: str, exc: BaseException, secret: str | None) -> None:
    """Report a failure at debug level, with the credential scrubbed out.

    The traceback is formatted here rather than handed to `exc_info=`
    because only text korvid controls can be scrubbed: a proxy that
    quotes the request headers back in its error message would otherwise
    write the operator's key into a debug log.
    """
    detail = "".join(traceback.format_exception(exc))
    logger.debug("%s: %s", _redacted(what, secret), _redacted(detail, secret))


def _endpoint_base(base_url: str) -> httpx.URL | None:
    """The operator's endpoint as a URL korvid will send a key to, or None.

    Returns None for anything that does not name an `http(s)` origin, so
    an unusable endpoint stops discovery before a client is built rather
    than surfacing as an exception from inside one.
    """
    try:
        url = httpx.URL(base_url.strip())
    except httpx.InvalidURL:
        return None
    if url.scheme not in _ALLOWED_SCHEMES or not url.host:
        return None
    return url


class EndpointDiscovery:
    """Best-effort model listing from an operator-supplied endpoint.

    Tries `GET {base}/v1/models` (OpenAI-compatible) then `GET {base}/api/tags`
    (Ollama-native), takes the first that parses, and returns `()` on any
    failure. One 5 s deadline covers both attempts, their reads and the
    parse; a 2 MiB ceiling, `application/json` only, redirects refused, at
    most 500 entries kept.

    Args:
        ca_bundle: `network.ca_bundle`. One trust decision for every
            korvid-owned HTTPS client; setup discovery is no exception —
            an endpoint the running agent can reach must be an endpoint
            the setup screen can list. The bundle is held, not opened:
            the client is built when a listing is requested, so wiring
            reads nothing off disk and a misconfigured path cannot keep
            the TUI from starting.
        client_factory: Callable that returns an `httpx.AsyncClient`. The
            test seam, and it wins outright — trust is then that
            factory's business.
    """

    def __init__(
        self,
        *,
        ca_bundle: str | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        # Bound, not called: no socket is opened and no bundle is read
        # until someone asks for a listing.
        self._client_factory = client_factory or partial(_default_client_factory, ca_bundle)

    async def list_models(
        self, *, base_url: str, api_key: str | None, prefix: str
    ) -> tuple[ModelEntry, ...]:
        """List models from the operator's endpoint.

        The credential, when there is one, is sent as a bearer token to
        this endpoint only. It is not stored, not logged, and not carried
        anywhere else: no redirect is followed and both attempts are built
        on the endpoint's own origin.

        Returns:
            A tuple of `ModelEntry` objects, or `()` on any failure.
        """
        base = _endpoint_base(base_url)
        if base is None:
            logger.debug("endpoint discovery skipped: %r names no http(s) endpoint", base_url)
            return ()

        try:
            # One deadline over both attempts, their reads and the parse.
            # An HTTP client's own timeout is spent *per operation*, so a
            # server that answers every individual read inside the limit —
            # a slow drip, a stalled proxy — could otherwise hold the
            # setup screen open indefinitely, and do it twice over because
            # each attempt would start the clock again.
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                return await self._discover(base=base, api_key=api_key, prefix=prefix)
        except TimeoutError:
            logger.debug("endpoint discovery exceeded its %ss budget", _TIMEOUT_SECONDS)
        except Exception as exc:  # network errors must never surface to UI
            _log_failure("endpoint discovery failed", exc, api_key)
        return ()

    async def _discover(
        self, *, base: httpx.URL, api_key: str | None, prefix: str
    ) -> tuple[ModelEntry, ...]:
        """Try each shape in turn, under the caller's deadline."""
        headers: dict[str, str] = {"accept": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"

        attempts = (
            (_OPENAI_PATH, self._parse_openai),
            (_OLLAMA_PATH, self._parse_ollama),
        )
        async with self._client_factory() as client:
            for path, parse in attempts:
                # `join` keeps the endpoint's scheme, host and port: the
                # fallback cannot wander off the origin the operator named.
                payload = await self._fetch(client, base.join(path), headers, secret=api_key)
                if payload is not None:
                    return parse(payload, prefix)
        return ()

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        url: httpx.URL,
        headers: dict[str, str],
        *,
        secret: str | None,
    ) -> Any | None:
        """Fetch *url* and return the parsed JSON body, or None on any failure.

        Args:
            client: The client to send on.
            url: An absolute URL on the operator's own endpoint.
            headers: Request headers, credential included.
            secret: The credential, for scrubbing it back out of any
                failure this logs. Never used to build the request.
        """
        try:
            async with client.stream(
                "GET",
                url,
                headers=headers,
                # A per-operation ceiling under the whole-operation
                # deadline: it fails a single stalled socket call fast,
                # but only the outer `asyncio.timeout` bounds the listing.
                timeout=_TIMEOUT_SECONDS,
                # Written on the request, not left to the client: a 3xx is
                # a failed attempt, never an instruction to take the
                # operator's credential to some other host.
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    return None

                media_type = response.headers.get("content-type", "").split(";")[0].strip()
                if media_type != "application/json":
                    return None

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        return None
                    chunks.append(chunk)

                body = b"".join(chunks)
                return json.loads(body)
        except Exception as exc:  # connection refused, TLS refusal, bad JSON
            # `%r` on an httpx URL masks a password the operator may have
            # written into the endpoint itself.
            _log_failure(f"endpoint discovery attempt failed for {url!r}", exc, secret)
            return None

    def _parse_openai(self, payload: Any, prefix: str) -> tuple[ModelEntry, ...]:
        """Parse an OpenAI-compat `{"data": [...]}` response."""
        if not isinstance(payload, dict):
            return ()
        data = payload.get("data")
        if not isinstance(data, list):
            return ()
        entries: list[ModelEntry] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            reference = f"{prefix}/{model_id}"
            entries.append(
                ModelEntry(
                    reference=reference,
                    provider_id=prefix,
                    display_name=model_id,
                    source=ModelEntrySource.ENDPOINT,
                )
            )
            if len(entries) >= _MAX_ENTRIES:
                break
        return tuple(entries)

    def _parse_ollama(self, payload: Any, prefix: str) -> tuple[ModelEntry, ...]:
        """Parse an Ollama-native `{"models": [...]}` response."""
        if not isinstance(payload, dict):
            return ()
        models = payload.get("models")
        if not isinstance(models, list):
            return ()
        entries: list[ModelEntry] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            reference = f"{prefix}/{name}"
            entries.append(
                ModelEntry(
                    reference=reference,
                    provider_id=prefix,
                    display_name=name,
                    source=ModelEntrySource.ENDPOINT,
                )
            )
            if len(entries) >= _MAX_ENTRIES:
                break
        return tuple(entries)
