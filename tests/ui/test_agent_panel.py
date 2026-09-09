"""Tests for AgentPanel: conversational chat panel (VS Code chat-style UX)."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from korvid.agent.diagnostics import (
    AgentPhase,
    ProviderRoundDiagnostics,
    ToolDiagnostics,
    TurnDiagnostics,
    TurnOutcome,
    format_diagnostics,
)
from korvid.agent.events import (
    AgentError,
    AgentEvent,
    AgentPhaseChanged,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
    TurnInterrupted,
)
from korvid.ui.messages import AgentPromptSubmitted
from korvid.ui.widgets.agent_panel import AgentPanel, ChatEntry


class PanelApp(App[None]):
    def __init__(self, panel_type: type[AgentPanel] = AgentPanel) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self._panel_type = panel_type

    def compose(self) -> ComposeResult:
        yield self._panel_type()

    def on_agent_prompt_submitted(self, msg: AgentPromptSubmitted) -> None:
        self.prompts.append(msg.text)


def _log_text(app: PanelApp) -> str:
    return "\n".join(entry.raw for entry in app.query(ChatEntry))


def _status_text(app: PanelApp) -> str:
    panel = app.query_one(AgentPanel)
    return panel.status_text


class DispatchProbePanel(AgentPanel):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def _apply_text_delta(self, event: TextDelta) -> None:
        self.calls.append("text")

    def _apply_tool_started(self, event: ToolCallStarted) -> None:
        self.calls.append("tool-started")

    def _apply_tool_finished(self, event: ToolCallFinished) -> None:
        self.calls.append("tool-finished")

    def _apply_agent_error(self, event: AgentError) -> None:
        self.calls.append("error")

    def _apply_turn_complete(self, event: TurnComplete) -> None:
        self.calls.append("complete")

    def _apply_turn_interrupted(self, event: TurnInterrupted) -> None:
        self.calls.append("interrupted")


# --- prompt input ---


async def test_prompt_submitted_posted_and_input_cleared() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        inp = app.query_one("#agent-input", Input)
        inp.focus()
        await pilot.pause()
        inp.value = "why is my pod crashing?"
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompts == ["why is my pod crashing?"]
        assert inp.value == ""


async def test_empty_prompt_not_posted() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        inp = app.query_one("#agent-input", Input)
        inp.focus()
        await pilot.pause()
        inp.value = "   "
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompts == []


# --- streaming ---


async def test_partial_delta_streams_immediately() -> None:
    """Text must appear as it arrives — not buffered until a newline."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(TextDelta(text="Looking at your"))
        await pilot.pause()
        assert "Looking at your" in _log_text(app)


async def test_apply_event_dispatches_every_event_to_its_typed_handler() -> None:
    events: list[tuple[AgentEvent, str]] = [
        (TextDelta(text="x"), "text"),
        (ToolCallStarted(call_id="1", name="get_logs", arguments="{}"), "tool-started"),
        (ToolCallFinished(call_id="1", name="get_logs", ok=True, summary=""), "tool-finished"),
        (AgentError(message="boom"), "error"),
        (TurnComplete(input_tokens=1, output_tokens=2, estimated=False), "complete"),
        (TurnInterrupted(input_tokens=1, output_tokens=2, estimated=False), "interrupted"),
    ]
    app = PanelApp(DispatchProbePanel)
    async with app.run_test():
        panel = app.query_one(DispatchProbePanel)
        for event, expected in events:
            panel.apply_event(event)
            assert panel.calls.pop() == expected


async def test_text_deltas_accumulate_in_one_message() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(TextDelta(text="Hello "))
        panel.apply_event(TextDelta(text="world.\nSecond "))
        panel.apply_event(TextDelta(text="line."))
        panel.apply_event(TurnComplete(input_tokens=1, output_tokens=2, estimated=True))
        await pilot.pause()
        text = _log_text(app)
        assert "Hello world." in text
        assert "Second line." in text
        # One agent message widget, not one per delta.
        assert len(app.query(".agent-msg")) == 1


async def test_user_message_is_distinct_entry() -> None:
    """The user's message renders as its own styled block, visually distinct
    from agent output."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("why is my pod crashing?")
        panel.apply_event(TextDelta(text="Let me check."))
        await pilot.pause()
        users = list(app.query(".user-msg"))
        agents = list(app.query(".agent-msg"))
        assert len(users) == 1
        assert len(agents) == 1
        assert users[0].raw == "why is my pod crashing?"  # type: ignore[attr-defined]  # ChatEntry.raw


# --- progress status ---


async def test_status_shows_thinking_during_turn() -> None:
    """While a turn is running the user must see live progress, so 'is it
    still working?' is never ambiguous."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        await pilot.pause()
        assert "thinking" in _status_text(app)


async def test_status_shows_tool_activity() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(
            ToolCallStarted(call_id="c1", name="list_resources", arguments='{"kind": "pods"}')
        )
        await pilot.pause()
        assert "pods" in _status_text(app)


async def test_status_cleared_when_turn_completes() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(TextDelta(text="done"))
        panel.apply_event(TurnComplete(input_tokens=1, output_tokens=1, estimated=False))
        await pilot.pause()
        assert _status_text(app) == ""


async def test_status_cleared_on_error() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(AgentError(message="boom"))
        await pilot.pause()
        assert _status_text(app) == ""


# --- tool call rendering ---


async def test_tool_call_renders_friendly_label_not_json() -> None:
    """Tool lines read like actions ('listing pods'), not raw JSON."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("check pods")
        panel.apply_event(
            ToolCallStarted(
                call_id="c1",
                name="list_resources",
                arguments='{"kind": "pods", "namespace": "app"}',
            )
        )
        await pilot.pause()
        text = _log_text(app)
        assert "pods" in text
        assert '{"kind"' not in text


async def test_tool_call_line_updates_in_place_on_finish() -> None:
    """Finishing a tool call updates its line — no separate '✓' row."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("check pods")
        panel.apply_event(
            ToolCallStarted(call_id="c1", name="list_resources", arguments='{"kind": "pods"}')
        )
        before = len(app.query(ChatEntry))
        panel.apply_event(
            ToolCallFinished(call_id="c1", name="list_resources", ok=True, summary="")
        )
        await pilot.pause()
        assert len(app.query(ChatEntry)) == before  # updated, not appended


async def test_failed_tool_call_shows_error_summary() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("check pods")
        panel.apply_event(
            ToolCallStarted(call_id="c1", name="get_logs", arguments='{"pod": "web-1"}')
        )
        panel.apply_event(
            ToolCallFinished(call_id="c1", name="get_logs", ok=False, summary="404 not found")
        )
        await pilot.pause()
        assert "404 not found" in _log_text(app)


async def test_write_tool_calls_keep_warning_marker() -> None:
    """Write tools carry the warning marker through start and finish so a
    cluster mutation is never rendered like a harmless read."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("scale it down")
        panel.apply_event(
            ToolCallStarted(
                call_id="c1",
                name="delete_resource",
                arguments='{"kind": "pods", "name": "web-1", "namespace": "app"}',
            )
        )
        await pilot.pause()
        assert "⚠" in _log_text(app)  # visible while the request is pending
        panel.apply_event(
            ToolCallFinished(call_id="c1", name="delete_resource", ok=True, summary="")
        )
        await pilot.pause()
        assert "⚠" in _log_text(app)  # still visible after completion


async def test_ui_tool_calls_read_as_screen_actions() -> None:
    """UI-driving tools must read as screen actions so the user understands
    the agent changed what they see."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("show me")
        panel.apply_event(
            ToolCallStarted(call_id="c1", name="open_logs", arguments='{"pod": "web-1"}')
        )
        panel.apply_event(ToolCallFinished(call_id="c1", name="open_logs", ok=True, summary=""))
        await pilot.pause()
        assert "screen" in _log_text(app)


async def test_text_after_tool_call_starts_new_message() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(TextDelta(text="Checking."))
        panel.apply_event(
            ToolCallStarted(call_id="c1", name="list_resources", arguments='{"kind": "pods"}')
        )
        panel.apply_event(
            ToolCallFinished(call_id="c1", name="list_resources", ok=True, summary="")
        )
        panel.apply_event(TextDelta(text="Found it."))
        await pilot.pause()
        assert len(app.query(".agent-msg")) == 2


async def test_cluster_and_ui_tools_have_distinct_markers() -> None:
    """Screen mutations (🖥) must be scannable apart from cluster reads (🔧)."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("go")
        panel.apply_event(
            ToolCallStarted(call_id="r1", name="list_resources", arguments='{"kind": "pods"}')
        )
        panel.apply_event(
            ToolCallStarted(call_id="u1", name="navigate", arguments='{"view": "pods"}')
        )
        panel.apply_event(
            ToolCallFinished(call_id="r1", name="list_resources", ok=True, summary="")
        )
        panel.apply_event(ToolCallFinished(call_id="u1", name="navigate", ok=True, summary=""))
        await pilot.pause()
        raws = [e.raw for e in app.query(ChatEntry)]
        read_line = next(r for r in raws if "pods" in r and "screen" not in r)
        ui_line = next(r for r in raws if "screen" in r)
        assert read_line.startswith("🔧")
        assert ui_line.startswith("🖥")


async def test_drill_down_shows_ui_marker_and_readable_label() -> None:
    """drill_down mutates the screen: it must get the 🖥 marker and a
    human-readable label, not the raw tool name with a cluster-read marker."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("go")
        panel.apply_event(
            ToolCallStarted(call_id="d1", name="drill_down", arguments='{"name": "web"}')
        )
        panel.apply_event(ToolCallFinished(call_id="d1", name="drill_down", ok=True, summary=""))
        await pilot.pause()
        raws = [e.raw for e in app.query(ChatEntry)]
        line = next(r for r in raws if "web" in r)
        assert line.startswith("🖥")
        assert "drilled into web" in line


async def test_begin_turn_drops_stale_tool_state() -> None:
    """A ToolCallFinished left over from a previous (errored) turn must not
    touch the new turn's transcript — no in-place flip, no new row."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("t1")
        panel.apply_event(
            ToolCallStarted(call_id="c1", name="list_resources", arguments='{"kind": "pods"}')
        )
        panel.apply_event(AgentError(message="provider died"))
        await pilot.pause()
        panel.begin_turn("t2")
        before = len(app.query(ChatEntry))
        panel.apply_event(
            ToolCallFinished(call_id="c1", name="list_resources", ok=True, summary="")
        )
        await pilot.pause()
        assert len(app.query(ChatEntry)) == before  # no new row for a stale call
        # the interrupted tool line still reads as unfinished
        assert any(e.raw.endswith("…") for e in app.query(ChatEntry))


async def test_stream_renders_markdown_only_when_message_ends() -> None:
    """Re-parsing the whole accumulated response as Markdown on every token
    is O(n^2); stream cheap, render Markdown once when the message ends."""
    from rich.markdown import Markdown

    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(TextDelta(text="**bold"))
        panel.apply_event(TextDelta(text="** rest"))
        await pilot.pause()
        entry = next(e for e in app.query(ChatEntry) if e.has_class("agent-msg"))
        assert not isinstance(entry.content, Markdown)
        panel.apply_event(TurnComplete(input_tokens=1, output_tokens=1, estimated=False))
        await pilot.pause()
        assert isinstance(entry.content, Markdown)
        assert entry.raw == "**bold** rest"


# --- errors / input state ---


async def test_agent_error_rendered() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(AgentError(message="provider unreachable"))
        await pilot.pause()
        assert "provider unreachable" in _log_text(app)


async def test_setup_hint_disables_input() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.show_setup_hint()
        await pilot.pause()
        assert app.query_one("#agent-input", Input).disabled is True
        assert ":ai" in _log_text(app)


async def test_input_stays_enabled_during_turn_and_after_complete() -> None:
    """Since issue #170 the input never locks during a turn: typing while
    the agent runs is interrupt-and-submit, not an error."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        inp = app.query_one("#agent-input", Input)
        panel.begin_turn("hi")
        await pilot.pause()
        assert inp.disabled is False
        panel.apply_event(TurnComplete(input_tokens=1, output_tokens=1, estimated=False))
        await pilot.pause()
        assert inp.disabled is False


async def test_agent_error_reenables_input() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        inp = app.query_one("#agent-input", Input)
        panel.begin_turn("hi")
        panel.apply_event(AgentError(message="boom"))
        await pilot.pause()
        assert inp.disabled is False


# --- header ---


async def test_header_formats_tokens() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.set_header("claude", 12345, 950, estimated=False)
        await pilot.pause()
        header = app.query_one("#agent-header", Static)
        text = str(header.render())
        assert "claude" in text
        assert "12.3k" in text
        assert "950" in text


async def test_turn_complete_updates_header_cumulatively() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.set_header("m", 100, 50, estimated=False)
        panel.apply_event(TurnComplete(input_tokens=100, output_tokens=25, estimated=False))
        await pilot.pause()
        header = app.query_one("#agent-header", Static)
        text = str(header.render())
        assert "200" in text
        assert "75" in text


async def test_header_shows_the_resolved_tier_and_its_provenance() -> None:
    """The old `[full]`/`[small]` capability-profile marker is gone: what the
    panel shows now is the tier the *session* resolved plus where the
    decision came from, so a silent fallback is visible as a fallback."""
    app = PanelApp()
    async with app.run_test():
        panel = app.query_one(AgentPanel)
        panel.set_header("qwen3:8b", 100, 20, estimated=False, tier="low (catalog)")
        header = str(app.query_one("#agent-header", Static).render())
        assert "low (catalog)" in header
        assert "[" not in header  # never the old bracketed profile marker
        assert "qwen3:8b" in header

        panel.set_header("gpt-4o", 100, 20, estimated=False, tier="high (user)")
        header = str(app.query_one("#agent-header", Static).render())
        assert "high (user)" in header
        assert "low" not in header


async def test_header_without_a_tier_shows_no_marker() -> None:
    """No session, no tier: the header must not invent one."""
    app = PanelApp()
    async with app.run_test():
        panel = app.query_one(AgentPanel)
        panel.set_header("qwen3:8b", 100, 20, estimated=False)
        header = str(app.query_one("#agent-header", Static).render())
        assert "(" not in header
        assert "qwen3:8b" in header


async def test_header_replays_the_tier_after_turn_complete() -> None:
    """TurnComplete re-renders the header from cached panel state; the tier
    must ride along with the token counts, not be dropped on the first turn."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.set_header("qwen3:8b", 0, 0, estimated=False, tier="low (fallback)")
        panel.begin_turn("q")
        panel.apply_event(TurnComplete(input_tokens=7, output_tokens=3, estimated=False))
        await pilot.pause()
        header = str(app.query_one("#agent-header", Static).render())
        assert "low (fallback)" in header
        assert "[" not in header


async def test_header_replays_the_tier_after_an_interrupt() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.set_header("qwen3:8b", 0, 0, estimated=False, tier="high (provider)")
        panel.begin_turn("q")
        panel.apply_event(TurnInterrupted(input_tokens=2, output_tokens=1, estimated=True))
        await pilot.pause()
        header = str(app.query_one("#agent-header", Static).render())
        assert "high (provider)" in header


async def test_unsupported_citations_are_marked_after_the_answer() -> None:
    """An invented reference has to be visible, not left looking sourced.

    The answer text stays exactly as the model wrote it; the warning is
    appended as korvid's own note (issue #192).
    """
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("what is wrong?")
        panel.apply_event(TextDelta(text="the pod is up [E1] and the node is fine [E9]"))
        panel.apply_event(
            TurnComplete(
                input_tokens=1,
                output_tokens=2,
                estimated=False,
                cited=("E1",),
                uncited=("E9",),
            )
        )
        await pilot.pause()

        text = _log_text(app)
        assert "the pod is up [E1] and the node is fine [E9]" in text
        assert "E9" in text
        assert "unsupported" in text.lower()


async def test_a_repeated_citation_is_marked() -> None:
    """Repetition is reported, since it is not extra support."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("what is wrong?")
        panel.apply_event(TextDelta(text="up [E1], still up [E1]"))
        panel.apply_event(
            TurnComplete(
                input_tokens=1,
                output_tokens=2,
                estimated=False,
                cited=("E1",),
                duplicated=("E1",),
            )
        )
        await pilot.pause()

        assert "cited more than once" in _log_text(app).lower()


async def test_a_clean_answer_gets_no_citation_note() -> None:
    """No noise when every citation resolves."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("what is wrong?")
        panel.apply_event(TextDelta(text="the pod is up [E1]"))
        panel.apply_event(
            TurnComplete(input_tokens=1, output_tokens=2, estimated=False, cited=("E1",))
        )
        await pilot.pause()

        assert "unsupported" not in _log_text(app).lower()


# --- latency diagnostics (issue #319) ---


def _diag(
    outcome: TurnOutcome, *, rounds: int = 1, tools: tuple[ToolDiagnostics, ...] = ()
) -> TurnDiagnostics:
    return TurnDiagnostics(
        correlation_id="corr-1234",
        outcome=outcome,
        total_seconds=3.0,
        rounds=tuple(
            ProviderRoundDiagnostics(
                round_number=n + 1,
                prepare_seconds=0.1,
                handoff_seconds=0.1,
                first_event_seconds=0.5,
                total_seconds=1.0,
            )
            for n in range(rounds)
        ),
        tools=tools,
    )


async def test_phase_waiting_for_model_updates_status() -> None:
    """A turn that records diagnostics distinguishes waiting for the model
    from running a tool or composing — the status line says which."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(AgentPhaseChanged(phase=AgentPhase.WAITING_FOR_MODEL, round_number=1))
        await pilot.pause()
        assert "waiting for model" in _status_text(app)


async def test_phase_running_tool_names_the_tool() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(
            AgentPhaseChanged(phase=AgentPhase.RUNNING_TOOL, round_number=1, tool="get_logs")
        )
        await pilot.pause()
        assert "get_logs" in _status_text(app)


async def test_phase_composing_answer_updates_status() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(AgentPhaseChanged(phase=AgentPhase.COMPOSING_ANSWER, round_number=2))
        await pilot.pause()
        assert "composing answer" in _status_text(app)


async def test_phase_change_dispatches_to_typed_handler() -> None:
    class Probe(AgentPanel):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []

        def _apply_phase_changed(self, event: AgentPhaseChanged) -> None:
            self.calls.append("phase")

    app = PanelApp(Probe)
    async with app.run_test():
        panel = app.query_one(Probe)
        panel.apply_event(AgentPhaseChanged(phase=AgentPhase.WAITING_FOR_MODEL, round_number=1))
        assert panel.calls == ["phase"]


async def test_turn_complete_renders_compact_diagnostics_summary() -> None:
    """A completed turn shows a single dim timing summary line."""
    app = PanelApp()
    snapshot = _diag(TurnOutcome.SUCCESS)
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(TextDelta(text="done"))
        panel.apply_event(
            TurnComplete(input_tokens=1, output_tokens=1, estimated=False, diagnostics=snapshot)
        )
        await pilot.pause()
        line = app.query_one(".diagnostics-line", ChatEntry)
        assert line.raw == format_diagnostics(snapshot)


async def test_turn_complete_without_diagnostics_shows_no_summary() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(TurnComplete(input_tokens=1, output_tokens=1, estimated=False))
        await pilot.pause()
        assert len(app.query(".diagnostics-line")) == 0


async def test_failed_turn_summary_is_not_shown_as_success() -> None:
    """A provider failure's timing summary must never read like a clean
    success: the outcome is named on the line."""
    app = PanelApp()
    snapshot = _diag(TurnOutcome.FAILED)
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(AgentError(message="provider exploded", diagnostics=snapshot))
        await pilot.pause()
        line = app.query_one(".diagnostics-line", ChatEntry)
        assert line.raw.startswith("failed · ")
        assert format_diagnostics(snapshot) in line.raw


async def test_interrupted_turn_summary_names_the_outcome() -> None:
    app = PanelApp()
    snapshot = _diag(TurnOutcome.INTERRUPTED)
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(
            TurnInterrupted(input_tokens=1, output_tokens=1, estimated=False, diagnostics=snapshot)
        )
        await pilot.pause()
        line = app.query_one(".diagnostics-line", ChatEntry)
        assert line.raw.startswith("interrupted · ")


async def test_recoverable_error_keeps_no_diagnostics_line() -> None:
    """A recoverable AgentError (no snapshot) mounts no timing line — the
    turn is not over."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("hi")
        panel.apply_event(AgentError(message="transient"))
        await pilot.pause()
        assert len(app.query(".diagnostics-line")) == 0
