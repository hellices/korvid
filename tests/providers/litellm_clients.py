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

import asyncio
import inspect
from collections.abc import Callable
from contextlib import suppress

import litellm


async def _drain_logging_worker() -> None:
    """Finish callbacks that have already reached LiteLLM's worker."""
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    await GLOBAL_LOGGING_WORKER.clear_queue()  # type: ignore[no-untyped-call]  # SDK has no return annotation.
    await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)


async def drain_logging() -> None:
    """Finish dispatched and queued callbacks before their event loop closes."""
    await _drain_logging_worker()

    current = asyncio.current_task()
    dispatchers = [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and getattr(task.get_coro(), "__qualname__", "") == "_client_async_logging_helper"
    ]
    await asyncio.gather(*dispatchers)

    # Dispatchers create the callback coroutine and initialize the global
    # worker, so drain again after they have handed their work to its queue.
    await _drain_logging_worker()


async def _wait_for_transport_closures() -> None:
    """Cross the ready-queue boundary after cached transports start closing."""
    loop = asyncio.get_running_loop()
    barrier = asyncio.Event()
    loop.call_soon(barrier.set)
    await barrier.wait()


async def drop_cached_clients() -> None:
    """Close and forget every client LiteLLM cached, without leaking one."""
    await drain_logging()
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
    # Proactor transport.close() queues connection_lost(), which owns the
    # socket until that callback runs even after the client's aclose() returns.
    await _wait_for_transport_closures()
