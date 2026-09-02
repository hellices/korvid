"""Unit tests for `LogController` (log subsystem ownership extraction).

The controller owns the log subsystem's mutable state (stream tasks, buffer,
reconnect/error flags, selected-stream triples, pane generation, pane mode and
pane ownership) and its workflows (open/toggle, stream construction, the
live/previous stream lifecycle, and the display actions). It reaches the UI
only through the narrow `UiSurface` (for notifications) and a `LogPaneView`
accessor, so its non-widget logic is testable here without a running app.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from korvid.core.logbuffer import LogBuffer
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.ui.log_controller import LogController
from korvid.ui.ui_surface import Severity, UiSurface

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Notification:
    message: str
    severity: str
    markup: bool


class FakeUiSurface(UiSurface):
    """Records the notifications the controller emits; the rest is unused."""

    def __init__(self) -> None:
        self.notifications: list[_Notification] = []

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

    def push_screen(self, screen: Any, callback: Any = None) -> Any:
        raise NotImplementedError  # pragma: no cover

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
        raise NotImplementedError  # pragma: no cover

    async def cancel_workers(self, group: str) -> None:
        raise NotImplementedError  # pragma: no cover

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

    def inline_focus_release_hint(self) -> str | None:
        return None  # pragma: no cover


class FakeLogPane:
    """A structural `LogPaneView` that records every call the controller makes."""

    def __init__(self) -> None:
        self.display: bool = False
        self.opened: list[list[tuple[str, str]]] = []
        self.closed: int = 0
        self.fed: list[LogLine] = []
        self.replayed: list[list[LogLine]] = []
        self.states: list[str] = []
        self.banners: list[str] = []
        self.overflow_banners: int = 0
        self.searches: list[str] = []
        self.toggles: list[str] = []
        self.last_force_prefix: bool | None = None
        self.last_log_buffer: LogBuffer | None = None

    def open(
        self,
        sources: list[tuple[str, str]],
        *,
        force_prefix: bool = False,
        log_buffer: LogBuffer | None = None,
    ) -> None:
        self.display = True
        self.opened.append(list(sources))
        self.last_force_prefix = force_prefix
        self.last_log_buffer = log_buffer

    def close(self) -> None:
        self.display = False
        self.closed += 1

    def feed(self, line: LogLine) -> None:
        self.fed.append(line)

    def replay(self, lines: list[LogLine]) -> None:
        self.replayed.append(list(lines))

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def write_banner(self, text: str) -> None:
        self.banners.append(text)

    def show_overflow_banner(self) -> None:
        self.overflow_banners += 1

    def search_next(self) -> None:
        self.searches.append("next")

    def search_prev(self) -> None:
        self.searches.append("prev")

    def toggle_format(self) -> None:
        self.toggles.append("format")

    def toggle_wrap(self) -> None:
        self.toggles.append("wrap")

    def toggle_timestamps(self) -> None:
        self.toggles.append("timestamps")


StreamFn = Callable[..., AsyncIterator[LogLine]]


@dataclasses.dataclass
class _Harness:
    controller: LogController
    ui: FakeUiSurface
    pane: FakeLogPane
    owner: object
    refreshes: list[bool]


async def _hanging_stream(
    namespace: str, pod: str, container: str, **_: Any
) -> AsyncIterator[LogLine]:
    """A stream that never yields and blocks until its task is cancelled."""
    await asyncio.Event().wait()
    yield LogLine(pod=pod, container=container, text="", timestamp=None)  # pragma: no cover


def make_harness(
    *,
    stream_logs: StreamFn | None = _hanging_stream,
    current_kind: str = "pods",
    selected: tuple[str | None, str | None] = ("default", "web"),
    pod_containers: Callable[[str, str], tuple[str, ...]] | None = None,
    visible_pod_keys: Callable[[], list[str]] | None = None,
    ctx_epoch: int = 0,
    ctx_switch_crossed: bool = False,
    ctx_reads_allowed: bool = True,
    buffer_max_lines: int = 5000,
) -> _Harness:
    ui = FakeUiSurface()
    pane = FakeLogPane()
    owner = object()
    refreshes: list[bool] = []
    controller = LogController(
        ui=ui,
        get_log_pane=lambda: pane,
        get_stream_logs=lambda: stream_logs,
        pod_containers=pod_containers or (lambda ns, name: ("main",)),
        selected_ns_name=lambda: selected,
        visible_pod_keys=visible_pod_keys or (lambda: []),
        current_kind=lambda: current_kind,
        focused_pane=lambda: owner,
        ctx_epoch=lambda: ctx_epoch,
        ctx_switch_crossed=lambda epoch: ctx_switch_crossed,
        ctx_reads_allowed=lambda: ctx_reads_allowed,
        refresh_bindings=lambda: refreshes.append(True),
        buffer_max_lines=buffer_max_lines,
    )
    return _Harness(controller, ui, pane, owner, refreshes)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_state_is_empty() -> None:
    h = make_harness()
    assert h.controller.mode == ""
    assert h.controller.pane_gen == 0
    assert h.controller.tasks == frozenset()
    assert h.controller.buffer is None
    assert h.controller.current_triples == []


# ---------------------------------------------------------------------------
# Open / close state ownership
# ---------------------------------------------------------------------------


async def test_open_pane_spawns_one_task_per_source_and_buffers() -> None:
    h = make_harness()
    await h.controller.open_pane("default", [("web", "main"), ("web", "sidecar")])
    try:
        assert h.pane.display is True
        assert len(h.controller.tasks) == 2
        assert h.controller.buffer is not None
        assert h.controller.current_triples == [
            ("default", "web", "main"),
            ("default", "web", "sidecar"),
        ]
        assert h.controller.pane_gen == 1
        assert h.refreshes  # the footer legend is refreshed on open
    finally:
        await h.controller.cancel_tasks()


async def test_close_resets_all_owned_state() -> None:
    h = make_harness()
    await h.controller.open_pane("default", [("web", "main")])
    await h.controller.close()

    assert h.pane.display is False
    assert h.pane.closed == 1
    assert h.controller.tasks == frozenset()
    assert h.controller.buffer is None
    assert h.controller.current_triples == []
    assert h.controller.mode == ""
    assert h.controller.pane_gen == 2  # +1 on open, +1 on close


async def test_open_pane_dropped_when_context_switch_crossed() -> None:
    h = make_harness(ctx_switch_crossed=True)
    await h.controller.open_pane("default", [("web", "main")], epoch=0)

    assert h.pane.display is False
    assert h.controller.tasks == frozenset()
    assert any("context changed" in n.message for n in h.ui.notifications)


async def test_open_pane_caps_spawned_tasks_at_max_panels() -> None:
    triples = [("default", f"pod-{i}", "main") for i in range(12)]
    sources = [(pod, ctr) for _, pod, ctr in triples]
    h = make_harness()
    await h.controller.open_pane("default", sources, triples=triples)
    try:
        assert len(h.controller.tasks) == 8  # MAX_PANELS
        assert len(h.controller.current_triples) == 8
        assert any("Showing first 8" in n.message for n in h.ui.notifications)
    finally:
        await h.controller.cancel_tasks()


# ---------------------------------------------------------------------------
# Split-pane owner semantics
# ---------------------------------------------------------------------------


async def test_close_if_owned_by_only_closes_the_owning_pane() -> None:
    h = make_harness()
    await h.controller.open_pane("default", [("web", "main")])

    other = object()
    await h.controller.close_if_owned_by(other)
    assert h.pane.display is True  # a non-owner's navigation leaves it streaming

    await h.controller.close_if_owned_by(h.owner)
    assert h.pane.display is False  # the owner's navigation tears it down


# ---------------------------------------------------------------------------
# Stream cancellation and reaping
# ---------------------------------------------------------------------------


async def test_cancel_tasks_reaps_without_hiding_pane() -> None:
    h = make_harness()
    await h.controller.open_pane("default", [("web", "main")])
    assert h.controller.tasks

    await h.controller.cancel_tasks()

    assert h.controller.tasks == frozenset()
    assert h.controller.buffer is None
    assert h.pane.display is True  # reopen path keeps the pane visible
    assert h.pane.closed == 0


async def test_shutdown_cancels_and_clears_tasks() -> None:
    h = make_harness()
    await h.controller.open_pane("default", [("web", "main")])
    assert h.controller.tasks

    await h.controller.shutdown()

    assert h.controller.tasks == frozenset()


# ---------------------------------------------------------------------------
# Reconnect and previous-log transition
# ---------------------------------------------------------------------------


async def test_live_stream_gives_up_visibly_after_max_reconnects() -> None:
    attempts = 0

    async def failing(namespace: str, pod: str, container: str, **_: Any) -> AsyncIterator[LogLine]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("transient")
        yield  # pragma: no cover - marks this an async generator

    h = make_harness(stream_logs=failing)
    h.controller.reconnect_sleep = 0.0
    await h.controller.open_pane("default", [("web", "main")])
    await asyncio.gather(*h.controller.tasks, return_exceptions=True)

    # One initial try plus five retries reach the cap of _MAX_RECONNECT_ATTEMPTS.
    assert attempts == 6
    assert h.pane.states[-1] == "error"
    assert any(
        n.severity == "error" and "reconnect attempts" in n.message for n in h.ui.notifications
    )


async def test_action_log_previous_transitions_to_previous_mode() -> None:
    line = LogLine(pod="web", container="main", text="hi", timestamp=None)

    async def one_shot(
        namespace: str, pod: str, container: str, **_: Any
    ) -> AsyncIterator[LogLine]:
        yield line

    h = make_harness(stream_logs=one_shot)
    h.controller.reconnect_sleep = 0.0
    # Seed an open live pane; `p` cancels it and re-streams the same triples.
    await h.controller.open_pane("default", [("web", "main")], triples=[("default", "web", "main")])
    await h.controller.action_log_previous()
    await asyncio.gather(*h.controller.tasks, return_exceptions=True)

    assert h.controller.mode == "p"
    assert "\u2500\u2500 previous container logs \u2500\u2500" in h.pane.banners
    assert h.pane.states[-1] == "ended"  # a finished previous stream ends cleanly


# ---------------------------------------------------------------------------
# Worker / API error visibility
# ---------------------------------------------------------------------------


async def test_api_error_notifies_and_enters_error_state() -> None:
    async def denied(namespace: str, pod: str, container: str, **_: Any) -> AsyncIterator[LogLine]:
        raise ApiStatusError(403, "forbidden")
        yield  # pragma: no cover - marks this an async generator

    h = make_harness(stream_logs=denied)
    await h.controller.open_pane("default", [("web", "main")])
    await asyncio.gather(*h.controller.tasks, return_exceptions=True)

    assert h.pane.states[-1] == "error"
    assert any(n.severity == "error" for n in h.ui.notifications)


# ---------------------------------------------------------------------------
# Display action delegation
# ---------------------------------------------------------------------------


async def test_display_toggles_replay_the_buffer() -> None:
    h = make_harness()
    await h.controller.open_pane("default", [("web", "main")])
    try:
        await h.controller.action_log_format()
        await h.controller.action_log_wrap()
        await h.controller.action_log_timestamps()

        assert h.pane.toggles == ["format", "wrap", "timestamps"]
        assert len(h.pane.replayed) == 3  # each toggle replays the buffer
    finally:
        await h.controller.cancel_tasks()


async def test_action_log_save_warns_on_empty_buffer() -> None:
    h = make_harness()
    await h.controller.open_pane("default", [("web", "main")])  # buffer exists, nothing streamed
    try:
        h.controller.action_log_save()
        assert any("empty" in n.message.lower() for n in h.ui.notifications)
    finally:
        await h.controller.cancel_tasks()


def test_action_log_save_noop_when_pane_closed() -> None:
    h = make_harness()
    h.controller.action_log_save()

    assert h.ui.notifications == []


def test_search_prev_reports_whether_it_handled_the_key() -> None:
    h = make_harness()
    assert h.controller.search_prev() is False  # closed pane defers to the caller

    h.pane.display = True
    assert h.controller.search_prev() is True
    assert h.pane.searches == ["prev"]


def test_search_next_only_acts_on_a_visible_pane() -> None:
    h = make_harness()
    h.controller.search_next()
    assert h.pane.searches == []

    h.pane.display = True
    h.controller.search_next()
    assert h.pane.searches == ["next"]


# ---------------------------------------------------------------------------
# action_logs / agent entry points
# ---------------------------------------------------------------------------


async def test_action_logs_rejects_non_pod_views() -> None:
    h = make_harness(current_kind="deployments")
    await h.controller.action_logs()

    assert h.pane.display is False
    assert any("only available for pods" in n.message for n in h.ui.notifications)


async def test_action_logs_opens_selected_pod_streams() -> None:
    h = make_harness(
        selected=("default", "web"),
        pod_containers=lambda ns, name: ("main", "sidecar"),
    )
    await h.controller.action_logs()
    try:
        assert h.controller.mode == "l"
        assert h.pane.display is True
        assert h.controller.current_triples == [
            ("default", "web", "main"),
            ("default", "web", "sidecar"),
        ]
    finally:
        await h.controller.cancel_tasks()


async def test_action_logs_refused_during_context_switch() -> None:
    h = make_harness(ctx_reads_allowed=False)
    await h.controller.action_logs()

    assert h.pane.display is False
    assert h.controller.mode == ""


async def test_open_agent_logs_sets_live_mode() -> None:
    h = make_harness()
    await h.controller.open_agent_logs("default", [("default", "web", "main")])
    try:
        assert h.controller.mode == "l"
        assert h.controller.current_triples == [("default", "web", "main")]
        assert h.controller.pane_gen == 1
    finally:
        await h.controller.cancel_tasks()


@pytest.mark.parametrize("knob", ["reconnect_sleep", "buffer_max_lines"])
def test_public_tuning_knobs_are_writable(knob: str) -> None:
    h = make_harness()
    setattr(h.controller, knob, 0)
    assert getattr(h.controller, knob) == 0
