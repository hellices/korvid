"""Ops hint strip: explains an abnormal pod under the cursor without a describe.

Renders only API data (`ContainerTrouble` captured from container statuses,
plus an optional warning event line) — no synthesized diagnoses.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from rich.text import Text
from textual.widgets import Static

from korvid.k8s.models import ContainerTrouble
from korvid.ui.theme import phase_style

#: Total line budget for the strip (issue #34): everything beyond it folds
#: behind the on-demand detail overlay, indicated by "+N more (i: details)".
_MAX_STRIP_LINES = 2

#: Fragments that repeat what the cursor row already shows (container name,
#: pod name with namespace and uid) - pure noise inside a hint message.
_NOISE_FRAGMENT_RE = re.compile(r"\s*\b(?:container|pod)=\S+")

#: Normalization for reason-vs-event dedupe: case and separators ignored.
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


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


def _clean_message(message: str) -> str:
    """Single-line message with `container=...` / `pod=name_ns(uid)` fragments
    removed - they repeat what the cursor row and the line prefix already
    show. Only removal, never rewording: the strip stays verbatim API data."""
    return _single_line(_NOISE_FRAGMENT_RE.sub("", message))


def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub("", text.lower())


def _event_repeats_trouble(event: str, trouble: tuple[ContainerTrouble, ...]) -> bool:
    """Whether the Warning event restates a status-derived entry: the BackOff
    event and the CrashLoopBackOff status describe the same failure, and two
    near-identical lines bury the signal (issue #34). True when the event's
    reason (the part before ':') matches a trouble reason or is its suffix
    after normalization. Suffix (not substring) matching: a short generic
    reason (`Failed`) appearing inside `FailedScheduling` describes a
    different failure, while `BackOff` ending `CrashLoopBackOff` is the
    same story."""
    reason = _normalize(event.split(":", 1)[0])
    if not reason:
        return False
    return any(_normalize(entry.reason).endswith(reason) for entry in trouble)


def _trouble_line(entry: ContainerTrouble, *, now: datetime | None = None) -> Text:
    """Structured one-line rendering (issue #34), composed from parsed fields:
    `\u25cf demo-app CrashLoopBackOff: back-off 5m0s - exit 137 (OOMKilled), restarts 696`
    - bullet and reason carry the severity color, the container is cyan, and
    counters are dim, so reason/container/message no longer run together."""
    style = phase_style(entry.reason)
    line = Text()
    line.append("\u25cf ", style=style)
    line.append(f"{entry.container} ", style="bold cyan")
    line.append(entry.reason, style=style)
    if entry.message and (cleaned := _clean_message(entry.message)):
        line.append(f": {cleaned}")
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
    """At most `_MAX_STRIP_LINES` concise lines (issue #34). An event
    restating a status entry is dropped; when the entries plus the event
    exceed the budget, everything past the first line folds behind the
    detail overlay, indicated by `+N more (i: details)`."""
    if event and _event_repeats_trouble(event, trouble):
        event = None
    total = len(trouble) + (1 if event else 0)
    if total <= _MAX_STRIP_LINES:
        lines = [_trouble_line(entry, now=now) for entry in trouble]
        if event:
            lines.append(Text(_single_line(event), style="yellow"))
        return lines
    shown = trouble[: _MAX_STRIP_LINES - 1]
    lines = [_trouble_line(entry, now=now) for entry in shown]
    lines.append(Text(f"+{total - len(shown)} more (i: details)", style="dim"))
    return lines


class HintStrip(Static):
    """Thin panel above the status bar (normal flow); hidden while healthy."""

    DEFAULT_CSS = """
    HintStrip {
        height: auto;
        /* border-top consumes one layout row; the content is capped at two
           rows (issue #34) - the rest lives in the detail overlay. */
        max-height: 3;
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
