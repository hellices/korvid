"""`PickScreen` must survive being mounted before its options exist."""

from __future__ import annotations

from korvid.ui.widgets.pick_screen import PickScreen


def test_mount_does_not_require_the_option_list_to_exist_yet() -> None:
    """`on_mount` can fire before `compose` children are mounted.

    Textual does not guarantee that a screen's composed children are queryable
    by the time `on_mount` runs. When the option list is not there yet, the
    screen must schedule the focus instead of raising `NoMatches` and killing
    the whole app (observed as an intermittent CI failure in
    `tests/ui/test_ctx_switch.py`).
    """
    screen = PickScreen("pick a context", ["ctx-a", "ctx-b"])
    scheduled: list[object] = []

    def record(callback: object, *args: object, **kwargs: object) -> bool:
        scheduled.append(callback)
        return True

    screen.call_after_refresh = record  # type: ignore[method-assign]  # exercising the unmounted path without a running app

    screen.on_mount()

    assert scheduled, "focus should be retried once the option list is composed"
