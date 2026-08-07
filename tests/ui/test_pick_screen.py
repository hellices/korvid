"""`PickScreen` must survive being mounted before its options exist."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from korvid.ui.widgets.pick_screen import PickScreen


def _capture_scheduled_calls(screen: PickScreen) -> list[Callable[..., Any]]:
    """Record `call_after_refresh` callbacks instead of running them.

    The screen is never mounted in these tests, so there is no app to drive the
    refresh; capturing the callback lets a test invoke it explicitly.
    """
    scheduled: list[Callable[..., Any]] = []

    def record(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        scheduled.append(lambda: callback(*args, **kwargs))
        return True

    screen.call_after_refresh = record  # type: ignore[method-assign]  # exercising the unmounted path without a running app
    return scheduled


def test_mount_does_not_require_the_option_list_to_exist_yet() -> None:
    """`on_mount` can fire before `compose` children are mounted.

    Textual does not guarantee that a screen's composed children are queryable
    by the time `on_mount` runs. When the option list is not there yet, the
    screen must schedule the focus instead of raising `NoMatches` and killing
    the whole app (observed as an intermittent CI failure in
    `tests/ui/test_ctx_switch.py`).
    """
    screen = PickScreen("pick a context", ["ctx-a", "ctx-b"])
    scheduled = _capture_scheduled_calls(screen)

    screen.on_mount()

    assert scheduled, "focus should be retried once the option list is composed"


def test_the_deferred_focus_retry_does_not_reschedule_itself() -> None:
    """A screen whose options never appear must stop, not spin.

    The retry exists to survive one lost race, not to poll forever: if the
    option list is still missing when the deferred callback runs, the screen
    gives up quietly rather than queueing more work on every refresh.
    """
    screen = PickScreen("pick a context", ["ctx-a", "ctx-b"])
    scheduled = _capture_scheduled_calls(screen)
    screen.on_mount()
    retry = scheduled.pop()

    retry()

    assert not scheduled, "the retry must not queue another retry"
