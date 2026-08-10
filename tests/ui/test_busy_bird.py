"""Corvid busy indicator (issue #143): an animated ASCII bird next to the
status-bar progress label so long operations visibly *move*. Frame cycling
is a pure function (no timing assertions, per AGENTS.md)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from textual.widgets import Static

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.status_bar import BIRD_FRAMES, StatusBar, bird_frame

from .waits import until

# ---------------------------------------------------------------------------
# pure frame logic
# ---------------------------------------------------------------------------


def test_frames_are_nonempty_and_equal_width() -> None:
    """Frames swap in place on the status bar: unequal widths would make
    the whole line jitter."""
    assert len(BIRD_FRAMES) >= 2
    widths = {len(f) for f in BIRD_FRAMES}
    assert len(widths) == 1


def test_bird_frame_cycles_deterministically() -> None:
    n = len(BIRD_FRAMES)
    assert [bird_frame(i) for i in range(n)] == list(BIRD_FRAMES)
    assert bird_frame(n) == BIRD_FRAMES[0]  # wraps
    assert bird_frame(7 * n + 2) == BIRD_FRAMES[2 % n]


def test_frames_are_pure_ascii() -> None:
    """No emoji dependency: the indicator must render on every terminal."""
    for frame in BIRD_FRAMES:
        assert frame.isascii()


# ---------------------------------------------------------------------------
# StatusBar animation lifecycle
# ---------------------------------------------------------------------------


class _Host(KorvidApp):
    pass


def _make_app(audit_path: Path | None = None) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        while True:
            await asyncio.sleep(0.01)
        yield ("", None)  # pragma: no cover - typing aid

    async def list_namespaces() -> list[str]:
        return ["default"]

    aliases: dict[str, ResourceMeta] = {"pods": PODS_META}
    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=aliases,
        audit=AuditLog(audit_path) if audit_path is not None else None,
    )


async def test_progress_label_renders_a_bird_frame() -> None:
    app = _make_app()
    async with app.run_test():
        bar = app.query_one(StatusBar)
        # _set_progress renders synchronously; inspecting before yielding to
        # the event loop keeps the frame deterministic on slow runners.
        app._set_progress("test", "rendering helm preview")
        text = str(bar.render())
        assert BIRD_FRAMES[0] in text
        assert "rendering helm preview" in text
        # the bird leads the label (decided format)
        assert text.index(BIRD_FRAMES[0]) < text.index("rendering helm preview")


async def test_animation_tick_swaps_the_frame_in_place() -> None:
    """The interval callback advances the frame without a new update_status
    call - invoked directly here (no wall-clock assertions)."""
    app = _make_app()
    async with app.run_test():
        bar = app.query_one(StatusBar)
        # synchronous render: frame 0 is showing before the loop runs
        app._set_progress("test", "draining node")
        assert BIRD_FRAMES[0] in str(bar.render())
        bar._advance_bird()  # what the timer does every 500ms
        text = str(bar.render())
        assert BIRD_FRAMES[1] in text
        assert BIRD_FRAMES[0] not in text
        assert "draining node" in text  # the label survived the swap


async def test_animation_tick_does_not_schedule_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frame swaps are equal-width in-place updates: a tick every 500ms must
    not schedule a layout pass, only a repaint. Status changes still lay out."""
    app = _make_app()
    async with app.run_test() as pilot:
        app._set_progress("test", "draining node")
        await pilot.pause()
        bar = app.query_one(StatusBar)
        calls: list[bool] = []
        original = Static.update

        def spy(self: Static, content: object = "", *, layout: bool = True) -> None:
            calls.append(layout)
            original(self, content, layout=layout)  # type: ignore[arg-type]  # spy passthrough

        monkeypatch.setattr(Static, "update", spy)
        bar._advance_bird()
        assert calls == [False]  # animation: repaint only
        calls.clear()
        app._set_progress("test", "still draining")
        await pilot.pause()
        assert True in calls  # real status changes still lay out


async def test_animation_stops_and_resets_when_progress_clears() -> None:
    app = _make_app()
    async with app.run_test() as pilot:
        app._set_progress("test", "rendering")
        await pilot.pause()
        bar = app.query_one(StatusBar)
        assert bar._anim_timer is not None  # running while a label is live
        bar._advance_bird()
        app._set_progress("test", "")
        await pilot.pause()
        assert bar._anim_timer is None  # stopped with the last owner
        text = str(bar.render())
        assert all(frame not in text for frame in BIRD_FRAMES)
        # a new operation restarts from frame 0: _set_progress renders
        # synchronously, so inspect before yielding to the interval timer.
        app._set_progress("test2", "next op")
        assert BIRD_FRAMES[0] in str(bar.render())
        assert bar._anim_timer is not None


# ---------------------------------------------------------------------------
# post-approval write phase (issue #143 scope 2)
# ---------------------------------------------------------------------------


async def test_run_write_publishes_progress_while_the_op_runs(tmp_path: Path) -> None:
    """Between approval and the outcome toast there was no in-flight state:
    _run_write now owns a progress label for exactly the op's duration."""
    app = _make_app(audit_path=tmp_path / "audit.jsonl")
    gate = asyncio.Event()

    async def slow_op() -> None:
        await gate.wait()

    async with app.run_test() as pilot:
        task = asyncio.ensure_future(
            app._run_write("delete", PODS_META, "default", "web-1", slow_op)
        )
        await until(
            pilot,
            lambda: any("delete pods/web-1" in v for v in app._progress_labels.values()),
            label="in-flight label published",
        )
        bar_text = str(app.query_one(StatusBar).render())
        assert "delete pods/web-1" in bar_text
        gate.set()
        result = await task
        assert result == "done"
        assert not app._progress_labels  # cleared with the op
