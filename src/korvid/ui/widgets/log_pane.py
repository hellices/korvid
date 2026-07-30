"""Log pane widget — split panels per (pod, container) source.

Multi-source streams (multi-container ``l`` or multi-pod ``L``) render one
panel per source in a 2-column grid, each with a coloured ``pod/container``
title. This lets you compare several
containers side-by-side instead of untangling one merged stream.

Design notes
~~~~~~~~~~~~
**Fixed panel pool**: ``MAX_PANELS`` panels are composed up-front and shown /
hidden per ``open()`` call. This avoids async mount timing issues and keeps
``feed()`` a plain synchronous routing call.

**f-rerender approach**: The App owns the LogBuffer and calls
``log_pane.replay(buffer.lines())`` after toggling ``log_pane.formatted``.
``replay()`` clears every visible panel and re-feeds each line through the
same routing as ``feed()``.

**/-routing**: ``App.action_open_filter`` checks ``log_pane.display`` first.
When the pane is open it calls ``log_pane.open_search()`` instead of opening
the table FilterBar. Search hits index into the shared LogBuffer; the pane
maps each hit to its source panel and scrolls that panel.
"""

from __future__ import annotations

from rich.measure import measure_renderables
from rich.segment import Segment
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.events import Key
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

from korvid.core.logbuffer import LogBuffer
from korvid.k8s.logs import LogLine
from korvid.ui.logformat import format_log_line

#: Maximum simultaneous log sources (matches the App's multi-stream cap).
MAX_PANELS = 8

# Distinct colours cycled across sources so each panel title is attributable
# at a glance.
_SOURCE_COLORS = (
    "bright_cyan",
    "bright_magenta",
    "bright_yellow",
    "bright_green",
    "bright_blue",
    "orange1",
    "turquoise2",
    "violet",
)


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
    LogPane #log-panels {
        layout: grid;
        grid-size: 2;
        height: 1fr;
    }
    LogPane .log-panel {
        height: 1fr;
    }
    LogPane .panel-title {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    LogPane RichLog {
        height: 1fr;
        background: $surface;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._panel_keys: list[str] = []
        self._show_titles: bool = False
        self._sources_text: str = ""
        self._state: str = ""
        self._search_counter: str = ""
        self._search_hits: list[int] = []
        self._search_idx: int = 0
        self._log_buffer: LogBuffer | None = None
        self._banners: list[str] = []
        self.formatted: bool = True
        self.wrap_lines: bool = False
        self.show_timestamps: bool = False
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", id="log-header")
        yield Input(placeholder="search logs…", id="log-search")
        with Container(id="log-panels"):
            for i in range(MAX_PANELS):
                with Vertical(classes="log-panel", id=f"log-panel-{i}"):
                    yield Static("", classes="panel-title", markup=False)
                    yield RichLog(wrap=False, highlight=False, markup=False)

    def on_mount(self) -> None:
        self.query_one("#log-search", Input).display = False
        for i in range(MAX_PANELS):
            self._panel(i).display = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open(
        self,
        sources: list[tuple[str, str]],
        *,
        force_prefix: bool = False,
        log_buffer: LogBuffer | None = None,
    ) -> None:
        """Open the pane with one panel per ``(pod, container)`` source.

        ``force_prefix=True`` shows the panel title even for a single source
        (used by the multi-pod ``L`` path so the pod is always identified).

        ``log_buffer`` is stored for later inline search; pass the App's
        current ``LogBuffer`` instance.
        """
        sources = sources[:MAX_PANELS]
        self._panel_keys = [f"{pod}/{ctr}" for pod, ctr in sources]
        self._show_titles = force_prefix or len(sources) > 1
        self._sources_text = ", ".join(f"{pod}/{ctr}" if ctr else pod for pod, ctr in sources)
        self._state = ""
        self._search_counter = ""
        self._search_hits = []
        self._search_idx = 0
        self._log_buffer = log_buffer
        self._banners = []
        # Hide the search input in case it was open from a previous session.
        self.query_one("#log-search", Input).display = False

        panels_container = self.query_one("#log-panels", Container)
        panels_container.styles.grid_size_columns = 1 if len(sources) == 1 else 2

        max_lines = log_buffer.max_lines + 8 if log_buffer is not None else None
        for i in range(MAX_PANELS):
            panel = self._panel(i)
            if i < len(sources):
                pod, ctr = sources[i]
                title = panel.query_one(".panel-title", Static)
                key = self._panel_keys[i]
                title.update(Text(key if ctr else pod, style=self._color(i)))
                title.display = self._show_titles
                rich_log = panel.query_one(RichLog)
                rich_log.clear()
                # Bound RichLog to the buffer capacity (+ headroom for banner
                # lines) so a long stream can't grow display memory unboundedly.
                rich_log.max_lines = max_lines
                # Session-scoped setting: reopening keeps the last wrap choice.
                rich_log.wrap = self.wrap_lines
                panel.display = True
            else:
                panel.display = False

        self._update_header()
        self.display = True

    def feed(self, line: LogLine) -> None:
        """Route *line* to its source panel's RichLog."""
        self._write_line(line)

    def replay(self, lines: list[LogLine]) -> None:
        """Re-render all *lines* using the current display settings.

        Called by the App after toggling ``formatted`` / ``wrap_lines`` /
        ``show_timestamps`` so the whole visible buffer is re-displayed
        without re-buffering.  Contextual banners (previous-logs, overflow)
        are re-written at the top of each panel: their exact original
        position is not recoverable from the buffer, and for both banner
        kinds the top is where the information belongs after a replay.
        """
        for i in range(len(self._panel_keys)):
            self._panel(i).query_one(RichLog).clear()
        for banner in self._banners:
            self._write_banner_text(banner)
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
        """Write a one-time overflow warning line to every visible panel."""
        self.write_banner("\u2500\u2500 buffer overflowed; oldest lines dropped \u2500\u2500")

    def write_banner(self, text: str) -> None:
        """Write an informational banner line to every visible panel.

        Banners are remembered so ``replay()`` can restore them after the
        panels are cleared by a display toggle.
        """
        self._banners.append(text)
        self._write_banner_text(text)

    def _write_banner_text(self, text: str) -> None:
        for i in range(len(self._panel_keys)):
            self._panel(i).query_one(RichLog).write(text)

    def close(self) -> None:
        """Hide the pane and clear its contents."""
        self._state = ""
        self._search_counter = ""
        self._search_hits = []
        self._search_idx = 0
        self._log_buffer = None
        self._banners = []
        self.query_one("#log-search", Input).display = False
        for i in range(MAX_PANELS):
            panel = self._panel(i)
            panel.query_one(RichLog).clear()
            panel.display = False
        self._panel_keys = []
        self.display = False

    def toggle_format(self) -> None:
        """Toggle between JSON-formatted and raw display; refresh header tag."""
        self.formatted = not self.formatted
        self._update_header()

    def toggle_wrap(self) -> None:
        """Toggle line wrapping on every panel; refresh the header tag.

        RichLog applies ``wrap`` at write time, so the caller must ``replay()``
        the buffer afterwards to re-render existing lines.
        """
        self.wrap_lines = not self.wrap_lines
        for i in range(MAX_PANELS):
            self._panel(i).query_one(RichLog).wrap = self.wrap_lines
        self._update_header()

    def toggle_timestamps(self) -> None:
        """Toggle the kubelet-timestamp prefix; refresh the header tag.

        The caller must ``replay()`` the buffer afterwards so existing lines
        are re-rendered with (or without) the prefix.
        """
        self.show_timestamps = not self.show_timestamps
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

    def _panel(self, index: int) -> Vertical:
        return self.query_one(f"#log-panel-{index}", Vertical)

    @staticmethod
    def _color(index: int) -> str:
        return _SOURCE_COLORS[index % len(_SOURCE_COLORS)]

    def _panel_index(self, key: str) -> int:
        """Panel index for a source key; -1 for unknown sources.

        Unknown keys are dropped rather than routed to panel 0: after a
        ``_toggle_log_pod`` reopen, a straggler line from a just-cancelled
        stream must not be misattributed to another source's panel.
        """
        try:
            return self._panel_keys.index(key)
        except ValueError:
            return -1

    def _write_line(self, line: LogLine) -> None:
        """Route a single line to its panel, applying JSON formatting."""
        if not self._panel_keys:
            return
        index = self._panel_index(f"{line.pod}/{line.container}")
        if index < 0:
            return
        self._panel(index).query_one(RichLog).write(self._render_line_text(line))

    def _render_line_text(self, line: LogLine) -> Text:
        """Render *line* with the current format / timestamp settings."""
        rendered = format_log_line(line.text, formatted=self.formatted)
        if self.show_timestamps and line.timestamp is not None:
            # Kubelet timestamps are UTC; show the user's local wall clock.
            # Style only the prefix span — a base style would dim the body too.
            prefixed = Text()
            prefixed.append(line.timestamp.astimezone().strftime("%H:%M:%S") + " ", style="dim")
            prefixed.append_text(rendered)
            return prefixed
        return rendered

    def _display_rows(self, rich_log: RichLog, renderable: Text) -> int:
        """Display rows *renderable* occupies in *rich_log* (mirrors ``RichLog.write``).

        With wrap off RichLog renders each single-line ``Text`` as exactly one
        row; with wrap on it re-renders at the same width ``write()`` used, so
        one logical line can span several rows.
        """
        if not rich_log.wrap:
            return 1
        console = self.app.console
        options = console.options
        width = measure_renderables(console, options, [renderable]).maximum
        render_width = max(min(width, rich_log.scrollable_content_region.width), rich_log.min_width)
        segments = console.render(renderable, options.update_width(render_width))
        return max(len(list(Segment.split_lines(segments))), 1)

    def _update_header(self) -> None:
        fmt_tag = "[json]" if self.formatted else "[raw]"
        header = f"{self._sources_text} {fmt_tag}"
        if self.wrap_lines:
            header = f"{header} [wrap]"
        if self.show_timestamps:
            header = f"{header} [ts]"
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
        if not self._search_hits or self._log_buffer is None:
            return
        lines = self._log_buffer.lines()
        hit_idx = self._search_hits[self._search_idx]
        if hit_idx >= len(lines):
            return
        hit = lines[hit_idx]
        key = f"{hit.pod}/{hit.container}"
        index = self._panel_index(key)
        if index < 0:
            return  # hit belongs to a source no longer shown
        rich_log = self._panel(index).query_one(RichLog)
        # Display row where the hit starts: sum the rendered heights of the
        # earlier buffer lines routed to the same panel (one row each without
        # wrap; possibly several with wrap).
        start_row = 0
        total_rows = 0
        for i, line in enumerate(lines):
            if self._panel_index(f"{line.pod}/{line.container}") != index:
                continue
            rows = self._display_rows(rich_log, self._render_line_text(line))
            if i < hit_idx:
                start_row += rows
            total_rows += rows
        # Rows the RichLog shows beyond the buffered lines (banners) minus
        # rows it evicted from the top (``max_lines`` trimming) — the latter
        # makes the offset negative.
        offset = len(rich_log.lines) - total_rows
        rich_log.scroll_to(y=max(start_row + offset, 0), animate=False)

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
