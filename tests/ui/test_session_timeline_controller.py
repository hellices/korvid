"""Unit tests for `SessionTimelineController` (issue #282).

The controller owns the session timeline producers — watch-delta recording,
Warning-event feed lifecycle, context-switch recording, write recording, and
the modal open/navigate flow — without touching Textual workers directly: it
calls the narrow `UiSurface` and `ViewState` boundaries, so everything is
testable here without a running app.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from typing import Any, Literal

import pytest

from korvid.core.session_timeline import (
    AppendResult,
    SessionTimeline,
    TimelineResourceRef,
    TimelineSource,
)
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary
from korvid.ui.session_timeline_controller import (
    TIMELINE_EVENT_GROUP,
    TIMELINE_NAVIGATION_GROUP,
    SessionTimelineController,
)
from korvid.ui.ui_surface import Severity, UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.session_timeline_screen import SessionTimelineScreen

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Notification:
    message: str
    severity: str
    markup: bool


class FakeUiSurface(UiSurface):
    """Minimal UiSurface that records every call a controller makes."""

    def __init__(self, *, close_worker_coroutines: bool = True) -> None:
        self.notifications: list[_Notification] = []
        self.workers: list[Any] = []  # stores the coroutine/callable passed to run_worker
        self.worker_groups: list[tuple[str, bool]] = []  # (group, exit_on_error)
        self.cancelled_groups: list[str] = []
        self.screens: list[Any] = []
        self.screen_callback: Callable[[Any], None] | None = None
        # Tests that need to actually run the submitted worker (e.g. to
        # assert what a navigation coroutine was called with) set this
        # False and await the captured coroutine themselves; every other
        # test keeps the default so an unawaited coroutine never survives
        # to trip `filterwarnings = ["error"]` in a later test.
        self._close_worker_coroutines = close_worker_coroutines

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notifications.append(_Notification(message, severity, markup))

    def push_screen(
        self,
        screen: Any,
        callback: Callable[[Any], None] | None = None,
    ) -> Any:
        self.screens.append(screen)
        self.screen_callback = callback
        return None

    def run_worker(
        self,
        work: Any,
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
        exit_on_error: bool = True,
        thread: bool = False,
    ) -> Any:
        # Close bare coroutines immediately to prevent RuntimeWarning about
        # unawaited coroutines — sync tests that only check group metadata
        # don't need to run the work.
        if asyncio.iscoroutine(work) and self._close_worker_coroutines:
            work.close()
            self.workers.append(None)
        else:
            self.workers.append(work)
        self.worker_groups.append((group, exit_on_error))
        return None

    async def cancel_workers(self, group: str) -> None:
        self.cancelled_groups.append(group)

    def suspend(self) -> contextlib.AbstractContextManager[None]:
        raise NotImplementedError  # pragma: no cover

    def refresh(self) -> None:
        pass  # pragma: no cover

    def call_from_thread(self, callback: Callable[..., Any], *args: Any) -> None:
        pass  # pragma: no cover

    def call_later(self, callback: Callable[..., None], *args: Any) -> None:
        callback(*args)  # pragma: no cover

    def progress(self, label: str) -> contextlib.AbstractContextManager[None]:
        raise NotImplementedError  # pragma: no cover

    def is_current_screen(self, screen: Any) -> bool:
        return False  # pragma: no cover

    def screen_depth(self) -> int:
        return 1  # pragma: no cover

    def inline_input_active(self) -> bool:
        return False  # pragma: no cover


class FakeViewState(ViewState):
    """Minimal ViewState wrapping an alias map."""

    def __init__(self, alias_map: dict[str, ResourceMeta] | None = None) -> None:
        self._alias_map: dict[str, ResourceMeta] = alias_map or {}

    def canonical_kind(self, kind: str) -> str:
        meta = self._alias_map.get(kind)
        if meta is None:
            return kind
        if self._alias_map.get(meta.plural) is meta:
            return meta.plural
        candidates = [a for a, m in self._alias_map.items() if m is meta]
        return min(candidates, default=kind)

    def aliases(self) -> Mapping[str, ResourceMeta]:
        return self._alias_map

    # ---- unused abstract methods ----

    def current_kind(self) -> str:
        return "pods"  # pragma: no cover

    def current_scope(self) -> str:
        return ""  # pragma: no cover

    def current_namespace(self) -> str:
        return ""  # pragma: no cover

    def resources(self, kind: str, scope: str) -> list[Any]:
        return []  # pragma: no cover

    def readonly(self) -> bool:
        return False  # pragma: no cover

    def default_namespace(self) -> str | None:
        return None  # pragma: no cover

    def selected_ns_name(self) -> tuple[str | None, str | None]:
        return None, None  # pragma: no cover

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        return None  # pragma: no cover

    def gvr_label(self, meta: ResourceMeta) -> str:
        return meta.plural  # pragma: no cover

    def write_locus(self, namespace: str | None) -> str:
        return ""  # pragma: no cover


# ---------------------------------------------------------------------------
# Fake watch manager
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _FakeWatchManager:
    """Exposes only the `on_event` attribute the controller sets."""

    on_event: Callable[[str, str, str, Any], None] | None = None


# ---------------------------------------------------------------------------
# Fake timeline that raises
# ---------------------------------------------------------------------------


class _ExplodingTimeline(SessionTimeline):
    """A `SessionTimeline` whose `append_context_switch` always raises.

    Exercises `_append_timeline`'s own exception handler — a producer
    call that raises must become a visible, non-fatal notification rather
    than propagating out of a watch/context-switch/write call site that
    must never raise.
    """

    def append_context_switch(
        self,
        *,
        epoch: int,
        phase: Literal["started", "completed", "failed"],
        from_context: str | None,
        to_context: str | None,
        note: str = "",
    ) -> AppendResult:
        raise RuntimeError("simulated timeline failure")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PODS_ALIASES = build_alias_map([PODS_META])


async def _default_navigate(kind_alias: str, namespace: str, name: str, epoch: int) -> None:
    """Stand-in for the app's real navigate callback.

    `navigate` is a required constructor argument (the app always wires
    one), so every test factory needs a value even when the test does not
    care what navigation happens.
    """
    return None


def make_controller(
    *,
    timeline: SessionTimeline | None,
    epoch: int = 0,
    epoch_crossed: bool = False,
    watch_warning_events: Callable[[str | None], AsyncIterator[dict[str, Any]]] | None = None,
    selected_resource: Callable[[], TimelineResourceRef | None] | None = None,
    navigate: Callable[[str, str, str, int], Coroutine[Any, Any, None]] = _default_navigate,
    alias_map: dict[str, ResourceMeta] | None = None,
    close_worker_coroutines: bool = True,
) -> tuple[SessionTimelineController, _FakeWatchManager, FakeUiSurface]:
    ui = FakeUiSurface(close_worker_coroutines=close_worker_coroutines)
    view = FakeViewState(alias_map if alias_map is not None else dict(_PODS_ALIASES))
    wm: _FakeWatchManager = _FakeWatchManager()
    controller = SessionTimelineController(
        ui=ui,
        view=view,
        watch_manager=wm,
        timeline=timeline,
        get_epoch=lambda: epoch,
        epoch_crossed=lambda ep: epoch_crossed,
        watch_warning_events=watch_warning_events,
        selected_resource=selected_resource,
        navigate=navigate,
    )
    return controller, wm, ui


# ---------------------------------------------------------------------------
# Constructor contract
# ---------------------------------------------------------------------------


def test_navigate_callback_is_a_required_constructor_argument() -> None:
    """`navigate` has no default: a controller built without it is a
    construction error, not a silently-inert modal (the app always wires
    one; only a broken caller would omit it)."""
    with pytest.raises(TypeError, match="navigate"):
        SessionTimelineController(  # type: ignore[call-arg]
            ui=FakeUiSurface(),
            view=FakeViewState(dict(_PODS_ALIASES)),
            watch_manager=_FakeWatchManager(),
            timeline=None,
            get_epoch=lambda: 0,
            epoch_crossed=lambda epoch: False,
        )


# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


def test_start_is_inert_without_timeline() -> None:
    controller, watch_manager, ui = make_controller(timeline=None)

    controller.start()

    assert watch_manager.on_event is None
    assert ui.workers == []


def test_start_wires_watch_manager_and_starts_worker() -> None:
    async def _noop_watch(ns: str | None) -> AsyncIterator[dict[str, Any]]:
        return
        yield  # pragma: no cover

    timeline = SessionTimeline(8, 4096)
    controller, watch_manager, ui = make_controller(
        timeline=timeline,
        watch_warning_events=_noop_watch,
    )

    controller.start()

    assert watch_manager.on_event == controller.record_watch_event
    assert len(ui.workers) == 1
    assert ui.worker_groups[0] == (TIMELINE_EVENT_GROUP, False)


def test_watch_event_records_live_epoch_and_canonical_alias() -> None:
    timeline = SessionTimeline(8, 4096)
    controller, _, _ = make_controller(timeline=timeline, epoch=7)

    controller.record_watch_event(
        "po",
        "default",
        "ADDED",
        GenericSummary(
            name="api",
            namespace="default",
            kind="Pod",
            created="",
            uid="pod-1",
        ),
    )

    entry = timeline.snapshot(epoch=7, source=TimelineSource.WATCH, resource=None).entries[0]
    assert entry.resource is not None
    assert entry.resource.kind_alias == "pods"


def test_watch_event_ignores_unknown_event_type() -> None:
    timeline = SessionTimeline(8, 4096)
    controller, _, _ = make_controller(timeline=timeline, epoch=0)

    controller.record_watch_event(
        "pods",
        "default",
        "BOOKMARK",
        GenericSummary(name="x", namespace="default", kind="Pod", created="", uid="u"),
    )

    snapshot = timeline.snapshot(epoch=0, source=None, resource=None)
    assert snapshot.entries == ()


def test_watch_event_noop_without_timeline() -> None:
    controller, _, ui = make_controller(timeline=None)

    controller.record_watch_event(
        "pods",
        "default",
        "ADDED",
        GenericSummary(name="x", namespace="default", kind="Pod", created="", uid="u"),
    )

    assert ui.notifications == []


def test_record_context_switch_appends_entry() -> None:
    timeline = SessionTimeline(8, 4096)
    controller, _, _ = make_controller(timeline=timeline, epoch=3)

    controller.record_context_switch(
        epoch=3,
        phase="completed",
        from_context="old",
        to_context="new",
    )

    snapshot = timeline.snapshot(epoch=3, source=TimelineSource.CONTEXT, resource=None)
    assert len(snapshot.entries) == 1


def test_record_context_switch_noop_without_timeline() -> None:
    controller, _, ui = make_controller(timeline=None)

    controller.record_context_switch(epoch=0, phase="started", from_context=None, to_context="dev")

    assert ui.notifications == []


def test_record_write_notifies_on_refused_append() -> None:
    """An entry too large for `max_bytes` is refused by `SessionTimeline`
    (`AppendResult.accepted=False`, a diagnostic string) — the controller
    must surface that refusal, not drop the write silently."""
    tiny_timeline = SessionTimeline(max_entries=8, max_bytes=1)
    controller, _, ui = make_controller(timeline=tiny_timeline, epoch=1)

    controller.record_write(
        epoch=1,
        action="delete",
        kind_alias="deployments",
        display_kind="Deployment",
        namespace="default",
        name="api",
        outcome="success",
    )

    warning_notes = [n for n in ui.notifications if n.severity == "warning"]
    assert len(warning_notes) == 1
    assert "write entry" in warning_notes[0].message
    # Keep diagnostics literal rather than parsing their details as markup.
    assert warning_notes[0].markup is False


def test_record_context_switch_notifies_when_append_raises() -> None:
    """A producer call whose `append_*` raises must not propagate: the
    controller's `_append_timeline` catches it and turns it into a visible
    warning instead of breaking the watch/context-switch call site."""
    controller, _, ui = make_controller(timeline=_ExplodingTimeline(8, 4096), epoch=0)

    controller.record_context_switch(epoch=0, phase="started", from_context="old", to_context="new")

    warning_notes = [n for n in ui.notifications if n.severity == "warning"]
    assert len(warning_notes) == 1
    assert "context switch" in warning_notes[0].message
    assert "internal timeline error" in warning_notes[0].message
    # The message is a fixed literal, but every timeline diagnostic is
    # markup=False on principle - none of them should ever be interpreted
    # as console markup.
    assert warning_notes[0].markup is False


def test_record_write_appends_entry() -> None:
    timeline = SessionTimeline(8, 4096)
    controller, _, _ = make_controller(timeline=timeline, epoch=1)

    controller.record_write(
        epoch=1,
        action="delete",
        kind_alias="deployments",
        display_kind="Deployment",
        namespace="default",
        name="api",
        outcome="success",
    )

    snapshot = timeline.snapshot(epoch=1, source=TimelineSource.WRITE, resource=None)
    assert len(snapshot.entries) == 1


def test_open_pushes_timeline_screen() -> None:
    timeline = SessionTimeline(8, 4096)
    controller, _, ui = make_controller(timeline=timeline, epoch=0)

    controller.open()

    assert len(ui.screens) == 1
    assert isinstance(ui.screens[0], SessionTimelineScreen)


def test_open_notifies_when_no_timeline() -> None:
    controller, _, ui = make_controller(timeline=None)

    controller.open()

    assert any("unavailable" in n.message for n in ui.notifications)
    assert ui.screens == []


@pytest.mark.asyncio
async def test_open_result_invokes_navigate_when_epoch_current() -> None:
    navigated: list[tuple[str, str, str, int]] = []

    async def _nav(kind_alias: str, namespace: str, name: str, epoch: int) -> None:
        navigated.append((kind_alias, namespace, name, epoch))

    timeline = SessionTimeline(8, 4096)
    controller, _, ui = make_controller(
        timeline=timeline,
        epoch=5,
        epoch_crossed=False,
        navigate=_nav,
        close_worker_coroutines=False,
    )

    controller.open()
    # Simulate the screen callback with a valid goto result
    assert ui.screen_callback is not None
    ui.screen_callback(("goto", "pods", "default", "api"))

    # navigate coroutine is submitted as a worker
    assert len(ui.workers) == 1
    assert ui.worker_groups[0] == (TIMELINE_NAVIGATION_GROUP, False)
    navigation_coroutine = ui.workers[0]
    assert navigation_coroutine is not None
    await navigation_coroutine
    assert navigated == [("pods", "default", "api", 5)]


def test_open_result_is_ignored_on_none_result() -> None:
    timeline = SessionTimeline(8, 4096)
    controller, _, ui = make_controller(timeline=timeline, epoch=0)

    controller.open()
    assert ui.screen_callback is not None
    ui.screen_callback(None)

    assert ui.workers == []
    assert ui.notifications == []


def test_stale_navigation_warns_and_does_not_navigate() -> None:
    navigated: list[Any] = []

    async def _nav(kind_alias: str, namespace: str, name: str, epoch: int) -> None:
        navigated.append((kind_alias, namespace, name, epoch))  # pragma: no cover

    timeline = SessionTimeline(8, 4096)
    controller, _, ui = make_controller(
        timeline=timeline, epoch=0, epoch_crossed=True, navigate=_nav
    )

    controller.open()
    assert ui.screen_callback is not None
    ui.screen_callback(("goto", "pods", "default", "api"))

    assert navigated == []
    assert ui.workers == []
    assert any("cancelled" in n.message for n in ui.notifications)


# ---------------------------------------------------------------------------
# Async tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_group_cancellation_on_stop() -> None:
    controller, _, ui = make_controller(timeline=SessionTimeline(8, 4096))

    await controller.stop()

    assert TIMELINE_EVENT_GROUP in ui.cancelled_groups


@pytest.mark.asyncio
async def test_warning_loop_stops_on_permanent_denial() -> None:
    calls = 0

    async def _forbidden_watch(ns: str | None) -> AsyncIterator[dict[str, Any]]:
        nonlocal calls
        calls += 1
        raise ApiStatusError(403, "Forbidden")
        yield  # pragma: no cover

    timeline = SessionTimeline(8, 4096)
    controller, _, ui = make_controller(timeline=timeline, watch_warning_events=_forbidden_watch)
    controller.TIMELINE_EVENT_RETRY_SECONDS = 0.0

    await controller._run_warning_watch()

    assert calls == 1  # tried exactly once; no retry on permanent denial
    warning_notes = [n for n in ui.notifications if n.severity == "warning"]
    assert len(warning_notes) == 1
    # The denial notification carries a cluster-controlled API reason string
    # (via `explain_api_error`) — markup=False so it can never be
    # interpreted as Textual console markup.
    assert warning_notes[0].markup is False


@pytest.mark.asyncio
async def test_warning_loop_gives_up_after_max_failures() -> None:
    async def _failing_watch(ns: str | None) -> AsyncIterator[dict[str, Any]]:
        raise RuntimeError("cluster unreachable")
        yield  # pragma: no cover

    timeline = SessionTimeline(8, 4096)
    controller, _, ui = make_controller(timeline=timeline, watch_warning_events=_failing_watch)
    controller.TIMELINE_EVENT_RETRY_SECONDS = 0.0
    max_fail = controller.TIMELINE_EVENT_MAX_FAILURES

    await controller._run_warning_watch()

    error_notes = [n for n in ui.notifications if n.severity == "error"]
    assert len(error_notes) == 1
    assert str(max_fail) in error_notes[0].message
    # The failure count is baked into an f-string notification — markup=False
    # keeps it literal even though this particular value is not
    # cluster-controlled, matching every other diagnostic this feed emits.
    assert error_notes[0].markup is False


@pytest.mark.asyncio
async def test_warning_loop_propagates_cancellation() -> None:
    async def _cancelled_watch(ns: str | None) -> AsyncIterator[dict[str, Any]]:
        raise asyncio.CancelledError()
        yield  # pragma: no cover

    timeline = SessionTimeline(8, 4096)
    controller, _, _ = make_controller(timeline=timeline, watch_warning_events=_cancelled_watch)

    with pytest.raises(asyncio.CancelledError, match=r"^$"):
        await controller._run_warning_watch()


@pytest.mark.asyncio
async def test_warning_loop_stops_when_epoch_changes_mid_stream() -> None:
    """A stale epoch causes the loop to exit without recording more events."""
    current_epoch = [0]

    async def _watch_one_event(ns: str | None) -> AsyncIterator[dict[str, Any]]:
        # Bump epoch mid-stream so the next event is dropped.
        current_epoch[0] = 99
        yield {"kind": "Event", "involvedObject": {}}

    timeline = SessionTimeline(8, 4096)
    ui = FakeUiSurface()
    view = FakeViewState()
    wm = _FakeWatchManager()
    controller = SessionTimelineController(
        ui=ui,
        view=view,
        watch_manager=wm,
        timeline=timeline,
        get_epoch=lambda: current_epoch[0],
        epoch_crossed=lambda ep: False,
        watch_warning_events=_watch_one_event,
        navigate=_default_navigate,
    )
    controller.TIMELINE_EVENT_RETRY_SECONDS = 0.0

    await controller._run_warning_watch()

    snapshot = timeline.snapshot(epoch=0, source=None, resource=None)
    assert snapshot.entries == ()
