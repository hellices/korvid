"""Live endpoint model discovery — best-effort OpenAI/Ollama model listing.

Tries `GET {base}/v1/models` (OpenAI-compat) then `GET {base}/api/tags`
(Ollama-native), takes the first that parses, and returns `()` on any
failure. Bounded by a 5 s timeout and a 2 MiB ceiling, `application/json`
only, redirects refused, at most 500 entries kept.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx

from korvid.agent.model_profiles import ModelEntry, ModelEntrySource

logger = logging.getLogger(__name__)

_MAX_BYTES: int = 2 * 1024 * 1024  # 2 MiB
_TIMEOUT_SECONDS: float = 5.0
_MAX_ENTRIES: int = 500


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient()


class EndpointDiscovery:
    """Best-effort model listing from an operator-supplied endpoint.

    Tries `GET {base}/v1/models` (OpenAI-compatible) then `GET {base}/api/tags`
    (Ollama-native), takes the first that parses, and returns `()` on any
    failure. Bounded by a 5 s timeout and a 2 MiB ceiling, `application/json`
    only, redirects refused, at most 500 entries kept.

    Args:
        client_factory: Callable that returns an `httpx.AsyncClient`. Injected
            so tests can substitute a mock transport without network access.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._client_factory = client_factory or _default_client_factory

    async def list_models(
        self, *, base_url: str, api_key: str | None, prefix: str
    ) -> tuple[ModelEntry, ...]:
        """List models from the operator's endpoint.

        Returns:
            A tuple of `ModelEntry` objects, or `()` on any failure.
        """
        headers: dict[str, str] = {"accept": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"

        base = httpx.URL(base_url)

        try:
            async with self._client_factory() as client:
                # Try OpenAI-compat endpoint first
                openai_url = str(base.join("/v1/models"))
                payload = await self._fetch(client, openai_url, headers)
                if payload is not None:
                    return self._parse_openai(payload, prefix)

                # Fall through to Ollama-native endpoint
                ollama_url = str(base.join("/api/tags"))
                payload = await self._fetch(client, ollama_url, headers)
                if payload is not None:
                    return self._parse_ollama(payload, prefix)
        except Exception:  # network errors must never surface to UI
            logger.debug("endpoint discovery failed", exc_info=True)

        return ()

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> Any | None:
        """Fetch *url* and return the parsed JSON body, or None on any failure."""
        try:
            async with client.stream(
                "GET",
                url,
                headers=headers,
                timeout=_TIMEOUT_SECONDS,
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
        except Exception:  # connection refused, timeout, etc.
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
