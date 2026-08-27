"""Agent turn interruption UI (issue #170): stop key, interrupt-and-submit,
replacement queueing, transcript markers, and write-safety invariants."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import patch

import pytest
from textual.widgets import Input, Static

from korvid.agent.events import AgentEvent, TextDelta, ToolCallStarted, TurnComplete
from korvid.ui.app import KorvidApp
from korvid.ui.messages import AgentPromptSubmitted
from korvid.ui.widgets.agent_panel import AgentPanel, ChatEntry
from tests.ui.agent_session_fakes import FakeSession
from tests.ui.test_agent_wiring import StubSession, make_app

from .agent_write_support import Recorder, _expand_panel
from .agent_write_support import make_app as make_write_app
from .waits import until


def test_fake_session_interrupt_usage_matches_live_cumulative_totals() -> None:
    session = FakeSession(tokens=(5, 6))
    session._pending = True

    event = session.finalize_interrupt()

    assert (event.input_tokens, event.output_tokens, event.estimated) == (3, 1, True)
    assert session.total_tokens == (8, 7)
    assert session.usage_estimated is True


class BlockingSession(FakeSession):
    """Streams a little text, then blocks until stopped."""

    def __init__(self, events: list[AgentEvent] | None = None) -> None:
        super().__init__([TextDelta(text="let me check")] if events is None else events, block=True)


def _panel_text(app: KorvidApp) -> str:
    return "\n".join(entry.raw for entry in app.query_one(AgentPanel).query(ChatEntry))


def _header(app: KorvidApp) -> str:
    """The panel header exactly as the user reads it."""
    return str(app.query_one(AgentPanel).query_one("#agent-header", Static).render())


async def _start_turn(app: KorvidApp, pilot: Any, text: str) -> Input:
    await pilot.press("ctrl+a")
    inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
    inp.value = text
    await pilot.press("enter")
    return inp


async def test_input_stays_enabled_while_the_agent_runs() -> None:
    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "check my pods")
        await until(pilot, lambda: bool(session.prompts), label="turn running")
        assert inp.disabled is False  # corrections must be typable mid-turn


async def test_explicit_stop_marks_partial_and_returns_to_idle() -> None:
    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "check my pods")
        await until(pilot, lambda: "let me check" in _panel_text(app), label="streaming")
        await pilot.press("ctrl+x")
        await until(pilot, lambda: session.finalized == 1, label="finalized")
        await until(pilot, lambda: "interrupted" in _panel_text(app), label="marker shown")
        assert "let me check" in _panel_text(app)  # partial preserved, not deleted
        assert "✗" not in _panel_text(app)  # never rendered as an error
        assert inp.disabled is False
        assert app.focused is inp  # focus back in the input
        assert app._agent_ui._task is None or app._agent_ui._task.done()


async def test_interrupt_and_submit_starts_exactly_one_replacement() -> None:
    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "first question")
        await until(pilot, lambda: len(session.prompts) == 1, label="first turn running")
        inp.value = "actually check namespace foo"
        await pilot.press("enter")
        # the correction is echoed immediately, before the old turn drains
        await until(
            pilot,
            lambda: "actually check namespace foo" in _panel_text(app),
            label="correction echoed",
        )
        await until(pilot, lambda: len(session.prompts) == 2, label="replacement started")
        assert session.prompts[1] == "actually check namespace foo"
        assert session.finalized == 1  # old turn reached its terminal state first
        # the echoed correction appears exactly once (no re-echo at start)
        assert _panel_text(app).count("actually check namespace foo") == 1


async def test_rapid_submissions_keep_only_the_latest_replacement() -> None:
    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "first")
        await until(pilot, lambda: len(session.prompts) == 1, label="first turn running")
        inp.value = "second"
        await pilot.press("enter")
        inp.value = "third"
        await pilot.press("enter")
        await until(
            pilot,
            lambda: bool(session.prompts) and session.prompts[-1] == "third",
            label="latest replacement started",
        )
        await pilot.pause()
        # "second" may briefly start if its replacement won the race, but
        # the queue depth is one: nothing runs after the latest submission.
        assert session.prompts[-1] == "third"
        assert len(session.prompts) <= 3  # never an unbounded queue of turns


async def test_stop_hint_appears_in_the_running_status() -> None:
    session = BlockingSession()
    app = make_app(session)
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

    session: Any = BlockingSession()  # duck-typed stand-in, as in make_app
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
        agent_session=session,
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
        await until(pilot, lambda: session.finalized == 1, label="remapped key stops")


async def test_cancel_before_the_turn_coroutine_runs_still_starts_replacement() -> None:
    """A replacement task cancelled before the event loop first enters its
    coroutine never runs its CancelledError handler — the queued correction
    must still be drained (review on #175)."""
    from korvid.ui.messages import AgentPromptSubmitted

    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        # Both submissions land in the same tick: the first turn's task is
        # created but its coroutine has not run when the second arrives and
        # cancels it.
        app.on_agent_prompt_submitted(AgentPromptSubmitted("first"))
        assert app._agent_ui._task is not None
        app.on_agent_prompt_submitted(AgentPromptSubmitted("second"))
        await until(
            pilot,
            lambda: session.prompts[-1:] == ["second"],
            label="queued correction started",
        )


async def test_interrupted_tool_line_is_marked() -> None:
    session = BlockingSession(
        events=[ToolCallStarted(call_id="c1", name="get_logs", arguments="{}")]
    )
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "logs?")
        await until(pilot, lambda: "get_logs" in _panel_text(app), label="tool line shown")
        await pilot.press("ctrl+x")
        await until(pilot, lambda: "interrupted" in _panel_text(app), label="marked")


async def test_stop_while_idle_is_a_no_op() -> None:
    session = StubSession([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    app = make_app(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        await pilot.press("ctrl+x")
        await pilot.pause()
        assert not session.prompts  # nothing started, nothing crashed


async def test_stop_from_outside_the_input_returns_focus_to_it() -> None:
    """interrupt_agent is a priority global binding: stopping a turn while
    focus sits on the resource table must hand focus back to the input so
    the correction can be typed immediately."""
    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "check")
        await until(pilot, lambda: bool(session.prompts), label="turn running")
        app.set_focus(None)  # focus leaves the panel
        await pilot.press("ctrl+x")
        await until(pilot, lambda: session.finalized == 1, label="finalized")
        assert app.focused is inp


async def test_rapid_corrections_inject_cancellation_only_once() -> None:
    """A later correction while the old turn is already cancelling must not
    re-inject CancelledError — a second delivery can interrupt the
    cancellation cleanup itself (review on #175)."""
    from korvid.ui.messages import AgentPromptSubmitted

    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "first")
        await until(pilot, lambda: len(session.prompts) == 1, label="turn running")
        task = app._agent_ui._task
        assert task is not None
        app.on_agent_prompt_submitted(AgentPromptSubmitted("second"))
        app.on_agent_prompt_submitted(AgentPromptSubmitted("third"))
        assert task.cancelling() == 1  # cancellation was not re-injected
        await until(pilot, lambda: session.prompts[-1:] == ["third"], label="latest ran")


async def test_immediate_stop_before_the_turn_first_runs_clears_busy_state() -> None:
    """An explicit stop in the same tick the turn was created cancels the
    task before its coroutine (and CancelledError handler) ever runs — the
    panel must still leave its running state (review on #175)."""
    from korvid.ui.messages import AgentPromptSubmitted

    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        app.on_agent_prompt_submitted(AgentPromptSubmitted("first"))
        task = app._agent_ui._task
        assert task is not None
        app.action_interrupt_agent()  # same tick: the coroutine never ran
        await until(pilot, lambda: task.done(), label="task settled")
        panel = app.query_one(AgentPanel)
        await until(pilot, lambda: panel.status_text == "", label="status cleared")
        assert not session.prompts  # the turn never reached the session
        assert "interrupted" in _panel_text(app)


async def test_a_stop_before_the_turn_first_runs_does_not_recount_usage() -> None:
    """The header must not grow when nothing was spent.

    The panel adds a terminal event's token counts to the totals it
    already shows, so the fallback the controller mints for a turn
    cancelled before its first step has to be a *zero delta*. Minting it
    from the session's running totals adds the whole conversation a second
    time — after one 40/12 turn the header would read 80/24 for a turn
    that never reached the provider.
    """
    from korvid.ui.messages import AgentPromptSubmitted

    session = StubSession([TurnComplete(input_tokens=40, output_tokens=12, estimated=False)])
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "how many pods?")
        await until(pilot, lambda: "40" in _header(app), label="first turn counted")
        # A live session books the completed turn into its running totals.
        session.total_tokens = (40, 12)
        settled = _header(app)

        app.on_agent_prompt_submitted(AgentPromptSubmitted("and the nodes?"))
        task = app._agent_ui._task
        assert task is not None
        app.action_interrupt_agent()  # same tick: the coroutine never ran
        await until(pilot, lambda: task.done(), label="task settled")
        panel = app.query_one(AgentPanel)
        await until(pilot, lambda: panel.status_text == "", label="status cleared")
        assert _header(app) == settled
        assert "80" not in _header(app)


async def test_stop_then_immediate_shutdown_injects_cancellation_once() -> None:
    """on_unmount must not re-inject cancellation into a task an explicit
    stop already has draining its cleanup — a second CancelledError aborts
    the provider cleanup mid-flight (review on #175)."""

    class SlowCleanupSession(BlockingSession):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_done = False

        def run_turn(self, user_text: str) -> AsyncIterator[AgentEvent]:
            self.prompts.append(user_text)
            return self._slow()

        async def _slow(self) -> AsyncIterator[AgentEvent]:
            try:
                yield TextDelta(text="let me check")
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.05)  # provider cleanup on close
                self.cleanup_done = True

    session = SlowCleanupSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "check")
        await until(pilot, lambda: bool(session.prompts), label="turn running")
        app.action_interrupt_agent()
        # exit immediately — shutdown runs while the stop is still draining
    assert session.cleanup_done


async def test_unmount_marks_the_agent_down_before_its_first_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`on_unmount` must mark the agent session shutting down *synchronously*,
    before its first await.

    Teardown awaits the bridge-dispatch reap and the proposal expiry sweep
    before it reaches the agent controller. An interrupt-and-submit whose
    cancelled turn settles inside one of those awaits would otherwise drain
    its queued replacement and start a fresh turn — reading the screen stack
    of an app that is being torn down (ScreenStackError)."""

    class GatedCleanupSession(BlockingSession):
        """Holds its cancellation cleanup until released, so the interrupted
        turn settles exactly inside the teardown await under test."""

        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        def run_turn(self, user_text: str) -> AsyncIterator[AgentEvent]:
            self.prompts.append(user_text)
            return self._gated()

        async def _gated(self) -> AsyncIterator[AgentEvent]:
            try:
                yield TextDelta(text="let me check")
                await asyncio.Event().wait()
            finally:
                await self.release.wait()

    session = GatedCleanupSession()
    app = make_app(session)
    drained = asyncio.Event()
    screen_reads: list[str] = []

    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "first")
        await until(pilot, lambda: "let me check" in _panel_text(app), label="streaming")
        task = app._agent_ui._task
        assert task is not None
        # Added after the controller's own drain callback, and callbacks run
        # in the order they were added: the teardown step below resumes only
        # once the replacement has had its chance to start.
        task.add_done_callback(lambda _task: drained.set())

        screens = app._agent_ui._screens
        read_selected_row = screens.selected_row_key

        def _record_screen_read() -> str | None:
            screen_reads.append("selected_row_key")
            return read_selected_row()

        monkeypatch.setattr(screens, "selected_row_key", _record_screen_read)

        expire_proposals = app._proposals.expire_all

        async def _expire_while_the_turn_settles(reason: str) -> None:
            session.release.set()  # the interrupted turn settles in here
            await drained.wait()
            await asyncio.sleep(0)  # a replacement turn's first step, if any
            await expire_proposals(reason)

        monkeypatch.setattr(app._proposals, "expire_all", _expire_while_the_turn_settles)

        inp.value = "second"
        await pilot.press("enter")  # interrupt-and-submit: queued, then cancelled
        assert task.cancelling() == 1
        # exit immediately — unmount runs while the interrupt is still draining

    assert app._agent_ui._task is task  # no replacement turn was ever started
    assert session.prompts == ["first"]
    assert screen_reads == []  # nothing read the screen stack during teardown


async def test_interrupt_marker_owns_the_partial_not_the_replacement() -> None:
    """Interrupt-and-submit echoes the correction before the old turn
    drains: the ⏹ marker must attach to the partial assistant output, never
    trail the echoed replacement as if describing it (review on #175)."""
    session = BlockingSession()  # streams "let me check", then blocks
    app = make_app(session)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "first question")
        await until(pilot, lambda: "let me check" in _panel_text(app), label="streaming")
        inp.value = "actually check namespace foo"
        await pilot.press("enter")
        await until(pilot, lambda: len(session.prompts) == 2, label="replacement started")
        text = _panel_text(app)
        assert text.index("interrupted") < text.index("actually check namespace foo")


async def test_interrupt_marker_precedes_the_echo_when_nothing_streamed() -> None:
    """Even with no partial output to mark in place, the marker must land
    before the echoed replacement, not after it."""
    session = BlockingSession(events=[])  # blocks before any output
    app = make_app(session)
    async with app.run_test() as pilot:
        inp = await _start_turn(app, pilot, "first question")
        await until(pilot, lambda: len(session.prompts) == 1, label="turn running")
        inp.value = "actually check namespace foo"
        await pilot.press("enter")
        await until(pilot, lambda: len(session.prompts) == 2, label="replacement started")
        text = _panel_text(app)
        assert text.index("interrupted") < text.index("actually check namespace foo")


async def test_double_correction_before_drain_adds_one_marker() -> None:
    """Two corrections queued before the old task settles must not stack a
    second ⏹ marker — only the latest correction is retained, so a second
    marker would imply the first correction ran and was interrupted."""
    from korvid.ui.messages import AgentPromptSubmitted

    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "first")
        await until(pilot, lambda: len(session.prompts) == 1, label="turn running")
        app.on_agent_prompt_submitted(AgentPromptSubmitted("second"))
        app.on_agent_prompt_submitted(AgentPromptSubmitted("third"))
        await until(pilot, lambda: session.prompts[-1:] == ["third"], label="latest ran")
        assert _panel_text(app).count("interrupted") == 1


async def test_stop_while_a_replacement_is_queued_discards_it() -> None:
    """Ctrl+X after an interrupt-and-submit but before the old task settles
    means the user changed their mind: the queued correction must not start
    once the task drains (review on #175)."""
    from korvid.ui.messages import AgentPromptSubmitted

    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "first")
        await until(pilot, lambda: len(session.prompts) == 1, label="turn running")
        task = app._agent_ui._task
        assert task is not None
        drained = asyncio.Event()
        task.add_done_callback(lambda _done: drained.set())
        app.on_agent_prompt_submitted(AgentPromptSubmitted("second"))
        app.action_interrupt_agent()  # same tick: the queue must be dropped
        await until(pilot, lambda: drained.is_set(), label="queued replacement drain ran")
        assert session.prompts == ["first"]  # the replacement never started
        assert app._agent_ui._replacement is None


async def test_stale_done_callback_cannot_consume_the_replacement() -> None:
    """The drain callback must be scoped to the task that completed: a
    callback from a superseded task must neither consume the queued
    replacement nor start a second concurrent turn (review on #175)."""
    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "first")
        await until(pilot, lambda: len(session.prompts) == 1, label="turn running")
        stale = asyncio.create_task(asyncio.sleep(0))
        await stale
        app._agent_ui._replacement = "queued"
        app._agent_ui._drain_replacement(stale)  # not the current agent task
        await pilot.pause()
        assert len(session.prompts) == 1  # no second turn was launched
        assert app._agent_ui._replacement == "queued"  # left for the real owner


# --- write safety (issue #170): interrupt vs the approval gate -------------


async def test_interrupt_while_awaiting_approval_dismisses_dialog(tmp_path: Any) -> None:
    """Cancelling a turn that is waiting on the approval dialog must pop
    the dialog and never execute the write (no orphaned modal whose 'y'
    would resolve a dead future)."""
    import pytest

    from korvid.ui.widgets.confirm_screen import ConfirmScreen

    rec = Recorder()
    app = make_write_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        _expand_panel(app)
        task = asyncio.ensure_future(
            app._agent_ui.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog shown")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await until(
            pilot,
            lambda: not isinstance(app.screen, ConfirmScreen),
            label="approval dialog dismissed after cancellation",
        )
        assert not isinstance(app.screen, ConfirmScreen)  # dialog cleaned up
        assert rec.calls == []  # the write never ran


async def test_interrupt_after_approval_lets_the_write_finish(tmp_path: Any) -> None:
    """Once the user approved, the audited write runs to completion even if
    the turn is cancelled mid-flight (audit fail-closed invariant)."""
    import json

    import pytest

    from korvid.k8s.discovery import ResourceMeta
    from korvid.ui.widgets.confirm_screen import ConfirmScreen

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
        _expand_panel(app)
        task = asyncio.ensure_future(
            app._agent_ui.agent_request_write("delete", "deployments", "web", namespace="default")
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

    class ShieldProbe:
        def __init__(
            self,
            original: Callable[[asyncio.Future[Any] | Awaitable[Any]], asyncio.Future[Any]],
        ) -> None:
            self._original = original
            self.await_count = 0
            self.resumed_after_first_cancel = asyncio.Event()
            self.resumed_after_second_cancel = asyncio.Event()

        def shield(self, awaitable: asyncio.Future[Any] | Awaitable[Any]) -> asyncio.Future[Any]:
            self.await_count += 1
            if self.await_count >= 2:
                self.resumed_after_first_cancel.set()
            if self.await_count >= 3:
                self.resumed_after_second_cancel.set()
            return self._original(awaitable)

    rec = SlowRecorder()
    audit_path = tmp_path / "audit.jsonl"
    probe = ShieldProbe(asyncio.shield)
    # Patched on the module that owns the shielded write loop: the app no
    # longer runs one itself, `WriteCoordinator` does.
    with patch("korvid.ui.write_coordinator.asyncio.shield", new=probe.shield):
        app = make_write_app(rec, audit_path)
        async with app.run_test() as pilot:
            _expand_panel(app)
            task = asyncio.ensure_future(
                app._agent_ui.agent_request_write(
                    "delete", "deployments", "web", namespace="default"
                )
            )
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog shown")
            await pilot.press("y")
            await until(pilot, lambda: rec.started.is_set(), label="write in flight")
            task.cancel()
            await until(
                pilot,
                lambda: probe.resumed_after_first_cancel.is_set(),
                label="shield loop resumed after first cancellation",
            )
            assert not task.done()
            assert rec.calls == []
            task.cancel()
            await until(
                pilot,
                lambda: probe.resumed_after_second_cancel.is_set(),
                label="shield loop resumed after second cancellation",
            )
            assert not task.done()
            assert rec.calls == []
            rec.gate.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await until(pilot, lambda: bool(rec.calls), label="write completed")
            assert rec.calls == [("delete", "deployments", "default", "web")]
            lines = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
            assert lines[-1]["outcome"] == "success"  # never intent-with-no-outcome


# ---------------------------------------------------------------------------
# Session lifecycle (issue #316 task 12)
# ---------------------------------------------------------------------------


async def test_stop_signals_the_session_before_cancelling_the_task() -> None:
    """`session.interrupt()` is the cooperative stop: it must reach the
    session *before* the task is cancelled, or the session learns about
    the stop only through a CancelledError it cannot attribute."""
    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "check my pods")
        await until(pilot, lambda: bool(session.prompts), label="turn running")
        app.action_interrupt_agent()
        await until(pilot, lambda: session.finalized == 1, label="finalized")
        assert session.interrupts == 1
        # The generator is released before the outcome is minted, so the
        # finalized token counts include whatever the drain observed.
        assert session.iterators_released == 1


async def test_interrupt_and_submit_signals_the_session_first() -> None:
    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "first")
        await until(pilot, lambda: bool(session.prompts), label="turn running")
        app.query_one(AgentPanel).post_message(AgentPromptSubmitted("second"))
        await until(pilot, lambda: session.prompts == ["first", "second"], label="replaced")
        assert session.interrupts >= 1


async def test_finalize_is_skipped_when_the_session_has_nothing_pending() -> None:
    """A turn that ended on its own leaves nothing to finalize; calling
    `finalize_interrupt` anyway raises out of the drain."""
    session = FakeSession([TurnComplete(input_tokens=1, output_tokens=1, estimated=False)])
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "check")
        await until(pilot, lambda: session.iterators_released == 1, label="turn finished")
        app.action_interrupt_agent()
        await pilot.pause()
        assert session.finalized == 0
        assert session.finalization_pending is False


async def test_cancellation_during_iterator_cleanup_is_not_swallowed() -> None:
    """Task 11 close-join obligation: a stop whose generator cleanup is
    still running must not absorb the *caller's* cancellation. The drain
    task has to end cancelled, not as a normal return."""
    session = BlockingSession()
    session.release_gate = asyncio.Event()
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "check")
        await until(pilot, lambda: bool(session.prompts), label="turn running")
        task = app._agent_ui._task
        assert task is not None
        app.action_interrupt_agent()
        # The drain is now parked inside the iterator's cleanup.
        await until(pilot, lambda: session.releasing.is_set(), label="cleanup running")
        task.cancel()
        session.release_gate.set()
        await until(pilot, lambda: task.done(), label="task settled")
        assert task.cancelled()


async def test_shutdown_closes_the_session_exactly_once() -> None:
    session = BlockingSession()
    app = make_app(session)
    async with app.run_test() as pilot:
        await _start_turn(app, pilot, "check")
        await until(pilot, lambda: bool(session.prompts), label="turn running")
    assert session.closed == 1
