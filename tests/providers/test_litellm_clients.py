"""SDK cleanup must finish queued logging before a test's event loop closes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import litellm
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

from tests.providers.litellm_clients import drop_cached_clients


async def test_client_cleanup_drains_queued_logging() -> None:
    # Starting the SDK worker resets its old loop's queue, so retire it first.
    await drop_cached_clients()
    completed = asyncio.Event()

    async def callback() -> None:
        completed.set()

    enqueue: Callable[[Coroutine[Any, Any, None]], None] = (
        GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue
    )
    enqueue(callback())
    try:
        await drop_cached_clients()
        assert completed.is_set()
    finally:
        await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
        await GLOBAL_LOGGING_WORKER.stop()


async def test_client_cleanup_waits_for_already_dequeued_logging() -> None:
    await drop_cached_clients()
    started = asyncio.Event()
    release = asyncio.Event()

    async def callback() -> None:
        started.set()
        await release.wait()

    enqueue: Callable[[Coroutine[Any, Any, None]], None] = (
        GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue
    )
    enqueue(callback())
    await asyncio.wait_for(started.wait(), timeout=5)
    cleanup = asyncio.create_task(drop_cached_clients())
    try:
        await asyncio.sleep(0)
        assert not cleanup.done()
    finally:
        release.set()
        await asyncio.wait_for(cleanup, timeout=5)
        await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
        await GLOBAL_LOGGING_WORKER.stop()


async def test_client_cleanup_waits_for_transport_connection_lost_callback() -> None:
    await drop_cached_clients()

    class ProactorLikeTransport:
        def __init__(self) -> None:
            self.closing = False
            self.closed = False

        def close(self) -> None:
            self.closing = True
            asyncio.get_running_loop().call_soon(self.connection_lost)

        def connection_lost(self) -> None:
            self.closed = True

    transport = ProactorLikeTransport()

    class CachedClient:
        async def aclose(self) -> None:
            transport.close()

    litellm.in_memory_llm_clients_cache.cache_dict["closing-transport"] = CachedClient()

    await drop_cached_clients()

    assert transport.closing
    assert transport.closed
