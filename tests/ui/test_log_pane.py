"""Tests for Task 9: LogPane + 2-pane split + merged multi-container streams."""

from __future__ import annotations

import asyncio
import json as _json
from collections.abc import AsyncGenerator, AsyncIterator

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.log_pane import LogPane
from korvid.ui.widgets.resource_table import ResourceTable

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
            # Block until cancelled
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
        """Variant that returns immediately (stream ended naturally)."""
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
    """Concatenate all rendered RichLog lines for assertion."""
    from textual.widgets import RichLog

    rich_log = app.query_one(LogPane).query_one(RichLog)
    return "\n".join(strip.text for strip in rich_log.lines)


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


async def test_l_multi_container_prefix_in_output() -> None:
    """Multi-container pod: lines are prefixed with [pod/container]."""
    fake = FakeStream(lines_per_call=1)
    app = make_app(
        [_pod("myapp", containers=("main", "sidecar"))],
        stream_logs=fake,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
        await pilot.pause(0.2)  # let both stream tasks yield their lines
        text = _richlog_text(app)
        assert "[myapp/main]" in text
        assert "[myapp/sidecar]" in text


async def test_l_single_container_no_prefix() -> None:
    """Single-container pod: no [pod/container] prefix in output."""
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
        # Content present but no pod/container bracket prefix
        assert "line0" in text
        assert "[myapp/main]" not in text


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
        text = _richlog_text(app)
        assert "[app-alpha/main]" in text
        assert "[app-beta/main]" in text


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
    """When a stream ends naturally, the pane state becomes 'ended'."""
    fake = FakeStream(lines_per_call=1)
    app = make_app([_pod("myapp", containers=("main",))], stream_logs=fake.returning)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("l")
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
        text = _richlog_text(app)
        # Prefix must be present even with only 1 visible pod
        assert "[app-only/main]" in text


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
    """Yields 5 lines (3 containing 'findme') and returns immediately."""

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
