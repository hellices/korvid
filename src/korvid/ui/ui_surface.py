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
from typing import Any, Literal, TypeVar

from textual.await_complete import AwaitComplete
from textual.screen import Screen
from textual.widget import AwaitMount
from textual.worker import Worker

#: The severities Textual accepts. As a bare `str` an invalid value
#: type-checks and fails when the toast is rendered.
Severity = Literal["information", "warning", "error"]

#: Result a modal returns through its callback. Binding it makes the
#: screen and the callback agree, so a handler written for the wrong
#: screen no longer type-checks.
ScreenResultT = TypeVar("ScreenResultT")


class UiSurface(ABC):
    """Notifications, modals, workers and progress, as one named boundary."""

    @abstractmethod
    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
    ) -> None:
        """Show a toast. The only way a controller talks to the user directly."""

    @abstractmethod
    def push_screen(
        self,
        screen: Screen[ScreenResultT],
        callback: Callable[[ScreenResultT | None], None] | None = None,
    ) -> AwaitMount | AwaitComplete:
        """Open a modal and, optionally, route its result to `callback`.

        The callback takes the screen's own result type, so a handler
        written against a different screen is a type error rather than a
        runtime surprise. `None` is included because Textual passes it
        when a screen is dismissed without a result.

        The return value awaits the mount, not the dismissal - callers that
        need the result take it through `callback`.
        """

    @abstractmethod
    def run_worker(
        self,
        work: Coroutine[Any, Any, Any] | Callable[[], Any],
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
    ) -> Worker[Any]:
        """Run work off the message pump.

        Ownership stays with the app, so a controller must not spawn bare
        `asyncio` tasks that nothing supervises: app workers are cancelled
        on shutdown and are visible to the test pilot.

        A `:ctx` switch is *not* a cancellation point. It cancels only the
        `hint-events` group; every other worker keeps running against the
        cluster it was started for. A controller whose work outlives a
        context switch must revalidate through `WriteGate.context_intact`
        or the epoch it captured - the surface will not do it for you.
        """

    @abstractmethod
    def progress(self, label: str) -> contextlib.AbstractContextManager[None]:
        """Status-bar progress scoped exactly to the wrapped await."""

    @abstractmethod
    def is_current_screen(self, screen: Screen[Any]) -> bool:
        """Whether *screen* is still the one on top.

        A flow that awaited must not act on a screen the user has since
        covered or dismissed. The question is asked rather than the screen
        handed over: a live `Screen` also carries `dismiss` and `app`,
        which is app access routed around this surface.
        """

    @abstractmethod
    def screen_depth(self) -> int:
        """How many screens are stacked; more than one means an interloper.

        A depth rather than the stack itself: Textual's list is live, so
        handing it over lets a controller `pop`, `clear` or reorder screens
        outside the lifecycle that owns them.
        """
