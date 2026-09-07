"""Closing the SDK clients LiteLLM caches, so no test leaks an open one.

The cache holds live `httpx`/`aiohttp` sessions keyed by api key, base URL,
timeout and retry count. Flushing it alone drops the last reference to an
*open* session, whose finalizer raises a `ResourceWarning` that this
suite's `filterwarnings = ["error"]` turns into a failure in whichever
unrelated test happens to be running when the collector gets to it — and
with `pytest-randomly` that is rarely the test that built the client.

Every module that drives the real SDK against a local endpoint therefore
closes what it cached, before and after, through this one helper.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import suppress

import litellm


async def drop_cached_clients() -> None:
    """Close and forget every client LiteLLM cached, without leaking one."""
    cache = getattr(litellm.in_memory_llm_clients_cache, "cache_dict", {})
    for client in list(cache.values()):
        for name in ("aclose", "close"):
            closer = getattr(client, name, None)
            if closer is None:
                continue
            with suppress(Exception):  # a half-built client must not fail a test
                result = closer()
                if inspect.isawaitable(result):
                    await result
            break
    # `InMemoryCache.flush_cache` carries no annotations in 1.98.0, so it is
    # bound through the signature it actually has before being called.
    flush_cache: Callable[[], None] = litellm.in_memory_llm_clients_cache.flush_cache
    flush_cache()
