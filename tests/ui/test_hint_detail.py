"""Hint detail overlay (issue #34): `i` on a troubled pod row opens a modal
with the full trouble list and recent Warning events - nothing truncated."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from korvid.k8s.models import ContainerTrouble
from korvid.ui.widgets.hint_detail import HintDetailScreen, render_hint_detail

_NOW = datetime(2026, 7, 26, 8, 5, 0, tzinfo=UTC)


def test_detail_lists_every_trouble_entry_uncapped() -> None:
    entries = tuple(
        ContainerTrouble(container=f"c{i}", reason="CrashLoopBackOff") for i in range(5)
    )
    text = render_hint_detail(entries, [], now=_NOW).plain
    for i in range(5):
        assert f"c{i}" in text
    assert "more" not in text  # no fold in the overlay


def test_detail_keeps_full_message_and_termination_details() -> None:
    entry = ContainerTrouble(
        container="demo-app",
        reason="CrashLoopBackOff",
        message="back-off 5m0s restarting failed container=demo-app pod=demo-app-abc_ns(uid)",
        exit_code=137,
        exit_reason="OOMKilled",
        finished_at="2026-07-26T08:00:00Z",
        restarts=696,
    )
    text = render_hint_detail((entry,), [], now=_NOW).plain
    # verbatim API data: the overlay never strips fragments
    assert "pod=demo-app-abc_ns(uid)" in text
    assert "exit 137 (OOMKilled)" in text
    assert "restarts 696" in text
    assert "5m ago" in text


def test_detail_shows_warning_events_with_relative_age_and_count() -> None:
    events: list[dict[str, Any]] = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "lastTimestamp": "2026-07-26T08:04:20Z",
            "count": 123,
        },
        {
            "type": "Normal",
            "reason": "Pulled",
            "message": "image pulled",
            "lastTimestamp": "2026-07-26T08:04:00Z",
        },
    ]
    text = render_hint_detail((), events, now=_NOW).plain
    assert "BackOff" in text
    assert "Back-off restarting failed container" in text
    assert "40s ago" in text
    assert "\u00d7123" in text
    assert "image pulled" not in text  # Normal events stay out


def test_detail_without_warnings_says_so() -> None:
    text = render_hint_detail(
        (ContainerTrouble(container="app", reason="CrashLoopBackOff"),), [], now=_NOW
    ).plain
    assert "no warning events" in text


async def test_screen_dismisses_on_escape_and_i() -> None:
    from textual.app import App

    class _Host(App[None]):
        pass

    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(
            HintDetailScreen(
                "default/web-1",
                (ContainerTrouble(container="app", reason="CrashLoopBackOff"),),
                [],
            )
        )
        await pilot.pause()
        assert isinstance(app.screen, HintDetailScreen)
        await pilot.press("i")
        await pilot.pause()
        assert not isinstance(app.screen, HintDetailScreen)


def test_detail_marks_events_unavailable_on_fetch_failure() -> None:
    text = render_hint_detail(
        (ContainerTrouble(container="app", reason="CrashLoopBackOff"),),
        [],
        events_unavailable=True,
        now=_NOW,
    ).plain
    assert "warning events unavailable" in text
    assert "no warning events" not in text


def test_event_age_falls_back_to_creation_timestamp() -> None:
    """Review fix (PR #51 r2): core v1 events may carry only
    metadata.creationTimestamp - mirror the strip's fallback chain."""
    events: list[dict[str, Any]] = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "restarting",
            "metadata": {"creationTimestamp": "2026-07-26T08:04:20Z"},
        }
    ]
    text = render_hint_detail((), events, now=_NOW).plain
    assert "40s ago" in text
