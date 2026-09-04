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
    TurnInterrupted,
)
from korvid.tools.executor import UI_TOOL_NAMES, WRITE_TOOL_NAMES
from korvid.ui.messages import AgentPromptSubmitted

_SETUP_HINT = (
    "Agent not configured.\n\nRun :ai to configure the agent,\nor edit ~/.config/korvid/config.yaml"
)

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: Tools that mutate what's on screen (vs. read-only cluster queries).
#: Derived from the tool registry so a new UI tool can't silently fall back
#: to the cluster-read marker.
_UI_TOOLS = UI_TOOL_NAMES

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
    "drill_down": ("drilling into {name} on screen", "screen → drilled into {name}"),
    "delete_resource": ("requesting delete of {kind}/{name}", "delete request → {kind}/{name}"),
    "scale_resource": (
        "requesting scale of {kind}/{name} to {replicas}",
        "scale request → {kind}/{name} = {replicas}",
    ),
    "rollout_restart": (
        "requesting rollout restart of {kind}/{name}",
        "restart request → {kind}/{name}",
    ),
}


def _fmt_tokens(n: int) -> str:
    """Format token counts: 950 -> '950', 12345 -> '12.3k'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _tool_marker(name: str) -> str:
    """Cluster writes (⚠) vs screen mutations (🖥) vs cluster reads (🔧)."""
    if name in WRITE_TOOL_NAMES:
        return "⚠"
    return "🖥" if name in _UI_TOOLS else "🔧"


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
        self._tier: str | None = None
        self._tok_in = 0
        self._tok_out = 0
        self._estimated = False
        self._stream_widget: ChatEntry | None = None
        self._stream_text = ""
        self._stream_dirty = False
        self._flush_timer: Timer | None = None
        self._tool_widgets: dict[str, ChatEntry] = {}
        self._tool_args: dict[str, str] = {}
        self.status_text = ""
        # The advertised stop key (issue #170): the app resolves the
        # effective `interrupt_agent` binding so a remap moves the hint.
        self.stop_key = "ctrl+x"
        self._interrupt_marked = False
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
        tier: str | None = None,
    ) -> None:
        self._model = model
        self._tok_in = input_tokens
        self._tok_out = output_tokens
        self._estimated = estimated
        self._tier = tier
        prefix = "~" if estimated else ""
        # The *resolved* tier and where the routing decision came from —
        # never the requested override the wizard/config carry, so a
        # request the catalogue could not honour reads as the fallback it
        # became. No session, no marker: the panel invents nothing.
        routed = f"{tier} · " if tier else ""
        self.query_one("#agent-header", Static).update(
            f"⚡ {model} · {routed}"
            f"{prefix}↑{_fmt_tokens(input_tokens)} ↓{_fmt_tokens(output_tokens)} tok"
        )

    # --- conversation -----------------------------------------------------

    def show_setup_hint(self) -> None:
        chat = self.query_one("#agent-chat", VerticalScroll)
        # Setup-only chat: clear first so repeated Ctrl-A toggles don't
        # append duplicate hints.
        chat.remove_children()
        chat.mount(ChatEntry(_SETUP_HINT, raw=_SETUP_HINT, classes="agent-msg"))
        self.query_one("#agent-input", Input).disabled = True

    def show_reconnect_hint(self) -> None:
        """Disconnected state (issue #167): the transcript stays, prompt
        submission is disabled, and the way back is named. Idempotent —
        repeated panel toggles must not stack hint entries."""
        hint = "agent off — run :ai to reconnect"
        entries = list(self.query(ChatEntry))
        if not entries or entries[-1].raw != hint:
            self._mount_entry(ChatEntry(Text(hint, style="dim"), raw=hint, classes="agent-msg"))
        self.query_one("#agent-input", Input).disabled = True

    def echo_user(self, text: str) -> None:
        """Show a user message immediately, before its turn starts.

        Interrupt-and-submit (issue #170) echoes the correction the moment
        it is typed; the replacement turn then begins with `echo=False` so
        the message is not repeated. When a turn is still draining, the
        interrupted turn's transcript is settled first so the ⏹ marker
        attaches to the partial output, never to the echoed correction.
        """
        if self._status_timer is not None and not self._interrupt_marked:
            self._mark_interrupted()
        self._mount_entry(ChatEntry(Text(text), raw=text, classes="user-msg"))

    def _mark_interrupted(self) -> None:
        """Settle the interrupted turn's transcript: final-render the
        partial stream, mark unfinished tool lines, and append the ⏹
        marker adjacent to the output it describes."""
        self._end_stream()
        self._mark_tools_interrupted()
        self._mount_entry(
            ChatEntry(
                Text("⏹ interrupted", style="dim"),
                raw="⏹ interrupted",
                classes="agent-msg",
            )
        )
        self._interrupt_marked = True

    def begin_turn(self, user_text: str, *, echo: bool = True) -> None:
        if echo:
            self.echo_user(user_text)
        # The input stays enabled during the turn (issue #170): typing a
        # correction mid-turn is interrupt-and-submit, not an error.
        self._stream_widget = None
        self._stream_text = ""
        self._stream_dirty = False
        # Drop tool state from any previous turn: a late ToolCallFinished from
        # an errored turn must not touch this turn's transcript.
        self._tool_widgets.clear()
        self._interrupt_marked = False
        self._tool_args.clear()
        if self._flush_timer is not None:
            self._flush_timer.stop()
        self._flush_timer = self.set_interval(0.1, self._flush_stream)
        self._set_status("thinking")

    def apply_event(self, event: AgentEvent) -> None:
        match event:
            case TextDelta():
                self._apply_text_delta(event)
            case ToolCallStarted():
                self._apply_tool_started(event)
            case ToolCallFinished():
                self._apply_tool_finished(event)
            case AgentError():
                self._apply_agent_error(event)
            case TurnComplete():
                self._apply_turn_complete(event)
            case TurnInterrupted():
                self._apply_turn_interrupted(event)

    def _apply_text_delta(self, event: TextDelta) -> None:
        self._append_text(event.text)

    def _apply_tool_started(self, event: ToolCallStarted) -> None:
        self._end_stream()
        marker = _tool_marker(event.name)
        label = _tool_label(event.name, event.arguments, done=False)
        entry = ChatEntry(
            Text.assemble((f"{marker} ", "yellow"), (f"{label}…", "dim")),
            raw=f"{marker} {label}…",
            classes="tool-line",
        )
        self._tool_widgets[event.call_id] = entry
        self._tool_args[event.call_id] = event.arguments
        self._mount_entry(entry)
        self._set_status(label)

    def _apply_tool_finished(self, event: ToolCallFinished) -> None:
        self._finish_tool(event)
        self._set_status("thinking")

    def _apply_agent_error(self, event: AgentError) -> None:
        self._end_stream()
        self._stop_flush_timer()
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

    def _finish_turn_header(
        self,
        input_tokens: int,
        output_tokens: int,
        estimated: bool,
    ) -> None:
        self.set_header(
            self._model,
            self._tok_in + input_tokens,
            self._tok_out + output_tokens,
            self._estimated or estimated,
            tier=self._tier,
        )

    def _apply_turn_complete(self, event: TurnComplete) -> None:
        self._end_stream()
        self._note_citation_problems(event)
        self._stop_flush_timer()
        self._clear_status()
        self.query_one("#agent-input", Input).disabled = False
        self._finish_turn_header(event.input_tokens, event.output_tokens, event.estimated)

    def _apply_turn_interrupted(self, event: TurnInterrupted) -> None:
        # A stop is a normal outcome, not an error: the partial answer
        # stays in the transcript and in-flight tool lines are marked.
        # Interrupt-and-submit already settled the transcript when the
        # correction was echoed — never mount a second marker there.
        self._stop_flush_timer()
        if not self._interrupt_marked:
            self._mark_interrupted()
        self._interrupt_marked = False
        self._clear_status()
        # Focus returns to the input even when the stop key was pressed
        # elsewhere (it is a global binding): the natural next step
        # after stopping a turn is typing the correction.
        inp = self.query_one("#agent-input", Input)
        inp.disabled = False
        inp.focus()
        self._finish_turn_header(event.input_tokens, event.output_tokens, event.estimated)

    # --- internals ----------------------------------------------------------

    def _mount_entry(self, entry: ChatEntry) -> None:
        chat = self.query_one("#agent-chat", VerticalScroll)
        chat.mount(entry)
        chat.call_after_refresh(chat.scroll_end, animate=False)

    def _append_text(self, text: str) -> None:
        """Accumulate stream deltas; visual rendering is flushed on a timer.

        Re-rendering per token would reparse the whole accumulated text on
        every delta (O(n^2) on long answers), so the widget is repainted at
        ~10fps and the final Markdown render happens once in ``_end_stream``.
        """
        self._stream_text += text
        if self._stream_widget is None:
            self._stream_widget = ChatEntry("", raw="", classes="agent-msg")
            self._mount_entry(self._stream_widget)
        self._stream_widget.raw = self._stream_text
        self._stream_dirty = True

    def _flush_stream(self) -> None:
        if not self._stream_dirty or self._stream_widget is None:
            return
        self._stream_dirty = False
        self._stream_widget.set_content(Text(self._stream_text), self._stream_text)
        chat = self.query_one("#agent-chat", VerticalScroll)
        chat.call_after_refresh(chat.scroll_end, animate=False)

    def _note_citation_problems(self, event: TurnComplete) -> None:
        """Mark citations that do not hold up, without touching the answer.

        korvid's own note, appended after the model's text rather than
        edited into it: deleting an unsupported citation would delete the
        evidence that the claim was unsourced (issue #192).
        """
        problems: list[str] = []
        if event.uncited:
            problems.append(
                f"unsupported citation: {', '.join(event.uncited)}"
                " — no such evidence was read this turn"
            )
        if event.duplicated:
            problems.append(
                f"cited more than once: {', '.join(event.duplicated)}"
                " — repetition is not additional support"
            )
        if not problems:
            return
        note = "\n".join(problems)
        self._mount_entry(
            ChatEntry(Text(note, style="yellow"), raw=note, classes="agent-msg citation-note")
        )

    def _end_stream(self) -> None:
        if self._stream_widget is not None and self._stream_text:
            # The message is complete: render it as Markdown exactly once.
            self._stream_widget.set_content(Markdown(self._stream_text), self._stream_text)
        self._stream_widget = None
        self._stream_text = ""
        self._stream_dirty = False

    def _stop_flush_timer(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None

    def _finish_tool(self, event: ToolCallFinished) -> None:
        entry = self._tool_widgets.pop(event.call_id, None)
        if entry is None:
            # Stale finish from a previous turn (state was cleared) — ignore.
            return
        arguments = self._tool_args.pop(event.call_id, "")
        marker = _tool_marker(event.name)
        label = _tool_label(event.name, arguments, done=event.ok)
        if event.ok:
            entry.set_content(
                Text.assemble((f"{marker} ", "green"), (label, "dim")),
                f"{marker} {label}",
            )
        else:
            entry.set_content(
                Text.assemble(
                    (f"{marker} ", "red"), (label, "dim"), (f" — {event.summary}", "red")
                ),
                f"{marker} {label} — {event.summary}",
            )

    def _mark_tools_interrupted(self) -> None:
        """Mark tool lines that never got a finish event as interrupted."""
        for entry in self._tool_widgets.values():
            label = entry.raw.rstrip("…")
            entry.set_content(
                Text.assemble((label, "dim"), (" — interrupted", "dim")),
                f"{label} — interrupted",
            )
        self._tool_widgets.clear()
        self._tool_args.clear()

    # --- status / spinner ---------------------------------------------------

    def _set_status(self, text: str) -> None:
        # The stop hint rides along in the status line so the affordance is
        # discoverable exactly when it applies (issue #170).
        self.status_text = f"{text}… · {self.stop_key} stop"
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
            Text(f"{frame} {self.status_text}", style="dim")
        )
