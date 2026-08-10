"""The Textual capabilities a controller may use (issue #187).

Controllers need a handful of app-level affordances - telling the user
something, opening a modal, running work off the message pump, showing
progress. Passing those as five separate `Callable[..., Any]` arguments
costs the argument contract *and* makes "what can this controller do to the
UI?" a question you answer by reading a constructor.

`UiSurface` names that capability set instead. It is deliberately small: a
controller that needs more than this is reaching for app internals, which is
the thing the decomposition is trying to stop.

`AppUiSurface` on `KorvidApp` is the single implementation, an adapter for
the same reason `AppUIBridge` and `AppWriteGate` are - Textual's `App`
metaclass conflicts with `ABCMeta`.
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import Any


class UiSurface(ABC):
    """Notifications, modals, workers and progress, as one named boundary."""

    @abstractmethod
    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
    ) -> None:
        """Show a toast. The only way a controller talks to the user directly."""

    @abstractmethod
    def push_screen(self, screen: Any, callback: Any = None) -> Any:
        """Open a modal and, optionally, route its result to `callback`."""

    @abstractmethod
    def run_worker(
        self,
        work: Coroutine[Any, Any, Any] | Callable[[], Any],
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
    ) -> Any:
        """Run work off the message pump.

        Ownership stays with the app: it is the app's workers that get
        cancelled on shutdown and on a `:ctx` switch, so a controller must
        not spawn bare `asyncio` tasks that nothing supervises.
        """

    @abstractmethod
    def progress(self, label: str) -> contextlib.AbstractContextManager[None]:
        """Status-bar progress scoped exactly to the wrapped await."""

    @abstractmethod
    def screen(self) -> Any:
        """The screen currently on top.

        Read to check what the user is looking at *now* - a flow that
        awaited must not push a dialog over a modal the user opened during
        the gap.
        """

    @abstractmethod
    def screen_stack(self) -> list[Any]:
        """The whole modal stack; length is how flows detect an interloper."""
