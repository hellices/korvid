"""Log pane widget — two-pane split with RichLog and source/state header."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static

from korvid.k8s.logs import LogLine


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
    LogPane RichLog {
        height: 1fr;
        background: $surface;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._multi_source: bool = False
        self._sources_text: str = ""
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", id="log-header")
        yield RichLog(wrap=False, highlight=False, markup=False)

    def open(self, sources: list[tuple[str, str]], *, force_prefix: bool = False) -> None:
        """Open the pane with the given ``(pod, container)`` sources.

        ``force_prefix=True`` ensures ``[pod/container]`` prefixes are shown even
        when only one source is present (used by the multi-pod ``L`` path).
        """
        self._multi_source = force_prefix or len(sources) > 1
        self._sources_text = ", ".join(f"{pod}/{ctr}" if ctr else pod for pod, ctr in sources)
        self.query_one("#log-header", Static).update(self._sources_text)
        self.query_one(RichLog).clear()
        self.display = True

    def feed(self, line: LogLine) -> None:
        """Write *line* to the RichLog; prefix omitted for single-source streams."""
        rich_log = self.query_one(RichLog)
        if self._multi_source:
            rich_log.write(f"[{line.pod}/{line.container}] {line.text}")
        else:
            rich_log.write(line.text)

    def set_state(self, state: str) -> None:
        """Update the header to show the current streaming state indicator."""
        _indicators: dict[str, str] = {
            "streaming": "\u25cf streaming",
            "reconnecting": "\u27f3 reconnecting",
            "ended": "\u25ae ended",
            "error": "\u25ae error",
        }
        indicator = _indicators.get(state, state)
        self.query_one("#log-header", Static).update(f"{self._sources_text} \u2014 {indicator}")

    def show_overflow_banner(self) -> None:
        """Write a one-time overflow warning line to the log."""
        self.query_one(RichLog).write(
            "\u2500\u2500 buffer overflowed; oldest lines dropped \u2500\u2500"
        )

    def close(self) -> None:
        """Hide the pane and clear its contents."""
        self.query_one(RichLog).clear()
        self.display = False
