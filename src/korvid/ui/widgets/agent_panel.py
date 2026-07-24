"""AgentPanel: docked chat panel for the LLM agent (plan 4, slice 1)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, RichLog, Static

from korvid.agent.events import (
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
)
from korvid.ui.messages import AgentPromptSubmitted

_SETUP_HINT = (
    "Agent not configured.\n"
    "\n"
    "Add to ~/.config/korvid/config.yaml:\n"
    "\n"
    "  agent:\n"
    "    provider: openai-compat\n"
    "    base_url: http://localhost:11434/v1\n"
    "    model: llama3\n"
    "    api_key_env: KORVID_API_KEY  # optional\n"
)


def _fmt_tokens(n: int) -> str:
    """Format token counts: 950 -> '950', 12345 -> '12.3k'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


class AgentPanel(Vertical):
    """Right-docked agent chat panel: header, conversation log, prompt input."""

    DEFAULT_CSS = """
    AgentPanel {
        width: 40%;
        dock: right;
        border-left: solid $accent;
    }
    AgentPanel #agent-header {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    AgentPanel #agent-log {
        height: 1fr;
    }
    AgentPanel #agent-input {
        dock: bottom;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending = ""
        self._model = "agent"
        self._tok_in = 0
        self._tok_out = 0
        self._estimated = False

    def compose(self) -> ComposeResult:
        yield Static("⚡ agent", id="agent-header")
        yield RichLog(id="agent-log", wrap=True, highlight=False, markup=False)
        yield Input(id="agent-input", placeholder="Ask about the cluster…")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.post_message(AgentPromptSubmitted(text))

    def set_header(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated: bool,
    ) -> None:
        self._model = model
        self._tok_in = input_tokens
        self._tok_out = output_tokens
        self._estimated = estimated
        prefix = "~" if estimated else ""
        self.query_one("#agent-header", Static).update(
            f"⚡ {model} · {prefix}↑{_fmt_tokens(input_tokens)} ↓{_fmt_tokens(output_tokens)} tok"
        )

    def show_setup_hint(self) -> None:
        log = self.query_one("#agent-log", RichLog)
        # Setup-only log: clear first so repeated Ctrl-A toggles don't
        # append duplicate hints.
        log.clear()
        log.write(_SETUP_HINT)
        self.query_one("#agent-input", Input).disabled = True

    def begin_turn(self, user_text: str) -> None:
        log = self.query_one("#agent-log", RichLog)
        log.write(f"> {user_text}")
        self.query_one("#agent-input", Input).disabled = True
        self._pending = ""

    def apply_event(self, event: AgentEvent) -> None:
        log = self.query_one("#agent-log", RichLog)
        if isinstance(event, TextDelta):
            self._append_text(log, event.text)
        elif isinstance(event, ToolCallStarted):
            self._flush(log)
            args = event.arguments
            if len(args) > 40:
                args = args[:40] + "…"
            log.write(f"🔧 {event.name}({args}) …")
        elif isinstance(event, ToolCallFinished):
            if event.ok:
                log.write(f"🔧 {event.name} ✓")
            else:
                log.write(f"🔧 {event.name} ✗ {event.summary}")
        elif isinstance(event, AgentError):
            self._flush(log)
            log.write(Text(f"[error] {event.message}", style="red"))
            # AgentError may be terminal (provider failure) — let the user retry.
            self.query_one("#agent-input", Input).disabled = False
        elif isinstance(event, TurnComplete):
            self._flush(log)
            self.query_one("#agent-input", Input).disabled = False
            self.set_header(
                self._model,
                self._tok_in + event.input_tokens,
                self._tok_out + event.output_tokens,
                self._estimated or event.estimated,
            )

    def _append_text(self, log: RichLog, text: str) -> None:
        """Buffer streamed deltas; emit a log line per completed newline."""
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            log.write(line)

    def _flush(self, log: RichLog) -> None:
        if self._pending:
            log.write(self._pending)
            self._pending = ""
