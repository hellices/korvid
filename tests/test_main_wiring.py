"""Tests for composition-root helpers in korvid.__main__."""

from __future__ import annotations

import asyncio
from typing import cast

from korvid.__main__ import _close_provider_in_background


class _BoomProvider:
    async def aclose(self) -> None:
        raise RuntimeError("boom")


class _OkProvider:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_close_task_reference_is_retained_until_done() -> None:
    from korvid.agent.provider import LLMProvider

    provider = _OkProvider()
    tasks: set[asyncio.Task[None]] = set()
    _close_provider_in_background(cast("LLMProvider", provider), tasks)
    assert len(tasks) == 1  # strong reference held while pending
    for _ in range(3):
        await asyncio.sleep(0)
    assert provider.closed
    assert not tasks  # reaped once complete


async def test_close_errors_are_consumed() -> None:
    from korvid.agent.provider import LLMProvider

    tasks: set[asyncio.Task[None]] = set()
    _close_provider_in_background(cast("LLMProvider", _BoomProvider()), tasks)
    for _ in range(3):
        await asyncio.sleep(0)
    # Exception must be retrieved by the done callback (no unhandled-task
    # warning); the set must not leak the failed task.
    assert not tasks
