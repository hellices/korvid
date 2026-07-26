"""Ops hint strip: explains an abnormal pod under the cursor without a describe.

Renders only API data (`ContainerTrouble` captured from container statuses,
plus an optional warning event line) — no synthesized diagnoses.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rich.text import Text
from textual.widgets import Static

from korvid.k8s.models import ContainerTrouble
from korvid.ui.theme import phase_style

#: At most this many per-container detail lines; the rest collapse to "+N more".
_MAX_DETAIL_LINES = 2


def parse_rfc3339(value: str) -> datetime | None:
    """Parse an RFC 3339 timestamp; None when malformed or naive."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def relative_age(value: str, *, now: datetime | None = None) -> str | None:
    """Compact age like `40s` / `45m` / `7h` / `6d`, or None when unparsable."""
    parsed = parse_rfc3339(value)
    if parsed is None:
        return None
    seconds = max(0.0, ((now or datetime.now(UTC)) - parsed).total_seconds())
    if seconds < 120:  # display bucket boundary
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 120:  # display bucket boundary
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 48:  # display bucket boundary
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def _single_line(text: str) -> str:
    """Collapse newlines and runs of whitespace: each hint entry must occupy
    exactly one terminal row or it would push the event/overflow rows out."""
    return " ".join(text.split())


def _trouble_line(entry: ContainerTrouble, *, now: datetime | None = None) -> Text:
    """One-line rendering: `app CrashLoopBackOff: msg - exit 137 (OOMKilled), restarts 12`."""
    line = Text()
    line.append(f"{entry.container} ", style="bold")
    line.append(entry.reason, style=phase_style(entry.reason))
    if entry.message:
        line.append(f": {_single_line(entry.message)}")
    tail: list[str] = []
    if entry.exit_code is not None:
        exit_part = f"exit {entry.exit_code}"
        if entry.exit_reason:
            exit_part += f" ({entry.exit_reason})"
        tail.append(exit_part)
    if entry.restarts:
        tail.append(f"restarts {entry.restarts}")
    if entry.finished_at:
        age = relative_age(entry.finished_at, now=now)
        tail.append(f"last {age} ago" if age else f"last {entry.finished_at}")
    if tail:
        line.append(f" - {', '.join(tail)}", style="dim")
    return line


def render_trouble_lines(
    trouble: tuple[ContainerTrouble, ...],
    *,
    event: str | None = None,
    now: datetime | None = None,
) -> list[Text]:
    """Compact hint lines for the strip: capped detail lines, then the event."""
    lines = [_trouble_line(entry, now=now) for entry in trouble[:_MAX_DETAIL_LINES]]
    remainder = len(trouble) - _MAX_DETAIL_LINES
    if remainder > 0:
        lines.append(Text(f"+{remainder} more container(s) failing", style="dim"))
    if event:
        lines.append(Text(_single_line(event), style="yellow"))
    return lines


class HintStrip(Static):
    """Thin panel above the status bar (normal flow); hidden while healthy."""

    DEFAULT_CSS = """
    HintStrip {
        height: auto;
        /* border-top consumes one layout row; the content can be up to four
           rows (two details, +N more, newest event), hence 1 + 4. */
        max-height: 5;
        padding: 0 1;
        background: $surface;
        border-top: solid $warning;
        /* One terminal row per logical entry: a long message must truncate,
           not wrap, or it would push the overflow/event rows out of view. */
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self.display = False

    def show_trouble(
        self,
        trouble: tuple[ContainerTrouble, ...],
        *,
        event: str | None = None,
    ) -> None:
        """Render trouble for the highlighted row; hides itself when empty."""
        lines = render_trouble_lines(trouble, event=event)
        if not lines:
            self.clear_hint()
            return
        self.update(Text("\n").join(lines))
        self.display = True

    def clear_hint(self) -> None:
        self.update("")
        self.display = False
