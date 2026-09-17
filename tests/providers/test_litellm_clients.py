"""SDK cleanup must finish queued logging before a test's event loop closes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import litellm
import pytest
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

from tests import local_endpoint
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


async def test_client_cleanup_waits_for_nested_transport_close_callbacks() -> None:
    await drop_cached_clients()
    closed = asyncio.Event()

    class ProactorTlsLikeTransport:
        def close(self) -> None:
            asyncio.get_running_loop().call_soon(self._begin_tls_shutdown)

        def _begin_tls_shutdown(self) -> None:
            asyncio.get_running_loop().call_soon(self._close_socket)

        def _close_socket(self) -> None:
            asyncio.get_running_loop().call_soon(closed.set)

    transport = ProactorTlsLikeTransport()

    class CachedClient:
        async def aclose(self) -> None:
            transport.close()

    litellm.in_memory_llm_clients_cache.cache_dict["closing-tls-transport"] = CachedClient()

    await drop_cached_clients()

    assert closed.is_set()


async def test_client_cleanup_drains_transport_closures_through_the_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One drain, shared with every local endpoint this suite stands up (#390).

    `tests/local_endpoint.py` takes the same two loop turns when it retires
    an endpoint, for the same reason — a queued `connection_lost()` still
    owns its socket — so a second copy here would be a second place to fix
    when that chain changes. The order is the other half of the contract:
    the drain follows the cache flush, because the closes it is draining are
    the ones the flush queued. An empty cache at drain time is what says so.
    """
    await drop_cached_clients()
    flushed: list[int] = []

    async def record_drain() -> None:
        flushed.append(len(litellm.in_memory_llm_clients_cache.cache_dict))

    with monkeypatch.context() as scoped:
        scoped.setattr(local_endpoint, "drain_transport_closures", record_drain)
        litellm.in_memory_llm_clients_cache.cache_dict["nothing-to-close"] = object()
        await drop_cached_clients()

    assert flushed == [0], "the drain has to follow the cache flush whose closes it drains"
