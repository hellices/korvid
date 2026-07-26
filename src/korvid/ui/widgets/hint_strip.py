"""Ops hint strip: explains an abnormal pod under the cursor without a describe.

Renders only API data (`ContainerTrouble` captured from container statuses,
plus an optional warning event line) — no synthesized diagnoses.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from korvid.k8s.models import ContainerTrouble
from korvid.ui.theme import phase_style

#: At most this many per-container detail lines; the rest collapse to "+N more".
_MAX_DETAIL_LINES = 2


def _trouble_line(entry: ContainerTrouble) -> Text:
    """One-line rendering: `app CrashLoopBackOff: msg - exit 137 (OOMKilled), restarts 12`."""
    line = Text()
    line.append(f"{entry.container} ", style="bold")
    line.append(entry.reason, style=phase_style(entry.reason))
    if entry.message:
        line.append(f": {entry.message}")
    tail: list[str] = []
    if entry.exit_code is not None:
        exit_part = f"exit {entry.exit_code}"
        if entry.exit_reason:
            exit_part += f" ({entry.exit_reason})"
        tail.append(exit_part)
    if entry.restarts:
        tail.append(f"restarts {entry.restarts}")
    if entry.finished_at:
        tail.append(f"last {entry.finished_at}")
    if tail:
        line.append(f" - {', '.join(tail)}", style="dim")
    return line


def render_trouble_lines(
    trouble: tuple[ContainerTrouble, ...],
    *,
    event: str | None = None,
) -> list[Text]:
    """Compact hint lines for the strip: capped detail lines, then the event."""
    lines = [_trouble_line(entry) for entry in trouble[:_MAX_DETAIL_LINES]]
    remainder = len(trouble) - _MAX_DETAIL_LINES
    if remainder > 0:
        lines.append(Text(f"+{remainder} more container(s) failing", style="dim"))
    if event:
        lines.append(Text(event, style="yellow"))
    return lines


class HintStrip(Static):
    """Thin panel docked above the status bar; hidden while the row is healthy."""

    DEFAULT_CSS = """
    HintStrip {
        dock: bottom;
        height: auto;
        max-height: 4;
        padding: 0 1;
        background: $surface;
        border-top: solid $warning;
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
