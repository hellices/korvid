"""Log pane widget — two-pane split with RichLog and source/state header.

New in Task 10
--------------
- ``formatted`` toggle (``f``): renders JSON lines with colour; plain otherwise.
- ``p`` key: re-open with ``previous=True`` (handled by the App, which calls
  ``open()`` with fresh triples and ``previous=True``).
- Inline search (``/``): ``open_search()`` shows a one-line Input; on submit
  hits come from ``LogBuffer.search()``.  ``search_next()`` / ``search_prev()``
  scroll the RichLog to the next/previous hit.

Design notes
~~~~~~~~~~~~
**f-rerender approach**: The App owns the LogBuffer and calls
``log_pane.replay(buffer.lines())`` after toggling ``log_pane.formatted``.
``replay()`` clears the RichLog and re-feeds every line through
``_write_line()``, which in turn calls ``format_log_line``.  This keeps the
formatting logic in one place and the App in charge of the source of truth.

**/-routing**: ``App.action_open_filter`` checks ``log_pane.display`` first.
When the pane is open it calls ``log_pane.open_search()`` instead of opening
the table FilterBar.  The ``n``/``N`` (shift+n) keys are App-level bindings
guarded by ``log_pane.display``, so they are inert when the pane is closed.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.events import Key
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

from korvid.core.logbuffer import LogBuffer
from korvid.k8s.logs import LogLine
from korvid.ui.logformat import format_log_line


class LogPane(Widget):
    """Collapsible log pane displayed below the resource table.

    Hidden by default; opened by the App's ``l`` / ``L`` bindings.
    """

    DEFAULT_CSS = """
    LogPane {
        height: 40%;
        border-top: solid $primary;
    }
    LogPane #log-header {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    LogPane #log-search {
        height: 1;
        display: none;
    }
    LogPane RichLog {
        height: 1fr;
        background: $surface;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._multi_source: bool = False
        self._sources_text: str = ""
        self._state: str = ""
        self._search_counter: str = ""
        self._search_hits: list[int] = []
        self._search_idx: int = 0
        self._log_buffer: LogBuffer | None = None
        self.formatted: bool = True
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", id="log-header")
        yield Input(placeholder="search logs…", id="log-search")
        yield RichLog(wrap=False, highlight=False, markup=False)

    def on_mount(self) -> None:
        self.query_one("#log-search", Input).display = False

    # ------------------------------------------------------------------
    # Public API (existing + extended)
    # ------------------------------------------------------------------

    def open(
        self,
        sources: list[tuple[str, str]],
        *,
        force_prefix: bool = False,
        log_buffer: LogBuffer | None = None,
    ) -> None:
        """Open the pane with the given ``(pod, container)`` sources.

        ``force_prefix=True`` ensures ``[pod/container]`` prefixes are shown
        even when only one source is present (used by the multi-pod ``L`` path).

        ``log_buffer`` is stored for later inline search; pass the App's current
        ``LogBuffer`` instance.
        """
        self._multi_source = force_prefix or len(sources) > 1
        self._sources_text = ", ".join(f"{pod}/{ctr}" if ctr else pod for pod, ctr in sources)
        self._state = ""
        self._search_counter = ""
        self._search_hits = []
        self._search_idx = 0
        self._log_buffer = log_buffer
        # Hide the search input in case it was open from a previous session.
        self.query_one("#log-search", Input).display = False
        rich_log = self.query_one(RichLog)
        rich_log.clear()
        # Bound RichLog to the buffer capacity (+ headroom for banner lines) so
        # a long-running stream can't grow display memory unboundedly.
        rich_log.max_lines = log_buffer.max_lines + 8 if log_buffer is not None else None
        self._update_header()
        self.display = True

    def feed(self, line: LogLine) -> None:
        """Write *line* to the RichLog; prefix omitted for single-source streams."""
        self._write_line(line)

    def replay(self, lines: list[LogLine]) -> None:
        """Re-render all *lines* using the current ``formatted`` setting.

        Called by the App after toggling ``formatted`` so the whole visible
        buffer is re-displayed without re-buffering.  The App provides the
        lines from its ``LogBuffer``; this method only touches the RichLog.
        """
        rich_log = self.query_one(RichLog)
        rich_log.clear()
        for line in lines:
            self._write_line(line)

    def set_state(self, state: str) -> None:
        """Update the header to show the current streaming state indicator."""
        _indicators: dict[str, str] = {
            "streaming": "\u25cf streaming",
            "reconnecting": "\u27f3 reconnecting",
            "ended": "\u25ae ended",
            "error": "\u25ae error",
        }
        self._state = _indicators.get(state, state)
        self._update_header()

    def show_overflow_banner(self) -> None:
        """Write a one-time overflow warning line to the log."""
        self.query_one(RichLog).write(
            "\u2500\u2500 buffer overflowed; oldest lines dropped \u2500\u2500"
        )

    def write_banner(self, text: str) -> None:
        """Write a plain informational banner line to the log."""
        self.query_one(RichLog).write(text)

    def close(self) -> None:
        """Hide the pane and clear its contents."""
        self._state = ""
        self._search_counter = ""
        self._search_hits = []
        self._search_idx = 0
        self._log_buffer = None
        self.query_one("#log-search", Input).display = False
        self.query_one(RichLog).clear()
        self.display = False

    def toggle_format(self) -> None:
        """Toggle between JSON-formatted and raw display; refresh header tag."""
        self.formatted = not self.formatted
        self._update_header()

    # ------------------------------------------------------------------
    # Inline search
    # ------------------------------------------------------------------

    def open_search(self) -> None:
        """Show the inline search Input and give it focus."""
        search_input = self.query_one("#log-search", Input)
        search_input.value = ""
        search_input.display = True
        search_input.focus()

    def search_next(self) -> None:
        """Advance to the next search hit and scroll to it."""
        if not self._search_hits:
            return
        self._search_idx = (self._search_idx + 1) % len(self._search_hits)
        self._update_search_counter()
        self._scroll_to_hit()

    def search_prev(self) -> None:
        """Go back to the previous search hit and scroll to it."""
        if not self._search_hits:
            return
        self._search_idx = (self._search_idx - 1) % len(self._search_hits)
        self._update_search_counter()
        self._scroll_to_hit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_line(self, line: LogLine) -> None:
        """Write a single line to RichLog, applying prefix and formatting."""
        rich_log = self.query_one(RichLog)
        formatted_text = format_log_line(line.text, formatted=self.formatted)
        if self._multi_source:
            prefix = Text(f"[{line.pod}/{line.container}] ")
            output: Text = prefix + formatted_text
            rich_log.write(output)
        else:
            rich_log.write(formatted_text)

    def _update_header(self) -> None:
        fmt_tag = "[json]" if self.formatted else "[raw]"
        header = f"{self._sources_text} {fmt_tag}"
        if self._state:
            header = f"{header} \u2014 {self._state}"
        if self._search_counter:
            header = f"{header} {self._search_counter}"
        # Use Text to avoid Rich markup parsing the [json]/[raw] literal tags.
        self.query_one("#log-header", Static).update(Text(header))

    def _update_search_counter(self) -> None:
        if self._search_hits:
            self._search_counter = f"{self._search_idx + 1}/{len(self._search_hits)}"
        else:
            self._search_counter = ""
        self._update_header()

    def _scroll_to_hit(self) -> None:
        if not self._search_hits:
            return
        rich_log = self.query_one(RichLog)
        line_idx = self._search_hits[self._search_idx]
        # Hits index into the LogBuffer, but RichLog also contains banner
        # lines and keeps lines the ring buffer has dropped; correct by the
        # difference so the scroll lands on the actual hit.
        if self._log_buffer is not None:
            offset = len(rich_log.lines) - len(self._log_buffer.lines())
            line_idx += max(offset, 0)
        rich_log.scroll_to(y=line_idx, animate=False)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run a search when the user submits a pattern in the search Input."""
        event.stop()
        pattern = event.value.strip()
        self.query_one("#log-search", Input).display = False
        if not pattern or self._log_buffer is None:
            return
        self._search_hits = self._log_buffer.search(pattern)
        self._search_idx = 0
        self._update_search_counter()
        self._scroll_to_hit()

    def on_key(self, event: Key) -> None:
        """Dismiss the search Input on Escape (prevents the App from closing the pane)."""
        search_input = self.query_one("#log-search", Input)
        if event.key == "escape" and search_input.display:
            search_input.display = False
            self._search_hits = []
            self._search_idx = 0
            self._search_counter = ""
            self._update_header()
            event.stop()
