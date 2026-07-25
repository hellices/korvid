"""Tests for Task 8: AgentPanel widget."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog

from korvid.agent.events import (
    AgentError,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
)
from korvid.ui.messages import AgentPromptSubmitted
from korvid.ui.widgets.agent_panel import AgentPanel


class PanelApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def compose(self) -> ComposeResult:
        yield AgentPanel()

    def on_agent_prompt_submitted(self, msg: AgentPromptSubmitted) -> None:
        self.prompts.append(msg.text)


def _log_text(app: PanelApp) -> str:
    log = app.query_one("#agent-log", RichLog)
    return "\n".join(strip.text for strip in log.lines)


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


async def test_text_deltas_accumulate_in_log() -> None:
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
        assert "> hi" in text
        assert "Hello world." in text
        assert "Second line." in text


async def test_tool_call_lines_rendered() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("q")
        panel.apply_event(
            ToolCallStarted(call_id="c1", name="get_manifest", arguments='{"kind":"pod"}')
        )
        panel.apply_event(
            ToolCallFinished(call_id="c1", name="get_manifest", ok=True, summary="apiVersion: v1")
        )
        panel.apply_event(
            ToolCallFinished(call_id="c2", name="get_logs", ok=False, summary="ERROR: not found")
        )
        await pilot.pause()
        text = _log_text(app)
        assert '🔧 get_manifest({"kind":"pod"}) …' in text
        assert "🔧 get_manifest ✓" in text
        assert "🔧 get_logs ✗ ERROR: not found" in text


async def test_agent_error_rendered() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("q")
        panel.apply_event(AgentError(message="connection refused"))
        await pilot.pause()
        assert "[error] connection refused" in _log_text(app)


async def test_setup_hint_disables_input() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.show_setup_hint()
        await pilot.pause()
        assert app.query_one("#agent-input", Input).disabled is True
        assert "Run :ai" in _log_text(app)


async def test_input_disabled_during_turn_reenabled_on_complete() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        inp = app.query_one("#agent-input", Input)
        panel.begin_turn("q")
        await pilot.pause()
        assert inp.disabled is True
        panel.apply_event(TurnComplete(input_tokens=10, output_tokens=5, estimated=False))
        await pilot.pause()
        assert inp.disabled is False


async def test_header_formats_tokens() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.set_header("llama3", 12345, 950, estimated=False)
        await pilot.pause()
        from textual.widgets import Static

        header = app.query_one("#agent-header", Static)
        assert "⚡ llama3 · ↑12.3k ↓950 tok" in str(header.render())

        panel.set_header("llama3", 100, 40, estimated=True)
        assert "~↑100" in str(header.render())


async def test_turn_complete_updates_header_cumulatively() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        from textual.widgets import Static

        panel = app.query_one(AgentPanel)
        panel.set_header("llama3", 100, 50, estimated=False)
        panel.begin_turn("q")
        panel.apply_event(TurnComplete(input_tokens=20, output_tokens=5, estimated=False))
        await pilot.pause()
        header = app.query_one("#agent-header", Static)
        assert "⚡ llama3 · ↑120 ↓55 tok" in str(header.render())


async def test_agent_error_reenables_input() -> None:
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        inp = app.query_one("#agent-input", Input)
        panel.begin_turn("q")
        panel.apply_event(AgentError(message="provider down"))
        await pilot.pause()
        assert inp.disabled is False


async def test_ui_tool_calls_use_screen_marker() -> None:
    """Slice 3: screen actions render with 🖥 so users can tell them from cluster reads."""
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin_turn("q")
        panel.apply_event(
            ToolCallStarted(call_id="c1", name="navigate", arguments='{"view":"pods"}')
        )
        panel.apply_event(
            ToolCallFinished(call_id="c1", name="navigate", ok=True, summary="switched")
        )
        panel.apply_event(
            ToolCallFinished(call_id="c2", name="set_filter", ok=False, summary="ERROR: x")
        )
        await pilot.pause()
        text = _log_text(app)
        assert '🖥 navigate({"view":"pods"}) …' in text
        assert "🖥 navigate ✓" in text
        assert "🖥 set_filter ✗ ERROR: x" in text
