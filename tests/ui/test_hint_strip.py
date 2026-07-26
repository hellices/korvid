"""Tests for the HintStrip widget rendering (ops hint strip, #26)."""

from __future__ import annotations

from datetime import UTC, datetime

from korvid.k8s.models import ContainerTrouble
from korvid.ui.widgets.hint_strip import HintStrip, relative_age, render_trouble_lines

_NOW = datetime(2026, 7, 26, 8, 5, 0, tzinfo=UTC)


def test_render_crashloop_line_shows_reason_message_and_last_exit() -> None:
    lines = render_trouble_lines(
        (
            ContainerTrouble(
                container="app",
                reason="CrashLoopBackOff",
                message="back-off 5m0s restarting failed container",
                exit_code=137,
                exit_reason="OOMKilled",
                finished_at="2026-07-26T08:00:00Z",
                restarts=12,
            ),
        ),
        now=_NOW,
    )
    assert len(lines) == 1
    text = lines[0].plain
    assert "app" in text
    assert "CrashLoopBackOff" in text
    assert "back-off 5m0s restarting failed container" in text
    assert "exit 137 (OOMKilled)" in text
    assert "restarts 12" in text
    assert "last 5m ago" in text  # relative age, not the raw RFC 3339 timestamp
    assert "2026-" not in text


def test_relative_age_buckets() -> None:
    assert relative_age("2026-07-26T08:04:20Z", now=_NOW) == "40s"
    assert relative_age("2026-07-26T07:20:00Z", now=_NOW) == "45m"
    assert relative_age("2026-07-26T01:05:00Z", now=_NOW) == "7h"
    assert relative_age("2026-07-20T08:05:00Z", now=_NOW) == "6d"
    assert relative_age("not-a-timestamp", now=_NOW) is None


def test_render_waiting_without_termination_omits_exit_segment() -> None:
    lines = render_trouble_lines(
        (
            ContainerTrouble(
                container="app",
                reason="ImagePullBackOff",
                message='Back-off pulling image "nginx:nope"',
            ),
        )
    )
    text = lines[0].plain
    assert "ImagePullBackOff" in text
    assert "exit" not in text
    assert "restarts" not in text  # zero restarts adds no noise


def test_render_caps_entries_and_reports_remainder() -> None:
    entries = tuple(
        ContainerTrouble(container=f"c{i}", reason="CrashLoopBackOff") for i in range(5)
    )
    lines = render_trouble_lines(entries)
    assert len(lines) == 3  # capped: 2 detail lines + "+3 more"
    assert "+3 more" in lines[-1].plain


def test_render_event_line_appended_when_given() -> None:
    lines = render_trouble_lines(
        (ContainerTrouble(container="app", reason="CrashLoopBackOff"),),
        event="Warning BackOff: restarting failed container app",
    )
    assert len(lines) == 2
    assert "Warning BackOff" in lines[-1].plain


async def test_hint_strip_widget_shows_and_clears() -> None:
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield HintStrip()

    app = _Host()
    async with app.run_test():
        strip = app.query_one(HintStrip)
        assert strip.display is False  # hidden until trouble arrives
        strip.show_trouble(
            (ContainerTrouble(container="app", reason="CrashLoopBackOff"),),
            event=None,
        )
        assert strip.display is True
        strip.clear_hint()
        assert strip.display is False


def test_multiline_message_is_collapsed_to_one_logical_line() -> None:
    entry = ContainerTrouble(
        container="app",
        reason="CrashLoopBackOff",
        message="line one\nline two\n  line three",
    )
    lines = render_trouble_lines((entry,), event="event first\nevent second")
    assert len(lines) == 2
    assert "\n" not in lines[0].plain
    assert "line one line two line three" in lines[0].plain
    assert lines[1].plain == "event first event second"


async def test_long_message_occupies_one_visual_row() -> None:
    from textual.app import App, ComposeResult

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield HintStrip()

    entry = ContainerTrouble(container="app", reason="CrashLoopBackOff", message="word " * 200)
    app = _Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        strip = app.query_one(HintStrip)
        strip.show_trouble((entry,), event="Back-off restarting failed container")
        await pilot.pause()
        # one trouble row + one event row: the long message truncates, never
        # wraps, so the reserved event row stays visible
        assert strip.display is True
        assert strip.region.height == 3  # border-top + 2 content rows
