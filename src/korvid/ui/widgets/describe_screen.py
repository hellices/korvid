"""Describe modal — full-screen YAML + events view for a selected resource."""

from __future__ import annotations

from typing import Any, ClassVar

import yaml
from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Static


def _format_age(event: dict[str, Any]) -> str:
    ts = event.get("lastTimestamp") or event.get("eventTime") or ""
    if not ts:
        return "-"
    # Return just the raw timestamp string; keep it simple.
    return str(ts)


def _render_events(events: list[dict[str, Any]]) -> Text:
    """Render events as a Rich Text; Warning lines are red."""
    if not events:
        return Text("<no events>", style="dim")
    result = Text()
    for i, ev in enumerate(events):
        if i > 0:
            result.append("\n")
        ev_type = str(ev.get("type", ""))
        reason = str(ev.get("reason", ""))
        age = _format_age(ev)
        message = str(ev.get("message", ""))
        line = f"{ev_type:<8} {reason:<20} {age:<30} {message}"
        result.append(line, style="red" if ev_type == "Warning" else "")
    return result


def _strip_managed_fields(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the manifest with metadata.managedFields removed."""
    result = dict(manifest)
    if "metadata" in result and isinstance(result["metadata"], dict):
        meta = dict(result["metadata"])
        meta.pop("managedFields", None)
        result["metadata"] = meta
    return result


def _manifest_yaml(manifest: dict[str, Any]) -> str:
    cleaned = _strip_managed_fields(manifest)
    return yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=True)


def _describe_body(manifest: dict[str, Any], events: list[dict[str, Any]]) -> RenderableType:
    """Render the shared YAML + events body used by both describe views.

    The YAML section is syntax-highlighted; Warning events render red.
    """
    return _body_renderable(_manifest_yaml(manifest), _render_events(events))


def _body_renderable(
    yaml_text: str,
    events_text: Text,
    *,
    yaml_highlights: set[int] | None = None,
) -> RenderableType:
    syntax = Syntax(
        yaml_text,
        "yaml",
        theme="ansi_dark",
        background_color="default",
        word_wrap=True,
        highlight_lines=yaml_highlights,
    )
    header = Text(f"\nEVENTS\n{'─' * 60}", style="bold")
    return Group(syntax, header, events_text)


class BodySearch:
    """Case-insensitive line search over a describe body (YAML + events).

    Mirrors the log pane's inline search semantics: substring match,
    wraparound `n`/`N` navigation, and a "3/17" position counter. Hit
    indices are display-line indices into the rendered body (YAML lines,
    then the 3 separator lines, then event lines).
    """

    #: Lines rendered between the YAML section and the events section
    #: (blank line, "EVENTS" header, horizontal rule).
    _SEPARATOR_LINES = 3

    def __init__(self) -> None:
        self._yaml_lines: list[str] = []
        self._event_lines: list[str] = []
        self.hits: list[int] = []
        self._idx = 0

    def set_body(self, yaml_text: str, events_text: Text) -> None:
        """Load a new body and reset any previous search state."""
        self._yaml_lines = yaml_text.splitlines()
        self._event_lines = events_text.plain.splitlines()
        self.clear()

    def clear(self) -> None:
        """Drop all hits and reset the position."""
        self.hits = []
        self._idx = 0

    def run(self, pattern: str) -> None:
        """Search for *pattern* (case-insensitive substring) in the body."""
        self._idx = 0
        needle = pattern.strip().lower()
        if not needle:
            self.hits = []
            return
        lines = [*self._yaml_lines, "", "EVENTS", "", *self._event_lines]
        self.hits = [i for i, line in enumerate(lines) if needle in line.lower()]

    def next(self) -> None:
        """Advance to the next hit, wrapping past the last one."""
        if self.hits:
            self._idx = (self._idx + 1) % len(self.hits)

    def prev(self) -> None:
        """Go back to the previous hit, wrapping before the first one."""
        if self.hits:
            self._idx = (self._idx - 1) % len(self.hits)

    @property
    def counter(self) -> str:
        """Position counter like "3/17"; empty when there are no hits."""
        if not self.hits:
            return ""
        return f"{self._idx + 1}/{len(self.hits)}"

    @property
    def current_line(self) -> int | None:
        """Display-line index of the current hit; None without hits."""
        if not self.hits:
            return None
        return self.hits[self._idx]

    def yaml_highlights(self) -> set[int]:
        """1-based YAML line numbers to highlight (hits in the YAML section)."""
        return {h + 1 for h in self.hits if h < len(self._yaml_lines)}


def _body_plain(yaml_text: str, events_text: Text) -> str:
    return f"{yaml_text}\n\nEVENTS\n{'─' * 60}\n{events_text.plain}"


def describe_body_text(manifest: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """Plain-text body (no styling) — consumed by the agent UI bridge."""
    return _body_plain(_manifest_yaml(manifest), _render_events(events))


class DescribeScreen(ModalScreen[None]):
    """Full-screen modal showing the raw manifest YAML and events for a resource."""

    # Empty selector disables auto-focus: without this the hidden search
    # Input grabs focus on push and swallows the / and n/N key bindings.
    AUTO_FOCUS = ""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close_search_or_dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
        Binding("slash", "open_search", "Search", show=True),
        Binding("n", "search_next", "Next hit", show=False),
        Binding("shift+n", "search_prev", "Prev hit", show=False),
        Binding("N", "search_prev", "Prev hit", show=False),
    ]

    DEFAULT_CSS = """
    DescribeScreen {
        layout: vertical;
        background: $background;
    }
    DescribeScreen VerticalScroll {
        height: 1fr;
        padding: 0 1;
    }
    DescribeScreen #describe-body {
        width: 100%;
    }
    DescribeScreen #describe-search {
        height: 1;
    }
    """

    def __init__(
        self,
        title: str,
        manifest: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        super().__init__()
        self._title = title
        self._manifest = manifest
        self._events = events
        self._search = BodySearch()
        self._pattern = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="search…", id="describe-search")
        with VerticalScroll():
            # markup=False: manifest/event text is cluster-controlled and may
            # contain bracketed sequences Rich would misinterpret as styles.
            yield Static(
                _describe_body(self._manifest, self._events), id="describe-body", markup=False
            )
        yield Footer()

    def on_mount(self) -> None:
        self.title = self._title
        self.query_one("#describe-search", Input).display = False
        self._search.set_body(_manifest_yaml(self._manifest), _render_events(self._events))

    # ------------------------------------------------------------------
    # Inline search (mirrors the log pane's / + n/N semantics)
    # ------------------------------------------------------------------

    def action_open_search(self) -> None:
        """Show the inline search Input and give it focus (``/`` key)."""
        search_input = self.query_one("#describe-search", Input)
        search_input.value = ""
        search_input.display = True
        search_input.focus()

    def action_search_next(self) -> None:
        """Advance to the next hit and scroll to it (``n`` key)."""
        self._search.next()
        self._show_current_hit()

    def action_search_prev(self) -> None:
        """Go back to the previous hit and scroll to it (``N`` key)."""
        self._search.prev()
        self._show_current_hit()

    def action_close_search_or_dismiss(self) -> None:
        """Close the search input if open, otherwise dismiss the screen."""
        search_input = self.query_one("#describe-search", Input)
        if search_input.display or self._search.hits or self._pattern:
            search_input.display = False
            self._pattern = ""
            self._search.clear()
            self._refresh_body()
            self.sub_title = ""
            self.set_focus(None)
            return
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run the search when the user submits a pattern."""
        event.stop()
        search_input = self.query_one("#describe-search", Input)
        search_input.display = False
        self.set_focus(None)
        self._pattern = event.value.strip()
        self._search.run(self._pattern)
        self._refresh_body()
        self._show_current_hit()

    def _show_current_hit(self) -> None:
        self.sub_title = self._search.counter
        line = self._search.current_line
        if line is not None:
            self.query_one(VerticalScroll).scroll_to(y=line, animate=False)

    def _refresh_body(self) -> None:
        yaml_text = _manifest_yaml(self._manifest)
        events_text = _render_events(self._events)
        if self._pattern and self._search.hits:
            events_text.highlight_words([self._pattern], "reverse", case_sensitive=False)
        self.query_one("#describe-body", Static).update(
            _body_renderable(yaml_text, events_text, yaml_highlights=self._search.yaml_highlights())
        )


class DescribePane(Vertical):
    """Non-modal describe view mounted on the app screen (agent-shared).

    ``DescribeScreen`` is a modal: pushing it makes it the active screen, so
    the chat input underneath cannot take focus. When the agent opens a
    describe while the chat panel is visible, this pane is shown instead —
    it takes the left side of the main screen and the conversation (and its
    input) stay fully interactive. Escape closes it (see App.on_key).
    """

    DEFAULT_CSS = """
    DescribePane {
        dock: left;
        width: 60%;
        background: $background;
        border-right: solid $accent;
    }
    DescribePane #describe-pane-title {
        height: 1;
        background: $surface;
        padding: 0 1;
        text-style: bold;
    }
    DescribePane #describe-pane-search {
        height: 1;
    }
    DescribePane VerticalScroll {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.display = False
        self.body_text = ""
        self._base_title = ""
        self._search = BodySearch()
        self._pattern = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="describe-pane-title")
        yield Input(placeholder="search…", id="describe-pane-search")
        with VerticalScroll():
            yield Static("", id="describe-pane-body", markup=False)

    def on_mount(self) -> None:
        self.query_one("#describe-pane-search", Input).display = False

    def show(self, title: str, manifest: dict[str, Any], events: list[dict[str, Any]]) -> None:
        yaml_text = _manifest_yaml(manifest)
        events_text = _render_events(events)
        self.body_text = _body_plain(yaml_text, events_text)
        self._base_title = title
        self._pattern = ""
        self._search.set_body(yaml_text, events_text)
        self.query_one("#describe-pane-search", Input).display = False
        self._update_title()
        self.query_one("#describe-pane-body", Static).update(
            _body_renderable(yaml_text, events_text)
        )
        self.query_one(VerticalScroll).scroll_home(animate=False)
        self.display = True

    def hide(self) -> None:
        self.display = False

    # ------------------------------------------------------------------
    # Inline search (same / + n/N semantics as DescribeScreen; the App
    # routes slash/n/N here while the pane is displayed)
    # ------------------------------------------------------------------

    def open_search(self) -> None:
        """Show the inline search Input and give it focus."""
        search_input = self.query_one("#describe-pane-search", Input)
        search_input.value = ""
        search_input.display = True
        search_input.focus()

    def search_next(self) -> None:
        """Advance to the next hit and scroll to it."""
        self._search.next()
        self._show_current_hit()

    def search_prev(self) -> None:
        """Go back to the previous hit and scroll to it."""
        self._search.prev()
        self._show_current_hit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run the search when the user submits a pattern."""
        event.stop()
        search_input = self.query_one("#describe-pane-search", Input)
        search_input.display = False
        self.screen.set_focus(None)
        self._pattern = event.value.strip()
        self._search.run(self._pattern)
        self._show_current_hit()

    def on_key(self, event: Key) -> None:
        """Dismiss the search Input on Escape (before the App closes the pane)."""
        search_input = self.query_one("#describe-pane-search", Input)
        if event.key == "escape" and search_input.display:
            search_input.display = False
            self._pattern = ""
            self._search.clear()
            self._update_title()
            self.screen.set_focus(None)
            event.stop()

    def _show_current_hit(self) -> None:
        self._update_title()
        line = self._search.current_line
        if line is not None:
            self.query_one(VerticalScroll).scroll_to(y=line, animate=False)

    def _update_title(self) -> None:
        counter = self._search.counter
        suffix = f"  {counter}" if counter else ""
        self.query_one("#describe-pane-title", Static).update(
            f"{self._base_title}{suffix}  (Esc to close)"
        )
