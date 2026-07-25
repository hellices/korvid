"""AgentPanel: conversational chat panel for the LLM agent.

UX modelled on VS Code chat / Claude Code: token-level streaming into a
message block, a live status line with a spinner while the turn runs, and
tool calls rendered as friendly one-line actions that update in place.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Input, Static

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
    "Agent not configured.\n\nRun :ai to configure the agent,\nor edit ~/.config/korvid/config.yaml"
)

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# (running, done) label templates per tool. Placeholders are filled from the
# call's JSON arguments; missing keys fall back to the bare tool name.
_TOOL_LABELS: dict[str, tuple[str, str]] = {
    "list_resources": ("listing {kind}", "listed {kind}"),
    "get_resource": ("reading {kind}/{name}", "read {kind}/{name}"),
    "get_logs": ("reading logs of {pod}", "read logs of {pod}"),
    "get_events": ("checking events of {name}", "checked events of {name}"),
    "navigate": ("switching screen to {view}", "screen → {view}"),
    "set_filter": ("filtering screen rows", "screen → filter applied"),
    "open_logs": ("opening logs of {pod} on screen", "screen → logs of {pod}"),
    "open_describe": ("opening describe of {name} on screen", "screen → describe of {name}"),
}


def _fmt_tokens(n: int) -> str:
    """Format token counts: 950 -> '950', 12345 -> '12.3k'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _tool_label(name: str, arguments: str, *, done: bool) -> str:
    """Human-friendly one-line description of a tool call."""
    args: dict[str, Any] = {}
    try:
        parsed = json.loads(arguments) if arguments else {}
        if isinstance(parsed, dict):
            args = parsed
    except ValueError:
        pass
    templates = _TOOL_LABELS.get(name)
    if templates is None:
        return name
    try:
        label = templates[1 if done else 0].format_map(
            {k: str(v) for k, v in args.items()},
        )
    except (KeyError, IndexError):
        return name
    ns = args.get("namespace")
    if ns and "{namespace}" not in templates[0]:
        label += f" ({ns})"
    return label


class ChatEntry(Static):
    """One conversation entry (user block, agent message, tool line, error).

    Keeps the plain-text source in ``raw`` so transcripts can be read back
    (tests, future copy-to-clipboard) without unrendering rich content.
    """

    def __init__(self, content: RenderableType, *, raw: str, classes: str) -> None:
        super().__init__(content, classes=f"chat-entry {classes}")
        self.raw = raw

    def set_content(self, content: RenderableType, raw: str) -> None:
        self.raw = raw
        self.update(content)


class AgentPanel(Vertical):
    """Right-docked agent chat panel: header, conversation, status, input."""

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
    AgentPanel #agent-chat {
        height: 1fr;
        padding: 0 1;
    }
    AgentPanel #agent-status {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    AgentPanel #agent-input {
        dock: bottom;
    }
    AgentPanel .user-msg {
        margin: 1 0 0 0;
        padding: 0 1;
        border-left: thick $accent;
        color: $text;
        background: $boost;
    }
    AgentPanel .agent-msg {
        margin: 0 0 0 0;
        padding: 0 0 0 1;
    }
    AgentPanel .tool-line {
        padding: 0 0 0 1;
    }
    AgentPanel .error-msg {
        padding: 0 1;
        color: $text-error;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._model = "agent"
        self._tok_in = 0
        self._tok_out = 0
        self._estimated = False
        self._stream_widget: ChatEntry | None = None
        self._stream_text = ""
        self._tool_widgets: dict[str, ChatEntry] = {}
        self._tool_args: dict[str, str] = {}
        self.status_text = ""
        self._status_timer: Timer | None = None
        self._spinner_frame = 0

    def compose(self) -> ComposeResult:
        yield Static("⚡ agent", id="agent-header")
        yield VerticalScroll(id="agent-chat")
        yield Static("", id="agent-status")
        yield Input(id="agent-input", placeholder="Ask about the cluster…")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.post_message(AgentPromptSubmitted(text))

    # --- header -----------------------------------------------------------

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

    # --- conversation -----------------------------------------------------

    def show_setup_hint(self) -> None:
        chat = self.query_one("#agent-chat", VerticalScroll)
        # Setup-only chat: clear first so repeated Ctrl-A toggles don't
        # append duplicate hints.
        chat.remove_children()
        chat.mount(ChatEntry(_SETUP_HINT, raw=_SETUP_HINT, classes="agent-msg"))
        self.query_one("#agent-input", Input).disabled = True

    def begin_turn(self, user_text: str) -> None:
        self._mount_entry(ChatEntry(Text(user_text), raw=user_text, classes="user-msg"))
        self.query_one("#agent-input", Input).disabled = True
        self._stream_widget = None
        self._stream_text = ""
        self._set_status("thinking")

    def apply_event(self, event: AgentEvent) -> None:
        if isinstance(event, TextDelta):
            self._append_text(event.text)
        elif isinstance(event, ToolCallStarted):
            self._end_stream()
            label = _tool_label(event.name, event.arguments, done=False)
            entry = ChatEntry(
                Text.assemble(("● ", "yellow"), (f"{label}…", "dim")),
                raw=f"● {label}…",
                classes="tool-line",
            )
            self._tool_widgets[event.call_id] = entry
            self._tool_args[event.call_id] = event.arguments
            self._mount_entry(entry)
            self._set_status(label)
        elif isinstance(event, ToolCallFinished):
            self._finish_tool(event)
            self._set_status("thinking")
        elif isinstance(event, AgentError):
            self._end_stream()
            self._mount_entry(
                ChatEntry(
                    Text(f"✗ {event.message}"),
                    raw=f"✗ {event.message}",
                    classes="error-msg",
                )
            )
            self._clear_status()
            # AgentError may be terminal (provider failure) — let the user retry.
            self.query_one("#agent-input", Input).disabled = False
        elif isinstance(event, TurnComplete):
            self._end_stream()
            self._clear_status()
            self.query_one("#agent-input", Input).disabled = False
            self.set_header(
                self._model,
                self._tok_in + event.input_tokens,
                self._tok_out + event.output_tokens,
                self._estimated or event.estimated,
            )

    # --- internals ----------------------------------------------------------

    def _mount_entry(self, entry: ChatEntry) -> None:
        chat = self.query_one("#agent-chat", VerticalScroll)
        chat.mount(entry)
        chat.call_after_refresh(chat.scroll_end, animate=False)

    def _append_text(self, text: str) -> None:
        """Stream deltas token-by-token into the current agent message."""
        self._stream_text += text
        if self._stream_widget is None:
            self._stream_widget = ChatEntry("", raw="", classes="agent-msg")
            self._mount_entry(self._stream_widget)
        self._stream_widget.set_content(
            Markdown(self._stream_text),
            self._stream_text,
        )
        chat = self.query_one("#agent-chat", VerticalScroll)
        chat.call_after_refresh(chat.scroll_end, animate=False)

    def _end_stream(self) -> None:
        self._stream_widget = None
        self._stream_text = ""

    def _finish_tool(self, event: ToolCallFinished) -> None:
        entry = self._tool_widgets.pop(event.call_id, None)
        arguments = self._tool_args.pop(event.call_id, "")
        label = _tool_label(event.name, arguments, done=event.ok)
        if entry is None:
            entry = ChatEntry("", raw="", classes="tool-line")
            self._mount_entry(entry)
        if event.ok:
            entry.set_content(
                Text.assemble(("● ", "green"), (label, "dim")),
                f"● {label}",
            )
        else:
            raw = f"● {label} — {event.summary}"
            entry.set_content(
                Text.assemble(("● ", "red"), (label, "dim"), (f" — {event.summary}", "red")),
                raw,
            )

    # --- status / spinner ---------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status_text = text
        if self._status_timer is None:
            self._status_timer = self.set_interval(0.1, self._tick_spinner)
        self._tick_spinner()

    def _clear_status(self) -> None:
        self.status_text = ""
        if self._status_timer is not None:
            self._status_timer.stop()
            self._status_timer = None
        self.query_one("#agent-status", Static).update("")

    def _tick_spinner(self) -> None:
        frame = _SPINNER_FRAMES[self._spinner_frame % len(_SPINNER_FRAMES)]
        self._spinner_frame += 1
        self.query_one("#agent-status", Static).update(
            Text(f"{frame} {self.status_text}…", style="dim")
        )
