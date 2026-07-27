"""Tests for Task 9: LogPane + 2-pane split + merged multi-container streams."""

from __future__ import annotations

import asyncio
import json as _json
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.log_pane import LogPane
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PODS_META_DICT: dict[str, object] = {}


def _pod(
    name: str,
    containers: tuple[str, ...] = ("main",),
    namespace: str = "default",
    phase: str = "Running",
) -> PodSummary:
    return PodSummary(
        name=name,
        namespace=namespace,
        phase=phase,
        ready="1/1",
        restarts=0,
        node=None,
        qos="-",
        containers=containers,
    )


class MixedFakeStream:
    """Stream factory where specific containers raise an error; others end naturally."""

    def __init__(self, error_containers: set[str], error: ApiStatusError) -> None:
        self.error_containers = error_containers
        self.error = error
        self.closed: dict[str, bool] = {}

    async def __call__(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncGenerator[LogLine, None]:
        key = f"{pod}/{container}"
        self.closed[key] = False
        try:
            if container in self.error_containers:
                raise self.error
            yield LogLine(pod=pod, container=container, text="line0")
            # end naturally
        finally:
            self.closed[key] = True


class FakeStream:
    """Controllable async generator factory for testing log streaming."""

    def __init__(self, lines_per_call: int = 1, error: ApiStatusError | None = None) -> None:
        self.lines_per_call = lines_per_call
        self.error = error
        self.closed: dict[str, bool] = {}
        self._call_count: int = 0

    def is_closed(self, pod: str, container: str) -> bool:
        return self.closed.get(f"{pod}/{container}", False)

    async def __call__(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncGenerator[LogLine, None]:
        key = f"{pod}/{container}"
        self.closed[key] = False
        self._call_count += 1
        try:
            if self.error is not None:
                raise self.error
            for i in range(self.lines_per_call):
                yield LogLine(pod=pod, container=container, text=f"line{i}")
            # Block live streams until cancelled; return immediately for previous logs.
            if follow:
                await asyncio.Event().wait()
        finally:
            self.closed[key] = True

    async def returning(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncGenerator[LogLine, None]:
        """Variant that always returns immediately (used for previous-logs tests)."""
        key = f"{pod}/{container}"
        self.closed[key] = False
        try:
            for i in range(self.lines_per_call):
                yield LogLine(pod=pod, container=container, text=f"line{i}")
            # return immediately (stream ended)
        finally:
            self.closed[key] = True


def make_app(
    pods: list[PodSummary],
    stream_logs: object = None,
) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, object]]:
        for p in pods:
            yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),  # type: ignore[arg-type]
        stream_logs=stream_logs,  # type: ignore[arg-type]
    )


def _richlog_text(app: KorvidApp) -> str:
    """Concatenate rendered lines of every visible panel for assertion."""
    from textual.widgets import RichLog

    pane = app.query_one(LogPane)
    parts = []
    for i in range(len(pane._panel_keys)):
        rich_log = pane.query_one(f"#log-panel-{i}").query_one(RichLog)
        parts.append("\n".join(strip.text for strip in rich_log.lines))
    return "\n".join(parts)


def _panel_texts(app: KorvidApp) -> dict[str, str]:
    """Map each visible panel's source key to its RichLog text."""
    from textual.widgets import RichLog

    pane = app.query_one(LogPane)
    result: dict[str, str] = {}
    for i, key in enumerate(pane._panel_keys):
        rich_log = pane.query_one(f"#log-panel-{i}").query_one(RichLog)
        result[key] = "\n".join(strip.text for strip in rich_log.lines)
    return result


def _titles_visible(app: KorvidApp) -> bool:
    """Whether panel title bars are shown (source attribution)."""
    from textual.widgets import Static

    pane = app.query_one(LogPane)
    if not pane._panel_keys:
        return False
    title = pane.query_one("#log-panel-0").query_one(".panel-title", Static)
    return bool(title.display)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_l_on_non_pods_kind_warns() -> None:
    """Pressing l when kind != pods shows a warning notification."""
    app = make_app([])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        # Switch to a non-pods kind via filter_pattern hack (simplest)
        app.current_kind = "deployments"
        await pilot.press("l")
        await pilot.pause(0.05)
        msgs = [n.message for n in app._notifications]
        assert any("pod" in m.lower() for m in msgs)


async def test_l_on_empty_table_warns() -> None:
    """Pressing l with an empty table shows a warning notification."""
    fake = FakeStream()
    app = make_app([], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.05)
        msgs = [n.message for n in app._notifications]
        assert any("select" in m.lower() or "no" in m.lower() for m in msgs)


async def test_l_without_stream_logs_warns() -> None:
    """Pressing l when stream_logs is None shows a warning notification."""
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.05)
        msgs = [n.message for n in app._notifications]
        assert any("unavailable" in m.lower() for m in msgs)


async def test_l_opens_pane_for_selected_pod() -> None:
    """l opens the log pane for the selected pod."""
    fake = FakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.1)
        assert app.query_one(LogPane).display is True


async def test_l_multi_container_split_panels() -> None:
    """Multi-container pod: one panel per container, each with its own lines."""
    fake = FakeStream(lines_per_call=1)
    app = make_app(
        [_pod("myapp", containers=("main", "sidecar"))],
        stream_logs=fake,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.2)  # let both stream tasks yield their lines
        panels = _panel_texts(app)
        assert set(panels) == {"myapp/main", "myapp/sidecar"}
        assert "line0" in panels["myapp/main"]
        assert "line0" in panels["myapp/sidecar"]
        assert _titles_visible(app) is True


async def test_l_single_container_no_title() -> None:
    """Single-container pod: one panel, no title bar needed."""
    fake = FakeStream(lines_per_call=1)
    app = make_app(
        [_pod("myapp", containers=("main",))],
        stream_logs=fake,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.2)
        text = _richlog_text(app)
        assert "line0" in text
        assert _titles_visible(app) is False


async def test_l_again_closes_pane_and_cancels() -> None:
    """Pressing l again closes the pane and cancels the stream tasks."""
    fake = FakeStream(lines_per_call=1)
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.1)
        assert app.query_one(LogPane).display is True
        # Press l again to close
        await pilot.press("l")
        await pilot.pause(0.2)
        assert app.query_one(LogPane).display is False
        # Stream should have been closed/cancelled
        assert fake.is_closed("myapp", "main") is True


async def test_escape_closes_pane_when_bars_closed() -> None:
    """Pressing Escape closes the log pane when no bars are open."""
    fake = FakeStream(lines_per_call=1)
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.1)
        assert app.query_one(LogPane).display is True
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app.query_one(LogPane).display is False
        assert fake.is_closed("myapp", "main") is True


async def test_L_streams_multiple_pods_with_prefix() -> None:
    """Shift+L streams all filtered pods and shows [pod/ctr] prefix."""
    fake = FakeStream(lines_per_call=1)
    app = make_app(
        [
            _pod("app-alpha", containers=("main",)),
            _pod("app-beta", containers=("main",)),
            _pod("other", containers=("main",)),
        ],
        stream_logs=fake,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        # Filter to only "app-" pods
        await pilot.press("slash")
        for ch in "app":
            await pilot.press(ch)
        await pilot.pause(0.1)
        # Two pods visible: app-alpha, app-beta
        table = app.query_one(ResourceTable)
        assert table.row_count == 2
        await pilot.press("shift+l")
        await pilot.pause(0.2)
        assert app.query_one(LogPane).display is True
        panels = _panel_texts(app)
        assert set(panels) == {"app-alpha/main", "app-beta/main"}
        assert "line0" in panels["app-alpha/main"]
        assert "line0" in panels["app-beta/main"]
        assert _titles_visible(app) is True


async def test_L_caps_at_8_pods_and_notifies() -> None:
    """Shift+L caps at 8 pods when more match and shows a notification."""
    fake = FakeStream(lines_per_call=1)
    pods = [_pod(f"app-{i:02d}", containers=("main",)) for i in range(10)]
    app = make_app(pods, stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        assert table.row_count == 10
        await pilot.press("shift+l")
        await pilot.pause(0.2)
        # Only 8 tasks should be running
        assert len(app._log_tasks) <= 8
        # Notification about capping
        msgs = [n.message for n in app._notifications]
        assert any("8" in m for m in msgs)


async def test_api_error_sets_error_state_and_notifies() -> None:
    """An ApiStatusError from the stream sets error state and shows notification."""
    error = ApiStatusError(403, "Forbidden")
    fake = FakeStream(error=error)
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.3)
        msgs = [n.message for n in app._notifications]
        assert any("RBAC" in m or "permission" in m.lower() or "403" in m for m in msgs)


async def test_switching_kind_closes_pane() -> None:
    """Navigating to a different kind closes the log pane."""
    fake = FakeStream(lines_per_call=1)
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.1)
        assert app.query_one(LogPane).display is True
        # Navigate away via colon command
        await pilot.press("colon")
        for ch in "ns kube-system":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.query_one(LogPane).display is False
        assert fake.is_closed("myapp", "main") is True


async def test_stream_ended_sets_ended_state() -> None:
    """When a previous-logs stream ends naturally, the pane state becomes 'ended'."""
    fake = FakeStream(lines_per_call=1)
    # FakeStream blocks for follow=True (live) but returns for follow=False (previous).
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")  # live stream opens and stays alive
        await pilot.pause(0.1)
        await pilot.press("p")  # previous logs: yields 1 line, returns → "ended"
        await pilot.pause(0.3)
        log_pane = app.query_one(LogPane)
        assert log_pane.display is True
        # Header should mention "ended"
        header_text = str(log_pane.query_one("#log-header").render())
        assert "ended" in header_text


# ---------------------------------------------------------------------------
# Fix round 1: Important 1 — L with single pod must always show prefix
# ---------------------------------------------------------------------------


async def test_L_single_pod_always_shows_prefix() -> None:
    """L with a filter matching exactly 1 single-container pod still shows prefix."""
    fake = FakeStream(lines_per_call=1)
    app = make_app(
        [
            _pod("app-only", containers=("main",)),
            _pod("other", containers=("main",)),
        ],
        stream_logs=fake,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        # Filter to exactly 1 pod
        await pilot.press("slash")
        for ch in "app-only":
            await pilot.press(ch)
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        assert table.row_count == 1
        await pilot.press("shift+l")
        await pilot.pause(0.2)
        # Title must be shown even with only 1 visible pod (L path)
        panels = _panel_texts(app)
        assert set(panels) == {"app-only/main"}
        assert "line0" in panels["app-only/main"]
        assert _titles_visible(app) is True


# ---------------------------------------------------------------------------
# Fix round 1: Important 2 — errored task discarded; error state not overwritten
# ---------------------------------------------------------------------------


async def test_error_task_discarded_state_stays_error() -> None:
    """One container errors, other ends naturally: state=error, _log_tasks empty."""
    error = ApiStatusError(403, "Forbidden")
    mixed = MixedFakeStream(error_containers={"sidecar"}, error=error)
    app = make_app(
        [_pod("myapp", containers=("main", "sidecar"))],
        stream_logs=mixed,
    )
    app._reconnect_sleep = 0.0  # speed up reconnect cycle in test
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.4)
        log_pane = app.query_one(LogPane)
        # State must remain "error" — not downgraded to "ended"
        header_text = str(log_pane.query_one("#log-header").render())
        assert "error" in header_text
        assert "ended" not in header_text
        # No task leak: all tasks must have been discarded
        assert len(app._log_tasks) == 0


# ---------------------------------------------------------------------------
# Fix round 1: Minor — L on non-pods kind warns and spawns no tasks
# ---------------------------------------------------------------------------


async def test_L_on_non_pods_kind_warns_no_tasks() -> None:
    """Shift+L when kind != pods shows warning and spawns no stream tasks."""
    fake = FakeStream(lines_per_call=1)
    app = make_app([_pod("myapp")], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.current_kind = "deployments"
        await pilot.press("shift+l")
        await pilot.pause(0.05)
        msgs = [n.message for n in app._notifications]
        assert any("pod" in m.lower() for m in msgs)
        assert len(app._log_tasks) == 0


# ---------------------------------------------------------------------------
# Fix round 1: Minor — L again while multi-stream open cancels old tasks
# ---------------------------------------------------------------------------


async def test_L_reopen_cancels_old_tasks_new_stream_opens() -> None:
    """Shift+L while streams are open cancels old tasks and opens a fresh stream."""
    fake = FakeStream(lines_per_call=1)
    app = make_app(
        [
            _pod("app-alpha", containers=("main",)),
            _pod("app-beta", containers=("main",)),
        ],
        stream_logs=fake,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("shift+l")
        await pilot.pause(0.1)
        assert app.query_one(LogPane).display is True
        first_tasks = set(app._log_tasks)
        # Press Shift+L again to re-open
        await pilot.press("shift+l")
        await pilot.pause(0.2)
        assert app.query_one(LogPane).display is True
        # All old tasks should be done (cancelled)
        for t in first_tasks:
            assert t.done()
        # New tasks are running
        assert len(app._log_tasks) > 0


# ---------------------------------------------------------------------------
# Task 10: JSON toggle (f), previous logs (p), search n/N
# ---------------------------------------------------------------------------


class JsonFakeStream:
    """Stream that yields one JSON log line and then blocks (for f/format tests)."""

    def __init__(self) -> None:
        self.line = _json.dumps({"level": "info", "msg": "greeting"})

    async def __call__(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncGenerator[LogLine, None]:
        yield LogLine(pod=pod, container=container, text=self.line)
        if follow:
            await asyncio.Event().wait()


class RecordingFakeStream:
    """Records every call's kwargs; yields one line and blocks unless follow=False."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncGenerator[LogLine, None]:
        self.calls.append({"previous": previous, "follow": follow})
        yield LogLine(pod=pod, container=container, text="initial-line")
        if follow:
            await asyncio.Event().wait()


class SearchFakeStream:
    """Yields 5 lines (3 containing 'findme') then blocks until cancelled."""

    async def __call__(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncGenerator[LogLine, None]:
        texts = ["findme-0", "normal-1", "findme-2", "normal-3", "findme-4"]
        for t in texts:
            yield LogLine(pod=pod, container=container, text=t)
        if follow:
            await asyncio.Event().wait()


def _header_text(app: KorvidApp) -> str:
    return str(app.query_one(LogPane).query_one("#log-header").render())


async def test_f_toggles_format_and_rerenders() -> None:
    """f flips header [json]<->[raw] and re-renders the buffer lines."""
    stream = JsonFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.15)

        # Initially formatted: header shows [json], rendered shows values not keys
        header = _header_text(app)
        assert "[json]" in header
        text_before = _richlog_text(app)
        assert "greeting" in text_before
        assert '"level"' not in text_before  # key not visible, only value

        # Toggle to raw
        await pilot.press("f")
        await pilot.pause(0.05)

        header2 = _header_text(app)
        assert "[raw]" in header2
        text_after = _richlog_text(app)
        assert '"level"' in text_after  # raw JSON shows the key

        # Toggle back to json
        await pilot.press("f")
        await pilot.pause(0.05)
        header3 = _header_text(app)
        assert "[json]" in header3


async def test_p_reopens_with_previous_true() -> None:
    """p cancels live streams and re-opens with previous=True; banner appears."""
    recording = RecordingFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=recording)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.15)
        assert app.query_one(LogPane).display is True

        # First call should be live (previous=False)
        assert recording.calls[0]["previous"] is False

        await pilot.press("p")
        await pilot.pause(0.2)

        # At least one call must have previous=True
        assert any(c["previous"] is True for c in recording.calls)
        # Banner must appear in the log output
        text = _richlog_text(app)
        assert "previous" in text


async def test_p_follow_false_for_previous_stream() -> None:
    """When p is pressed, the new stream is called with follow=False."""
    recording = RecordingFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=recording)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.15)
        await pilot.press("p")
        await pilot.pause(0.2)
        previous_calls = [c for c in recording.calls if c["previous"] is True]
        assert previous_calls, "expected at least one call with previous=True"
        assert all(c["follow"] is False for c in previous_calls)


async def test_search_shows_counter_and_n_advances() -> None:
    """/ + pattern + Enter shows counter; n advances the hit index."""
    stream = SearchFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.2)

        # Open inline search via slash
        await pilot.press("slash")
        await pilot.pause(0.05)

        # Type pattern and submit
        for ch in "findme":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.05)

        # Counter should appear: 1/3 (3 lines contain "findme")
        header = _header_text(app)
        assert "1/3" in header

        # n advances to second hit
        await pilot.press("n")
        await pilot.pause(0.05)
        header2 = _header_text(app)
        assert "2/3" in header2

        # n again to third hit
        await pilot.press("n")
        await pilot.pause(0.05)
        header3 = _header_text(app)
        assert "3/3" in header3


async def test_search_N_navigates_backwards_with_wraparound() -> None:
    """N goes to the previous hit; from the first hit it wraps to the last."""
    stream = SearchFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.2)

        await pilot.press("slash")
        await pilot.pause(0.05)
        for ch in "findme":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert "1/3" in _header_text(app)

        # N from the first hit wraps to the last
        await pilot.press("shift+n")
        await pilot.pause(0.05)
        assert "3/3" in _header_text(app)

        # N again steps back to the second hit
        await pilot.press("shift+n")
        await pilot.pause(0.05)
        assert "2/3" in _header_text(app)


async def test_search_escape_clears_stale_counter() -> None:
    """Escaping the search input clears the hit counter from the header."""
    stream = SearchFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.2)

        await pilot.press("slash")
        await pilot.pause(0.05)
        for ch in "findme":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert "1/3" in _header_text(app)

        # Re-open search and dismiss without submitting: counter must clear
        await pilot.press("slash")
        await pilot.pause(0.05)
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert "1/3" not in _header_text(app)


async def test_search_scroll_offsets_for_banner_lines() -> None:
    """Search scroll targets account for banner lines RichLog has but the buffer lacks."""
    from textual.widgets import RichLog

    stream = SearchFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.2)
        # p re-opens as a previous stream, which prepends a banner line to RichLog
        # only (not the buffer), shifting all hit lines down by one.
        await pilot.press("p")
        await pilot.pause(0.2)

        rich_log = app.query_one(LogPane).query_one(RichLog)
        scrolled: list[float] = []
        original_scroll = rich_log.scroll_to

        def _capture(*args: Any, **kwargs: Any) -> None:
            scrolled.append(float(kwargs["y"]))
            original_scroll(*args, **kwargs)

        rich_log.scroll_to = _capture  # type: ignore[method-assign]

        await pilot.press("slash")
        await pilot.pause(0.05)
        for ch in "findme":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.05)

        # Buffer hit index 0 ("findme-0") sits at RichLog line 1 (after banner).
        assert scrolled == [1]


async def test_f_closed_no_crash() -> None:
    """f when pane closed produces no error and no state change."""
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert app.query_one(LogPane).display is False
        await pilot.press("f")
        await pilot.pause(0.05)
        assert app.query_one(LogPane).display is False


async def test_p_closed_no_crash() -> None:
    """p when pane closed produces no error and no state change."""
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert app.query_one(LogPane).display is False
        await pilot.press("p")
        await pilot.pause(0.05)
        assert app.query_one(LogPane).display is False


async def test_n_closed_no_crash() -> None:
    """n when pane closed produces no error."""
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert app.query_one(LogPane).display is False
        await pilot.press("n")
        await pilot.pause(0.05)
        assert app.query_one(LogPane).display is False


async def test_p_unexpected_error_sets_error_state() -> None:
    """A non-API failure in the previous-logs stream surfaces as an error, not 'streaming'."""

    class BoomPreviousStream:
        async def __call__(
            self,
            namespace: str,
            pod: str,
            container: str,
            *,
            previous: bool = False,
            follow: bool = True,
            tail_lines: int = 200,
        ) -> AsyncGenerator[LogLine, None]:
            if previous:
                raise RuntimeError("transport exploded")
            yield LogLine(pod=pod, container=container, text="live-line")
            if follow:
                await asyncio.Event().wait()

    app = make_app([_pod("myapp", containers=("main",))], stream_logs=BoomPreviousStream())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.15)
        await pilot.press("p")
        await pilot.pause(0.2)

        log_pane = app.query_one(LogPane)
        assert "error" in log_pane._state
        assert not app._log_tasks  # failed task must be discarded
        assert any("Log stream error" in (n.title or "") for n in app._notifications)


async def test_open_bounds_richlog_to_buffer_capacity() -> None:
    """RichLog must be bounded when a buffer is attached (memory safety)."""
    from textual.widgets import RichLog

    app = make_app([_pod("myapp", containers=("main",))], stream_logs=FakeStream())
    app._log_buffer_max_lines = 10
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.15)
        rich_log = app.query_one(LogPane).query_one(RichLog)
        assert rich_log.max_lines == 18  # buffer cap + banner headroom


# ---------------------------------------------------------------------------
# Accumulating pod logs with repeated ``l`` (max 4 pods)
# ---------------------------------------------------------------------------


async def test_l_on_second_pod_adds_side_by_side() -> None:
    """l on pod A, then l on pod B shows both pods' panels."""
    fake = FakeStream(lines_per_call=1)
    app = make_app(
        [_pod("app-a", containers=("main",)), _pod("app-b", containers=("main",))],
        stream_logs=fake,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.15)
        await pilot.press("down")
        await pilot.press("l")
        await pilot.pause(0.2)
        panels = _panel_texts(app)
        assert set(panels) == {"app-a/main", "app-b/main"}
        assert "line0" in panels["app-a/main"]
        assert "line0" in panels["app-b/main"]
        assert _titles_visible(app) is True


async def test_l_on_shown_pod_removes_it() -> None:
    """l on a pod already in the pane removes only that pod's panels."""
    fake = FakeStream(lines_per_call=1)
    app = make_app(
        [_pod("app-a", containers=("main",)), _pod("app-b", containers=("main",))],
        stream_logs=fake,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.15)
        await pilot.press("down")
        await pilot.press("l")
        await pilot.pause(0.2)
        # Cursor still on app-b: pressing l again removes app-b only.
        await pilot.press("l")
        await pilot.pause(0.2)
        panels = _panel_texts(app)
        assert set(panels) == {"app-a/main"}
        assert app.query_one(LogPane).display is True


async def test_l_removing_last_pod_closes_pane() -> None:
    """l on the only shown pod closes the pane (toggle parity)."""
    fake = FakeStream(lines_per_call=1)
    app = make_app([_pod("app-a", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.15)
        await pilot.press("l")
        await pilot.pause(0.2)
        assert app.query_one(LogPane).display is False
        assert not app._log_tasks


async def test_l_caps_accumulation_at_4_pods() -> None:
    """A fifth pod is rejected with a warning; the four stay open."""
    fake = FakeStream(lines_per_call=1)
    pods = [_pod(f"app-{i}", containers=("main",)) for i in range(5)]
    app = make_app(pods, stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.1)
        for _ in range(4):
            await pilot.press("down")
            await pilot.press("l")
            await pilot.pause(0.15)
        panels = _panel_texts(app)
        assert len(panels) == 4
        msgs = [n.message for n in app._notifications]
        assert any("4" in m for m in msgs)


async def test_l_in_multi_stream_mode_closes_pane() -> None:
    """After L (multi-stream), l does not accumulate — it closes the pane."""
    fake = FakeStream(lines_per_call=1)
    app = make_app(
        [_pod("app-a", containers=("main",)), _pod("app-b", containers=("main",))],
        stream_logs=fake,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("shift+l")
        await pilot.pause(0.2)
        assert app.query_one(LogPane).display is True
        await pilot.press("l")
        await pilot.pause(0.2)
        assert app.query_one(LogPane).display is False


async def test_straggler_line_from_removed_source_is_dropped() -> None:
    """A line from a source not in the pane must not land in another panel."""
    fake = FakeStream(lines_per_call=1)
    app = make_app([_pod("app-a", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.15)
        pane = app.query_one(LogPane)
        pane.feed(LogLine(pod="ghost", container="old", text="stray-line"))
        await pilot.pause(0.05)
        assert "stray-line" not in _richlog_text(app)


# ---------------------------------------------------------------------------
# Copilot review: reconnect must not duplicate the replayed tail lines
# ---------------------------------------------------------------------------


class ReconnectingFakeStream:
    """First call yields 2 timestamped lines then errors; second call replays
    both lines (the API tail) plus one new line, then blocks."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncGenerator[LogLine, None]:
        def ts(second: int) -> datetime:
            return datetime(2024, 1, 1, 0, 0, second, tzinfo=UTC)

        self.calls += 1
        if self.calls == 1:
            yield LogLine(pod=pod, container=container, text="one", timestamp=ts(1))
            yield LogLine(pod=pod, container=container, text="two", timestamp=ts(2))
            raise RuntimeError("connection reset")
        # Reconnect: API replays the tail before following.
        yield LogLine(pod=pod, container=container, text="one", timestamp=ts(1))
        yield LogLine(pod=pod, container=container, text="two", timestamp=ts(2))
        yield LogLine(pod=pod, container=container, text="three", timestamp=ts(3))
        await asyncio.Event().wait()


async def test_reconnect_drops_replayed_tail_lines() -> None:
    """Lines at or before the last displayed timestamp are dropped on reconnect."""
    fake = ReconnectingFakeStream()
    app = make_app([_pod("app-a", containers=("main",))], stream_logs=fake)
    app._reconnect_sleep = 0.0
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.4)
        text = _richlog_text(app)
        assert fake.calls == 2
        assert text.count("one") == 1
        assert text.count("two") == 1
        assert "three" in text


class SameTimestampReconnectStream:
    """All lines share one (microsecond-truncated) timestamp.  First call
    yields 2 lines then errors; the reconnect replays both and follows with a
    *new* line stamped identically — it must not be dropped."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncGenerator[LogLine, None]:
        ts = datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC)
        self.calls += 1
        if self.calls == 1:
            yield LogLine(pod=pod, container=container, text="one", timestamp=ts)
            yield LogLine(pod=pod, container=container, text="two", timestamp=ts)
            raise RuntimeError("connection reset")
        yield LogLine(pod=pod, container=container, text="one", timestamp=ts)
        yield LogLine(pod=pod, container=container, text="two", timestamp=ts)
        yield LogLine(pod=pod, container=container, text="three", timestamp=ts)
        await asyncio.Event().wait()


async def test_reconnect_keeps_new_line_with_equal_timestamp() -> None:
    """A new line sharing the last displayed timestamp survives a reconnect."""
    fake = SameTimestampReconnectStream()
    app = make_app([_pod("app-a", containers=("main",))], stream_logs=fake)
    app._reconnect_sleep = 0.0
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.4)
        text = _richlog_text(app)
        assert fake.calls == 2
        assert text.count("one") == 1
        assert text.count("two") == 1
        assert text.count("three") == 1


# ---------------------------------------------------------------------------
# Copilot review: never spawn more streams than the pane has panels
# ---------------------------------------------------------------------------


async def test_open_log_pane_caps_spawned_streams_at_max_panels() -> None:
    """A pod with more containers than MAX_PANELS spawns exactly MAX_PANELS tasks."""
    from korvid.ui.widgets.log_pane import MAX_PANELS

    fake = FakeStream(lines_per_call=1)
    containers = tuple(f"c{i}" for i in range(MAX_PANELS + 3))
    app = make_app([_pod("bigpod", containers=containers)], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.2)
        assert len(app._log_tasks) == MAX_PANELS
        assert len(app._current_log_triples) == MAX_PANELS
        panels = _panel_texts(app)
        assert len(panels) == MAX_PANELS
        msgs = [n.message for n in app._notifications]
        assert any(str(MAX_PANELS) in m for m in msgs)


# ---------------------------------------------------------------------------
# Issue #43: wrap toggle (w)
# ---------------------------------------------------------------------------


def _panel_richlogs(app: KorvidApp) -> list[Any]:
    """Visible panels' RichLog widgets."""
    from textual.widgets import RichLog

    pane = app.query_one(LogPane)
    return [
        pane.query_one(f"#log-panel-{i}").query_one(RichLog) for i in range(len(pane._panel_keys))
    ]


async def test_w_toggles_wrap_on_panels_and_header() -> None:
    """w flips RichLog.wrap on every visible panel and tags the header."""
    stream = JsonFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(pilot, lambda: "greeting" in _richlog_text(app), label="first line")

        assert all(rl.wrap is False for rl in _panel_richlogs(app))
        assert "[wrap]" not in _header_text(app)

        await pilot.press("w")
        await until(pilot, lambda: "[wrap]" in _header_text(app), label="wrap tag on")
        assert all(rl.wrap is True for rl in _panel_richlogs(app))
        # Buffer content survives the wrap re-render.
        assert "greeting" in _richlog_text(app)

        await pilot.press("w")
        await until(pilot, lambda: "[wrap]" not in _header_text(app), label="wrap tag off")
        assert all(rl.wrap is False for rl in _panel_richlogs(app))


async def test_w_closed_no_crash() -> None:
    """Pressing w with the pane closed is a no-op."""
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("w")
        await pilot.pause()
        assert app.query_one(LogPane).display is False


async def test_wrap_persists_across_reopen() -> None:
    """The wrap setting survives closing and reopening the pane (session-scoped)."""
    stream = JsonFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(pilot, lambda: app.query_one(LogPane).display, label="pane open")
        await pilot.press("w")
        await until(pilot, lambda: "[wrap]" in _header_text(app), label="wrap tag on")
        await pilot.press("l")  # close
        await until(pilot, lambda: not app.query_one(LogPane).display, label="pane closed")
        await pilot.press("l")  # reopen
        await until(pilot, lambda: app.query_one(LogPane).display, label="pane open")
        assert all(rl.wrap is True for rl in _panel_richlogs(app))
        assert "[wrap]" in _header_text(app)


class WrapScrollFakeStream:
    """Yields three long (wrapping) lines around one short 'findme' line."""

    async def __call__(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncGenerator[LogLine, None]:
        texts = ["a" * 300, "findme", "b" * 300, "c" * 300]
        for t in texts:
            yield LogLine(pod=pod, container=container, text=t)
        if follow:
            await asyncio.Event().wait()


async def test_search_scroll_accounts_for_wrapped_rows() -> None:
    """With wrap on, search scrolls by display rows, not logical line indexes."""
    from textual.widgets import RichLog

    stream = WrapScrollFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(pilot, lambda: "findme" in _richlog_text(app), label="lines fed")
        await pilot.press("w")
        await until(pilot, lambda: "[wrap]" in _header_text(app), label="wrap tag on")

        rich_log = app.query_one(LogPane).query_one(RichLog)
        # The three identical long lines wrap to the same height; findme is 1 row.
        long_rows = (len(rich_log.lines) - 1) // 3
        assert long_rows >= 2, "long lines must wrap for this test to be meaningful"

        scrolled: list[float] = []
        original_scroll = rich_log.scroll_to

        def _capture(*args: Any, **kwargs: Any) -> None:
            scrolled.append(float(kwargs["y"]))
            original_scroll(*args, **kwargs)

        rich_log.scroll_to = _capture  # type: ignore[method-assign]

        await pilot.press("slash")
        await pilot.pause()
        for ch in "findme":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()

        # The hit is buffered line 1 but sits below the wrapped rows of line 0.
        assert scrolled == [long_rows]


# ---------------------------------------------------------------------------
# Issue #43: timestamps toggle (t)
# ---------------------------------------------------------------------------


class TimestampFakeStream:
    """Yields one line with a kubelet timestamp, one without, then blocks."""

    STAMP = datetime(2026, 7, 26, 10, 30, 45, tzinfo=UTC)

    async def __call__(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncGenerator[LogLine, None]:
        yield LogLine(
            pod=pod,
            container=container,
            text="stamped-line",
            timestamp=self.STAMP,
        )
        yield LogLine(pod=pod, container=container, text="bare-line")
        if follow:
            await asyncio.Event().wait()


# Timestamps display in the user's local timezone, not kubelet UTC.
_STAMP_LOCAL = TimestampFakeStream.STAMP.astimezone().strftime("%H:%M:%S")


async def test_t_toggles_timestamp_prefix() -> None:
    """t prefixes lines with HH:MM:SS from the parsed kubelet timestamp."""
    stream = TimestampFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(pilot, lambda: "bare-line" in _richlog_text(app), label="lines fed")

        assert _STAMP_LOCAL not in _richlog_text(app)
        assert "[ts]" not in _header_text(app)

        await pilot.press("t")
        await until(pilot, lambda: "[ts]" in _header_text(app), label="ts tag on")
        text = _richlog_text(app)
        assert f"{_STAMP_LOCAL} stamped-line" in text
        # Lines without a parsed timestamp render unchanged.
        assert "bare-line" in text
        assert f"{_STAMP_LOCAL} bare-line" not in text

        await pilot.press("t")
        await until(pilot, lambda: "[ts]" not in _header_text(app), label="ts tag off")
        assert _STAMP_LOCAL not in _richlog_text(app)


async def test_timestamp_prefix_dims_only_the_timestamp() -> None:
    """The dim style covers only the HH:MM:SS prefix, not the log body."""
    from textual.widgets import RichLog

    stream = TimestampFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(pilot, lambda: "bare-line" in _richlog_text(app), label="lines fed")
        await pilot.press("t")
        await until(
            pilot,
            lambda: f"{_STAMP_LOCAL} stamped-line" in _richlog_text(app),
            label="stamped line",
        )

        rich_log = app.query_one(LogPane).query_one(RichLog)
        strip = next(s for s in rich_log.lines if "stamped-line" in "".join(seg.text for seg in s))
        body = next(seg for seg in strip if "stamped-line" in seg.text)
        assert not (body.style is not None and body.style.dim)
        prefix = next(seg for seg in strip if _STAMP_LOCAL in seg.text)
        assert prefix.style is not None
        assert prefix.style.dim


async def test_t_closed_no_crash() -> None:
    """Pressing t with the pane closed is a no-op."""
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("t")
        await pilot.pause()
        assert app.query_one(LogPane).display is False


async def test_timestamps_persist_across_reopen() -> None:
    """The timestamp setting survives closing and reopening the pane."""
    stream = TimestampFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(pilot, lambda: app.query_one(LogPane).display, label="pane open")
        await pilot.press("t")
        await until(pilot, lambda: "[ts]" in _header_text(app), label="ts tag on")
        await pilot.press("l")  # close
        await until(pilot, lambda: not app.query_one(LogPane).display, label="pane closed")
        await pilot.press("l")  # reopen
        await until(
            pilot,
            lambda: f"{_STAMP_LOCAL} stamped-line" in _richlog_text(app),
            label="stamped line after reopen",
        )
        assert "[ts]" in _header_text(app)


# ---------------------------------------------------------------------------
# Issue #43: save buffer to file (ctrl+s)
# ---------------------------------------------------------------------------


async def test_ctrl_s_saves_buffer_and_notifies(monkeypatch: Any, tmp_path: Any) -> None:
    """ctrl+s writes the buffer under the export dir and toasts the path."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    stream = JsonFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(pilot, lambda: "greeting" in _richlog_text(app), label="line buffered")

        await pilot.press("ctrl+s")
        await until(
            pilot,
            lambda: list((tmp_path / "korvid" / "logs").glob("korvid-myapp-*.log")),
            label="file saved",
        )

        saved = list((tmp_path / "korvid" / "logs").glob("korvid-myapp-*.log"))
        assert len(saved) == 1
        assert "greeting" in saved[0].read_text()
        msgs = [n.message for n in app._notifications]
        assert any(str(saved[0]) in m for m in msgs)


async def test_ctrl_s_closed_no_crash(monkeypatch: Any, tmp_path: Any) -> None:
    """ctrl+s with the pane closed writes nothing."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not (tmp_path / "korvid" / "logs").exists()


async def test_ctrl_s_write_failure_notifies_error(monkeypatch: Any, tmp_path: Any) -> None:
    """An OSError during export surfaces as an error toast, not a crash."""
    blocker = tmp_path / "blocked"
    blocker.write_text("")  # a file where a directory is needed
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))
    stream = JsonFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=stream)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(pilot, lambda: "greeting" in _richlog_text(app), label="line buffered")

        await pilot.press("ctrl+s")
        await until(
            pilot,
            lambda: any(n.severity == "error" for n in app._notifications),
            label="error toast",
        )

        errors = [n for n in app._notifications if n.severity == "error"]
        assert any("save" in n.message.lower() for n in errors)


async def test_config_seeds_wrap_and_timestamp_defaults() -> None:
    """logs.wrap / logs.timestamps config defaults apply to the pane."""
    stream = TimestampFakeStream()
    store = ResourceStore()
    pods = [_pod("myapp", containers=("main",))]

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, object]]:
        for p in pods:
            yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    app = KorvidApp(
        config=KorvidConfig(namespace="default", log_wrap=True, log_timestamps=True),
        store=store,
        watch_manager=WatchManager(store, source),  # type: ignore[arg-type]
        stream_logs=stream,
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(
            pilot,
            lambda: f"{_STAMP_LOCAL} stamped-line" in _richlog_text(app),
            label="stamped line",
        )
        assert all(rl.wrap is True for rl in _panel_richlogs(app))
        header = _header_text(app)
        assert "[wrap]" in header
        assert "[ts]" in header


async def test_wrap_toggle_preserves_previous_banner() -> None:
    """Toggling wrap in previous-log mode keeps the contextual banner."""
    recording = RecordingFakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=recording)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(pilot, lambda: app.query_one(LogPane).display, label="pane open")
        await pilot.press("p")
        await until(pilot, lambda: "previous" in _richlog_text(app), label="previous banner")

        await pilot.press("w")
        await until(
            pilot,
            lambda: "[wrap]" in _header_text(app),
            label="wrap tag after toggle",
        )
        assert "previous" in _richlog_text(app)


async def test_timestamp_toggle_preserves_overflow_banner() -> None:
    """Toggling timestamps after a buffer overflow keeps the overflow banner."""
    store = ResourceStore()
    pods = [_pod("myapp", containers=("main",))]

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, object]]:
        for p in pods:
            yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    app = KorvidApp(
        config=KorvidConfig(namespace="default", log_buffer_lines=1),
        store=store,
        watch_manager=WatchManager(store, source),  # type: ignore[arg-type]
        stream_logs=_overflow_stream,
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod row")
        await pilot.press("l")
        await until(pilot, lambda: "overflowed" in _richlog_text(app), label="overflow banner")

        await pilot.press("t")
        await until(
            pilot,
            lambda: "[ts]" in _header_text(app),
            label="ts tag after toggle",
        )
        assert "overflowed" in _richlog_text(app)


async def _overflow_stream(
    namespace: str,
    pod: str,
    container: str,
    *,
    previous: bool = False,
    follow: bool = True,
    tail_lines: int = 200,
) -> AsyncGenerator[LogLine, None]:
    for i in range(3):  # 3 lines into a 1-line buffer forces overflow
        yield LogLine(pod=pod, container=container, text=f"line{i}")
    if follow:
        await asyncio.Event().wait()


async def test_l_refused_while_context_switching() -> None:
    """`l` during a :ctx switch must not spawn streams: they would attach to
    whichever cluster wins the swap while labeled with the old selection
    (issue #84)."""
    fake = FakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._ctx_switching = True
        try:
            await pilot.press("l")
            await pilot.pause(0.05)
        finally:
            app._ctx_switching = False
        assert app.query_one(LogPane).display is False
        msgs = [n.message for n in app._notifications]
        assert any("context switch is in progress" in m for m in msgs)


async def test_multi_logs_refused_while_context_switching() -> None:
    """`L` (multi-stream) during a :ctx switch is refused up front (issue #84)."""
    fake = FakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._ctx_switching = True
        try:
            await pilot.press("L")
            await pilot.pause(0.05)
        finally:
            app._ctx_switching = False
        assert app.query_one(LogPane).display is False
        msgs = [n.message for n in app._notifications]
        assert any("context switch is in progress" in m for m in msgs)


async def test_previous_logs_refused_while_context_switching() -> None:
    """`p` during a :ctx switch must not respawn streams: teardown is about
    to close the pane and the re-opened tasks would race the client swap
    (issue #84)."""
    fake = FakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.1)
        assert app.query_one(LogPane).display is True
        app._ctx_switching = True
        try:
            await pilot.press("p")
            await pilot.pause(0.05)
        finally:
            app._ctx_switching = False
        assert app._log_pane_mode == "l"  # previous mode never engaged
        msgs = [n.message for n in app._notifications]
        assert any("context switch is in progress" in m for m in msgs)


async def test_open_log_pane_dropped_when_epoch_moved() -> None:
    """A :ctx switch completing inside the awaited gap between the keypress
    and the pane open must drop the open — the streams would attach to the
    new cluster labeled with the old selection (issue #84)."""
    fake = FakeStream()
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._open_log_pane("default", [("myapp", "main")], epoch=app._ctx_epoch - 1)
        await pilot.pause(0.05)
        assert app.query_one(LogPane).display is False
        assert not app._log_tasks
        msgs = [n.message for n in app._notifications]
        assert any("kube context changed" in m for m in msgs)
