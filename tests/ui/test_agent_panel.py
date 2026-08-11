"""Tests for AgentPanel: conversational chat panel (VS Code chat-style UX)."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from korvid.agent.events import (
    AgentError,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
)
from korvid.ui.messages import AgentPromptSubmitted
from korvid.ui.widgets.agent_panel import AgentPanel, ChatEntry


class PanelApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def compose(self) -> ComposeResult:
        yield AgentPanel()

    def on_agent_prompt_submitted(self, msg: AgentPromptSubmitted) -> None:
        self.prompts.append(msg.text)


def _log_text(app: PanelApp) -> str:
    return "\n".join(entry.raw for entry in app.query(ChatEntry))


def _status_text(app: PanelApp) -> str:
    panel = app.query_one(AgentPanel)
    return panel.status_text


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


async def test_header_shows_small_profile_marker() -> None:
    """Users must be able to see which capability mode the agent runs in
    (issue #71); full stays unmarked so the frontier look is unchanged."""
    app = PanelApp()
    async with app.run_test():
        panel = app.query_one(AgentPanel)
        panel.set_header("qwen3:8b", 100, 20, estimated=False, profile="small")
        header = str(app.query_one("#agent-header", Static).render())
        assert "qwen3:8b [small]" in header

        panel.set_header("gpt-4o", 100, 20, estimated=False, profile="full")
        header = str(app.query_one("#agent-header", Static).render())
        assert "[full]" not in header
        assert "gpt-4o" in header


async def test_header_profile_marker_survives_turn_complete() -> None:
    """TurnComplete re-renders the header from panel state; the profile
    marker must not disappear after the first turn."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.set_header("qwen3:8b", 0, 0, estimated=False, profile="small")
        panel.begin_turn("q")
        panel.apply_event(TurnComplete(input_tokens=7, output_tokens=3, estimated=False))
        await pilot.pause()
        header = str(app.query_one("#agent-header", Static).render())
        assert "[small]" in header


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
