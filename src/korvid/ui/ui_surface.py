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
the same reason `AppUIBridge` is - Textual's `App` metaclass conflicts
with `ABCMeta`. (`WriteGate` needs no adapter: `WriteCoordinator` is a plain
class and implements it directly.)
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
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
        markup: bool = True,
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
        work: Awaitable[Any] | Callable[[], Any],
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
        exit_on_error: bool = True,
        thread: bool = False,
    ) -> Worker[Any]:
        """Run work off the message pump.

        Ownership stays with the app, so a controller must not spawn bare
        `asyncio` tasks that nothing supervises: app workers are cancelled
        on shutdown and are visible to the test pilot.

        A `:ctx` switch is not one blanket cancellation point: the app's
        context-switch teardown cancels exactly the groups it knows hold a
        stale-cluster connection, by name - `hint-events`, `relationships`,
        and `timeline-warning-events` - and `cancel_workers` is the seam a
        controller uses to do the same for a group it owns, the way
        `SessionTimelineController.stop` cancels `timeline-warning-events`.
        A worker in any other group (for example the timeline navigation
        worker) is not touched by the switch and keeps running against the
        cluster it was started for. A controller whose work outlives a
        context switch must revalidate through `WriteGate.context_intact`
        or the epoch it captured - the surface will not do it for you.
        """

    @abstractmethod
    def cancel_workers(self, group: str) -> Awaitable[None]:
        """Cancel every worker in *group* and wait for them to settle."""

    @abstractmethod
    def suspend(self) -> contextlib.AbstractContextManager[None]:
        """Hand the terminal to a child process for the wrapped block.

        Textual releases the screen on entry and restores it on exit, so an
        interactive `kubectl exec` feels like a direct connection. Only the
        app can do this - it owns the driver.
        """

    @abstractmethod
    def refresh(self) -> None:
        """Repaint after the terminal comes back from a suspended child."""

    @abstractmethod
    def call_from_thread(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run *callback* on the message pump from a worker thread.

        Interactive subprocesses are driven off-loop; touching the UI from
        that thread directly is a data race.
        """

    @abstractmethod
    def call_later(self, callback: Callable[..., None], *args: Any) -> None:
        """Run *callback* on the next message-pump iteration.

        The seam a modal's result callback needs: Textual invokes it
        *before* it pops the dismissed screen, so a re-validation that
        counts stacked screens must not run inline - it would see the
        confirmation the user just answered and read as "another dialog
        opened".
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

    @abstractmethod
    def inline_focus_release_hint(self) -> str | None:
        """Actionable copy for the focused inline blocker, if one owns the next key."""
