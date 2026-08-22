"""Marshaling foreign `UIBridge` calls onto the app's execution context.

MCP requests (and the follow mirrors they spawn) arrive in tasks whose
context lacks Textual's `active_app` ContextVar; composing a widget tree
there (`DescribeScreen`'s `VerticalScroll`) raises `NoActiveAppError` and
terminates the app (issue #165). Every bridge call is therefore run inside a
copy of the context the app captured on mount.

The snapshot and the in-flight dispatch set are one owner here rather than
app attributes a bridge adapter reaches into: the app activates the
dispatcher when it mounts and shuts it down when it unmounts, and nothing
else may hand out UI execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from typing import Any


class BridgeDispatch(ABC):
    """Where a `UIBridge` coroutine is allowed to run."""

    @abstractmethod
    async def run(self, coro: Coroutine[Any, Any, str]) -> str:
        """Run one bridge coroutine, or refuse with an "ERROR: …" string.

        Implementations must close *coro* when they refuse, so a rejected
        call never warns as un-awaited, and must propagate cancellation into
        the work they started so shutdown strands nothing.
        """


class AppContextDispatch(BridgeDispatch):
    """Runs bridge coroutines inside a copy of the app's mounted context.

    Inactive until `activate()` is called from the message pump, and
    inactive again after `shutdown()`. Both edges are production-reachable:
    the MCP endpoint goes live before `app.run_async()`, and a request
    racing teardown must not spawn work (log streams) against an unmounted
    app.
    """

    _NOT_READY = "ERROR: UI not ready — the app is starting or shutting down; retry shortly"

    def __init__(self) -> None:
        self._context: contextvars.Context | None = None
        self._tasks: set[asyncio.Task[str]] = set()

    def activate(self) -> None:
        """Capture the calling context as the one bridge calls run in.

        Called from `on_mount`, which runs inside Textual's message pump, so
        the snapshot carries `active_app` and the pump's ContextVars.
        """
        self._context = contextvars.copy_context()

    @property
    def active(self) -> bool:
        """Whether foreign UI work is currently accepted."""
        return self._context is not None

    async def run(self, coro: Coroutine[Any, Any, str]) -> str:
        """Run one bridge coroutine inside a copy of the app context.

        A fresh copy per call: a `contextvars.Context` cannot be entered
        concurrently, and the serialized proxy is not the only caller (the
        in-app agent path may overlap a queued MCP call's dispatch).
        Cancellation propagates into the inner task so shutdown never
        strands UI work.
        """
        snapshot = self._context
        if snapshot is None:
            coro.close()
            return self._NOT_READY
        task = asyncio.get_running_loop().create_task(
            coro, context=snapshot.run(contextvars.copy_context)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise

    async def shutdown(self) -> None:
        """Refuse new foreign UI work and reap the in-flight dispatches."""
        self._context = None
        for pending in [task for task in self._tasks if not task.done()]:
            pending.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
