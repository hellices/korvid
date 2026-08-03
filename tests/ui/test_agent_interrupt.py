"""Agent turn interruption UI (issue #170): stop key, interrupt-and-submit,
replacement queueing, transcript markers, and write-safety invariants."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from textual.widgets import Input

from korvid.agent.events import AgentEvent, TextDelta, ToolCallStarted, TurnComplete
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.agent_panel import AgentPanel, ChatEntry
from tests.ui.test_agent_wiring import StubRuntime, make_app

from .waits import until


class BlockingRuntime:
    """Streams a little text, then blocks until cancelled; supports the
    interruption contract (finalize_interrupt)."""

    def __init__(self, events: list[AgentEvent] | None = None) -> None:
        self._events = [TextDelta(text="let me check")] if events is None else events
        self.calls: list[str] = []
        self.finalized = 0
        self.total_tokens = (0, 0)
        self.usage_estimated = False

    async def run_turn(self, user_text: str, screen_context: str) -> AsyncIterator[AgentEvent]:
        self.calls.append(user_text)
        for ev in self._events:
            yield ev
        await asyncio.Event().wait()

    def finalize_interrupt(self) -> Any:
        from korvid.agent.events import TurnInterrupted

        self.finalized += 1
        return TurnInterrupted(input_tokens=3, output_tokens=1, estimated=True)


def _panel_text(app: KorvidApp) -> str:
    return "\n".join(entry.raw for entry in app.query_one(AgentPanel).query(ChatEntry))


async def _start_turn(app: KorvidApp, pilot: Any, text: str) -> Input:
    await pilot.press("ctrl+a")
    inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
    inp.value = text
    await pilot.press("enter")
    return inp


async def test_input_stays_enabled_while_the_agent_runs() -> None:
    runtime = BlockingRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "check my pods")
        await until(pilot, lambda: bool(runtime.calls), label="turn running")
        assert inp.disabled is False  # corrections must be typable mid-turn


async def test_explicit_stop_marks_partial_and_returns_to_idle() -> None:
    runtime = BlockingRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "check my pods")
        await until(pilot, lambda: "let me check" in _panel_text(app), label="streaming")
        await pilot.press("ctrl+x")
        await until(pilot, lambda: runtime.finalized == 1, label="finalized")
        await until(pilot, lambda: "interrupted" in _panel_text(app), label="marker shown")
        assert "let me check" in _panel_text(app)  # partial preserved, not deleted
        assert "✗" not in _panel_text(app)  # never rendered as an error
        assert inp.disabled is False
        assert app.focused is inp  # focus back in the input
        assert app._agent_task is None or app._agent_task.done()


async def test_interrupt_and_submit_starts_exactly_one_replacement() -> None:
    runtime = BlockingRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "first question")
        await until(pilot, lambda: len(runtime.calls) == 1, label="first turn running")
        inp.value = "actually check namespace foo"
        await pilot.press("enter")
        # the correction is echoed immediately, before the old turn drains
        await until(
            pilot,
            lambda: "actually check namespace foo" in _panel_text(app),
            label="correction echoed",
        )
        await until(pilot, lambda: len(runtime.calls) == 2, label="replacement started")
        assert runtime.calls[1] == "actually check namespace foo"
        assert runtime.finalized == 1  # old turn reached its terminal state first
        # the echoed correction appears exactly once (no re-echo at start)
        assert _panel_text(app).count("actually check namespace foo") == 1


async def test_rapid_submissions_keep_only_the_latest_replacement() -> None:
    runtime = BlockingRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "first")
        await until(pilot, lambda: len(runtime.calls) == 1, label="first turn running")
        inp.value = "second"
        await pilot.press("enter")
        inp.value = "third"
        await pilot.press("enter")
        await until(
            pilot,
            lambda: bool(runtime.calls) and runtime.calls[-1] == "third",
            label="latest replacement started",
        )
        await pilot.pause()
        # "second" may briefly start if its replacement won the race, but
        # the queue depth is one: nothing runs after the latest submission.
        assert runtime.calls[-1] == "third"
        assert len(runtime.calls) <= 3  # never an unbounded queue of turns


async def test_stop_hint_appears_in_the_running_status() -> None:
    runtime = BlockingRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "check")
        panel = app.query_one(AgentPanel)
        await until(pilot, lambda: "stop" in panel.status_text, label="stop hint shown")
        assert "ctrl+x" in panel.status_text


async def test_stop_hint_tracks_a_remapped_interrupt_key() -> None:
    """The advertised stop key must follow a `keybindings:` remap — a hint
    naming a key that no longer stops the turn is worse than none."""
    from korvid.core.config import KorvidConfig
    from korvid.core.store import ResourceStore
    from korvid.core.watch import WatchManager

    runtime: Any = BlockingRuntime()  # duck-typed stand-in, as in make_app
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[Any]:
        if False:  # pragma: no cover - typing seam: makes this an async generator
            yield ("ADDED", None)
        while True:
            await asyncio.sleep(0.01)

    app = KorvidApp(
        config=KorvidConfig(namespace="default", keybindings={"interrupt_agent": "ctrl+g"}),
        store=store,
        watch_manager=WatchManager(store, source),
        agent_runtime=runtime,
        agent_model_name="test-model",
    )
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "check")
        panel = app.query_one(AgentPanel)
        await until(pilot, lambda: "stop" in panel.status_text, label="stop hint shown")
        assert "ctrl+g" in panel.status_text
        assert "ctrl+x" not in panel.status_text
        # and the remapped key actually stops the turn
        await pilot.press("ctrl+g")
        await until(pilot, lambda: runtime.finalized == 1, label="remapped key stops")


async def test_cancel_before_the_turn_coroutine_runs_still_starts_replacement() -> None:
    """A replacement task cancelled before the event loop first enters its
    coroutine never runs its CancelledError handler — the queued correction
    must still be drained (review on #175)."""
    from korvid.ui.messages import AgentPromptSubmitted

    runtime = BlockingRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        # Both submissions land in the same tick: the first turn's task is
        # created but its coroutine has not run when the second arrives and
        # cancels it.
        app.on_agent_prompt_submitted(AgentPromptSubmitted("first"))
        assert app._agent_task is not None
        app.on_agent_prompt_submitted(AgentPromptSubmitted("second"))
        await until(
            pilot,
            lambda: runtime.calls[-1:] == ["second"],
            label="queued correction started",
        )


async def test_interrupted_tool_line_is_marked() -> None:
    runtime = BlockingRuntime(
        events=[ToolCallStarted(call_id="c1", name="get_logs", arguments="{}")]
    )
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "logs?")
        await until(pilot, lambda: "get_logs" in _panel_text(app), label="tool line shown")
        await pilot.press("ctrl+x")
        await until(pilot, lambda: "interrupted" in _panel_text(app), label="marked")


async def test_stop_while_idle_is_a_no_op() -> None:
    runtime = StubRuntime([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        await pilot.press("ctrl+x")
        await pilot.pause()
        assert not runtime.calls  # nothing started, nothing crashed


async def test_stop_from_outside_the_input_returns_focus_to_it() -> None:
    """interrupt_agent is a priority global binding: stopping a turn while
    focus sits on the resource table must hand focus back to the input so
    the correction can be typed immediately."""
    runtime = BlockingRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "check")
        await until(pilot, lambda: bool(runtime.calls), label="turn running")
        app.set_focus(None)  # focus leaves the panel
        await pilot.press("ctrl+x")
        await until(pilot, lambda: runtime.finalized == 1, label="finalized")
        assert app.focused is inp


async def test_rapid_corrections_inject_cancellation_only_once() -> None:
    """A later correction while the old turn is already cancelling must not
    re-inject CancelledError — a second delivery can interrupt the
    cancellation cleanup itself (review on #175)."""
    from korvid.ui.messages import AgentPromptSubmitted

    runtime = BlockingRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "first")
        await until(pilot, lambda: len(runtime.calls) == 1, label="turn running")
        task = app._agent_task
        assert task is not None
        app.on_agent_prompt_submitted(AgentPromptSubmitted("second"))
        app.on_agent_prompt_submitted(AgentPromptSubmitted("third"))
        assert task.cancelling() == 1  # cancellation was not re-injected
        await until(pilot, lambda: runtime.calls[-1:] == ["third"], label="latest ran")


async def test_immediate_stop_before_the_turn_first_runs_clears_busy_state() -> None:
    """An explicit stop in the same tick the turn was created cancels the
    task before its coroutine (and CancelledError handler) ever runs — the
    panel must still leave its running state (review on #175)."""
    from korvid.ui.messages import AgentPromptSubmitted

    runtime = BlockingRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        app.on_agent_prompt_submitted(AgentPromptSubmitted("first"))
        task = app._agent_task
        assert task is not None
        app.action_interrupt_agent()  # same tick: the coroutine never ran
        await until(pilot, lambda: task.done(), label="task settled")
        panel = app.query_one(AgentPanel)
        await until(pilot, lambda: panel.status_text == "", label="status cleared")
        assert not runtime.calls  # the turn never reached the runtime
        assert "interrupted" in _panel_text(app)


async def test_stop_then_immediate_shutdown_injects_cancellation_once() -> None:
    """on_unmount must not re-inject cancellation into a task an explicit
    stop already has draining its cleanup — a second CancelledError aborts
    the provider cleanup mid-flight (review on #175)."""

    class SlowCleanupRuntime(BlockingRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_done = False

        async def run_turn(self, user_text: str, screen_context: str) -> AsyncIterator[AgentEvent]:
            self.calls.append(user_text)
            try:
                yield TextDelta(text="let me check")
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.05)  # provider cleanup on close
                self.cleanup_done = True

    runtime = SlowCleanupRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "check")
        await until(pilot, lambda: bool(runtime.calls), label="turn running")
        app.action_interrupt_agent()
        # exit immediately — shutdown runs while the stop is still draining
    assert runtime.cleanup_done


async def test_interrupt_marker_owns_the_partial_not_the_replacement() -> None:
    """Interrupt-and-submit echoes the correction before the old turn
    drains: the ⏹ marker must attach to the partial assistant output, never
    trail the echoed replacement as if describing it (review on #175)."""
    runtime = BlockingRuntime()  # streams "let me check", then blocks
    app = make_app(runtime)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "first question")
        await until(pilot, lambda: "let me check" in _panel_text(app), label="streaming")
        inp.value = "actually check namespace foo"
        await pilot.press("enter")
        await until(pilot, lambda: len(runtime.calls) == 2, label="replacement started")
        text = _panel_text(app)
        assert text.index("interrupted") < text.index("actually check namespace foo")


async def test_interrupt_marker_precedes_the_echo_when_nothing_streamed() -> None:
    """Even with no partial output to mark in place, the marker must land
    before the echoed replacement, not after it."""
    runtime = BlockingRuntime(events=[])  # blocks before any output
    app = make_app(runtime)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "first question")
        await until(pilot, lambda: len(runtime.calls) == 1, label="turn running")
        inp.value = "actually check namespace foo"
        await pilot.press("enter")
        await until(pilot, lambda: len(runtime.calls) == 2, label="replacement started")
        text = _panel_text(app)
        assert text.index("interrupted") < text.index("actually check namespace foo")


async def test_stale_done_callback_cannot_consume_the_replacement() -> None:
    """The drain callback must be scoped to the task that completed: a
    callback from a superseded task must neither consume the queued
    replacement nor start a second concurrent turn (review on #175)."""
    runtime = BlockingRuntime()
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "first")
        await until(pilot, lambda: len(runtime.calls) == 1, label="turn running")
        stale = asyncio.create_task(asyncio.sleep(0))
        await stale
        app._agent_replacement = "queued"
        app._drain_agent_replacement(stale)  # not the current agent task
        await pilot.pause()
        assert len(runtime.calls) == 1  # no second turn was launched
        assert app._agent_replacement == "queued"  # left for the real owner


# --- write safety (issue #170): interrupt vs the approval gate -------------


async def test_interrupt_while_awaiting_approval_dismisses_dialog(tmp_path: Any) -> None:
    """Cancelling a turn that is waiting on the approval dialog must pop
    the dialog and never execute the write (no orphaned modal whose 'y'
    would resolve a dead future)."""
    import pytest

    from korvid.ui.widgets.confirm_screen import ConfirmScreen
    from tests.ui.test_agent_write import Recorder, _expand_panel
    from tests.ui.test_agent_write import make_app as make_write_app

    rec = Recorder()
    app = make_write_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog shown")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)  # dialog cleaned up
        assert rec.calls == []  # the write never ran


async def test_interrupt_after_approval_lets_the_write_finish(tmp_path: Any) -> None:
    """Once the user approved, the audited write runs to completion even if
    the turn is cancelled mid-flight (audit fail-closed invariant)."""
    import json

    import pytest

    from korvid.k8s.discovery import ResourceMeta
    from korvid.ui.widgets.confirm_screen import ConfirmScreen
    from tests.ui.test_agent_write import Recorder, _expand_panel
    from tests.ui.test_agent_write import make_app as make_write_app

    class SlowRecorder(Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.gate = asyncio.Event()

        async def delete_object(
            self,
            meta: ResourceMeta,
            namespace: str | None,
            name: str,
            *,
            uid: str | None = None,
        ) -> None:
            self.started.set()
            await self.gate.wait()
            await super().delete_object(meta, namespace, name, uid=uid)

    rec = SlowRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_write_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog shown")
        await pilot.press("y")
        await until(pilot, lambda: rec.started.is_set(), label="write in flight")
        task.cancel()
        rec.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await until(pilot, lambda: bool(rec.calls), label="write completed")
        assert rec.calls == [("delete", "deployments", "default", "web")]
        lines = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert lines[-1]["outcome"] == "success"  # audit trail is complete


async def test_repeated_cancels_never_kill_an_approved_write(tmp_path: Any) -> None:
    """A second cancellation arriving while the first is being absorbed must
    not propagate into the approved write: every wait stays shielded until
    the write reaches a terminal state (review on #175)."""
    import json

    import pytest

    from korvid.k8s.discovery import ResourceMeta
    from korvid.ui.widgets.confirm_screen import ConfirmScreen
    from tests.ui.test_agent_write import Recorder, _expand_panel
    from tests.ui.test_agent_write import make_app as make_write_app

    class SlowRecorder(Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.gate = asyncio.Event()

        async def delete_object(
            self,
            meta: ResourceMeta,
            namespace: str | None,
            name: str,
            *,
            uid: str | None = None,
        ) -> None:
            self.started.set()
            await self.gate.wait()
            await super().delete_object(meta, namespace, name, uid=uid)

    rec = SlowRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_write_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog shown")
        await pilot.press("y")
        await until(pilot, lambda: rec.started.is_set(), label="write in flight")
        task.cancel()
        await pilot.pause()  # the first CancelledError is being absorbed…
        task.cancel()  # …when a second cancellation arrives
        await pilot.pause()
        rec.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await until(pilot, lambda: bool(rec.calls), label="write completed")
        assert rec.calls == [("delete", "deployments", "default", "web")]
        lines = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert lines[-1]["outcome"] == "success"  # never intent-with-no-outcome
