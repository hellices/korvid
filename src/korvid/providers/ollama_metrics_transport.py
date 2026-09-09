"""Per-request capture of native terminal metrics before LiteLLM drops them.

LiteLLM's Ollama transformations receive decoded raw JSON and then retain only
token counts. This module observes the same request-local decoded mapping the
transformation consumes: a per-response `json()` wrapper for non-streaming
calls, and the response iterator's per-instance `chunk_parser` seam for
streaming calls. It creates no second raw buffer or decoded frame and keeps
only the provider-neutral numeric event produced by `decode_ollama_metrics`.

The wrapper is request-local. It installs no callback or process-global hook,
and the raw response continues through LiteLLM's normal transformation,
logging lockdown, retry, masking, and policy path unchanged.
"""

from __future__ import annotations

import weakref
from collections.abc import Mapping
from typing import Any, cast

import httpx

from korvid.providers import litellm_runtime
from korvid.providers.ollama_metrics import decode_ollama_metrics


class _MetricsCapture:
    """Inspect decoded terminal JSON while retaining only metrics."""

    def __init__(self) -> None:
        self._event: dict[str, Any] | None = None

    def observe_mapping(self, raw: object) -> None:
        """Inspect one mapping decoded by LiteLLM's stream iterator."""
        if not isinstance(raw, Mapping) or raw.get("done") is not True:
            return
        event = decode_ollama_metrics(raw)
        if event is not None:
            self._event = event

    def event(self) -> dict[str, Any] | None:
        """Return an independent copy of the retained neutral event."""
        return dict(self._event) if self._event is not None else None


def _http_handler_base() -> type[Any]:
    """Resolve the HTTP handler class re-exported by the pinned LiteLLM SDK."""
    sdk = getattr(litellm_runtime, "_litellm", None)
    factory = getattr(sdk, "AsyncHTTPHandler", None)
    if not isinstance(factory, type):
        raise RuntimeError("the supported LiteLLM HTTP handler is unavailable")
    return factory


_AsyncHTTPHandler = _http_handler_base()


class OllamaMetricsHTTPClient(
    _AsyncHTTPHandler  # type: ignore[misc, valid-type]  # LiteLLM exports this class lazily.
):
    """A LiteLLM-compatible HTTP client with request-local metrics capture."""

    def __init__(self, delegate: Any | None = None) -> None:
        if delegate is None or not isinstance(delegate, _AsyncHTTPHandler):
            super().__init__()
            self._delegate_post = super().post
            self._owns_delegate = True
        else:
            self._delegate_post = delegate.post
            self._owns_delegate = False
        self._capture = _MetricsCapture()

    async def post(self, **kwargs: Any) -> httpx.Response:
        """Delegate the request and observe only its raw response boundary."""
        response = cast(httpx.Response, await self._delegate_post(**kwargs))
        if kwargs.get("stream") is not True:
            self._attach_response_json(response)
        return response

    def _attach_response_json(self, response: httpx.Response) -> None:
        """Observe the same decoded mapping LiteLLM's transform consumes."""
        response_ref = weakref.ref(response)
        parser = type(response).json

        def capture_json(**kwargs: Any) -> Any:
            current = response_ref()
            if current is None:
                raise RuntimeError("the LiteLLM response was released before decoding")
            raw = parser(current, **kwargs)
            self._capture.observe_mapping(raw)
            return raw

        response.json = capture_json  # type: ignore[method-assign]  # Per-response hook, never global.

    def attach_stream(self, response: Any) -> None:
        """Observe raw dicts at the request-local iterator's parser seam."""
        iterator = getattr(response, "completion_stream", None)
        if iterator is None:
            raise RuntimeError("the supported LiteLLM stream parser is unavailable")
        parser = getattr(type(iterator), "chunk_parser", None)
        if not callable(parser):
            raise RuntimeError("the supported LiteLLM stream parser is unavailable")
        iterator_ref = weakref.ref(iterator)

        def capture_then_transform(chunk: Any) -> Any:
            current = iterator_ref()
            if current is None:
                raise RuntimeError("the LiteLLM stream parser was released during decoding")
            self._capture.observe_mapping(chunk)
            return parser(current, chunk)

        iterator.chunk_parser = capture_then_transform

    def metrics_event(self) -> dict[str, Any] | None:
        """Return the captured provider-neutral terminal metrics."""
        return self._capture.event()

    async def aclose(self) -> None:
        """Close only the per-request delegate this wrapper created."""
        if not self._owns_delegate:
            return
        await super().close()
