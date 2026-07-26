"""Tests for the HintStrip widget rendering (ops hint strip, #26)."""

from __future__ import annotations

from korvid.k8s.models import ContainerTrouble
from korvid.ui.widgets.hint_strip import HintStrip, render_trouble_lines


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
        )
    )
    assert len(lines) == 1
    text = lines[0].plain
    assert "app" in text
    assert "CrashLoopBackOff" in text
    assert "back-off 5m0s restarting failed container" in text
    assert "exit 137 (OOMKilled)" in text
    assert "restarts 12" in text


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
