"""Hint detail overlay (issue #34) — the full story behind a hint strip line.

The strip is capped at two concise lines; this read-only modal shows what
folded away: every troubled container with its verbatim message and
termination details, plus the pod's recent Warning events with relative
ages. Everything rendered is API data — no synthesized diagnoses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from korvid.ui.theme import phase_style
from korvid.ui.widgets.hint_strip import relative_age


def _event_count(event: dict[str, Any]) -> int | None:
    """Occurrences of a (possibly repeating) event: `series.count` for
    events.k8s.io series, `count` for core v1; None when unrecorded."""
    series = event.get("series") or {}
    raw = series.get("count") or event.get("count")
    try:
        count = int(raw)  # type: ignore[arg-type]  # tolerate str/int/None uniformly
    except (TypeError, ValueError):
        return None
    return count if count > 1 else None


def _event_age(event: dict[str, Any], *, now: datetime | None) -> str | None:
    series = event.get("series") or {}
    raw = (
        series.get("lastObservedTime")
        or event.get("lastTimestamp")
        or event.get("eventTime")
        or event.get("firstTimestamp")
        or (event.get("metadata") or {}).get("creationTimestamp")
        or ""
    )
    return relative_age(str(raw), now=now)


def _append_trouble_block(body: Text, entry: Any, *, now: datetime | None) -> None:
    style = phase_style(entry.reason)
    body.append("\u25cf ", style=style)
    body.append(f"{entry.container} ", style="bold cyan")
    body.append(entry.reason, style=style)
    body.append("\n")
    if entry.message:
        body.append(f"  {' '.join(entry.message.split())}\n")
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
        body.append(f"  {', '.join(tail)}\n", style="dim")


def render_hint_detail(
    trouble: tuple[Any, ...],
    events: list[dict[str, Any]],
    *,
    events_unavailable: bool = False,
    now: datetime | None = None,
) -> Text:
    """Full, uncapped detail body: every trouble entry verbatim, then the
    pod's Warning events (reason, occurrence count, relative age, message).
    `events_unavailable` marks a failed fetch - "unavailable" and "known to
    be absent" must not read the same."""
    body = Text()
    for entry in trouble:
        _append_trouble_block(body, entry, now=now)
    if trouble:
        body.append("\n")
    body.append("WARNING EVENTS\n", style="bold")
    if events_unavailable:
        body.append("<warning events unavailable - fetch failed>", style="yellow")
        return body
    warnings = [e for e in events if e.get("type") == "Warning"]
    if not warnings:
        body.append("<no warning events>", style="dim")
        return body
    for event in warnings:
        reason = str(event.get("reason") or "Warning")
        body.append(f"\u25cf {reason}", style="yellow")
        if (count := _event_count(event)) is not None:
            body.append(f" \u00d7{count}", style="dim")
        if (age := _event_age(event, now=now)) is not None:
            body.append(f" {age} ago", style="dim")
        if message := str(event.get("message") or "").strip():
            body.append(f"\n  {' '.join(message.split())}")
        body.append("\n")
    return body


class HintDetailScreen(ModalScreen[None]):
    """Read-only modal behind the strip's `(i: details)` fold indicator."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
        Binding("i", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    HintDetailScreen {
        align: center middle;
    }
    HintDetailScreen #hint-detail-frame {
        width: 80%;
        max-width: 100;
        max-height: 80%;
        height: auto;
        border: round $warning;
        background: $surface;
        padding: 0 1;
    }
    HintDetailScreen #hint-detail-title {
        height: 1;
        text-style: bold;
        color: $warning;
    }
    """

    def __init__(
        self,
        title: str,
        trouble: tuple[Any, ...],
        events: list[dict[str, Any]],
        *,
        events_unavailable: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._trouble = trouble
        self._events = events
        self._events_unavailable = events_unavailable

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="hint-detail-frame"):
            yield Static(f"{self._title}  (Esc to close)", id="hint-detail-title")
            # markup=False: trouble/event text is cluster-controlled and may
            # contain bracketed sequences Rich would misinterpret as styles.
            yield Static(
                render_hint_detail(
                    self._trouble, self._events, events_unavailable=self._events_unavailable
                ),
                id="hint-detail-body",
                markup=False,
            )
