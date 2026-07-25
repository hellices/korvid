"""Pure log-line formatter: plain text or structured JSON rendering.

No Textual imports — this module may be used outside the UI layer.
"""

from __future__ import annotations

import json
import re

from rich.text import Text

_LEVEL_KEY = "level"
_TS_KEYS = frozenset({"ts", "time", "timestamp"})
_MSG_KEYS = frozenset({"msg", "message"})

# Standalone level words in plain-text (non-JSON) log lines.
_PLAIN_LEVEL_RE = re.compile(
    r"\b(trace|debug|info|warn(?:ing)?|error|fatal|panic|critical)\b", re.IGNORECASE
)
# ISO-8601-ish timestamps: 2026-07-25T10:00:00Z, with optional fraction/offset.
_PLAIN_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def _level_style(level_str: str) -> str:
    """Return the Rich style for a log level string (case-insensitive)."""
    lower = level_str.lower()
    if lower in ("error", "fatal", "panic", "critical"):
        return "red"
    if lower in ("warn", "warning"):
        return "yellow"
    if lower == "info":
        return "green"
    return "dim"


def _first_key(keys: frozenset[str], data: dict[str, object]) -> str | None:
    """Return the first key from *keys* found in *data*, or ``None``."""
    return next((k for k in keys if k in data), None)


def _build_structured_text(data: dict[str, object]) -> Text:
    """Build a structured Rich Text from a parsed JSON log dict."""
    parts: list[tuple[str, str]] = []  # (display_string, rich_style)

    level_val = data.get(_LEVEL_KEY)
    if level_val is not None:
        parts.append((str(level_val), _level_style(str(level_val))))

    ts_key = _first_key(_TS_KEYS, data)
    if ts_key is not None:
        parts.append((str(data[ts_key]), "dim"))

    msg_key = _first_key(_MSG_KEYS, data)
    if msg_key is not None:
        parts.append((str(data[msg_key]), "bold"))

    consumed = {_LEVEL_KEY}
    if ts_key:
        consumed.add(ts_key)
    if msg_key:
        consumed.add(msg_key)

    for key, val in data.items():
        if key not in consumed:
            parts.append((f"{key}={val}", "dim"))

    return _parts_to_text(parts)


def _parts_to_text(parts: list[tuple[str, str]]) -> Text:
    """Join (display, style) pairs into a single Rich Text, space-separated."""
    result = Text()
    for i, (display, style) in enumerate(parts):
        if i > 0:
            result.append(" ")
        result.append(display, style=style)
    return result


def _build_plain_text(text: str) -> Text:
    """Style a non-JSON log line: level words colored, ISO timestamps dim."""
    result = Text(text)
    for match in _PLAIN_TS_RE.finditer(text):
        result.stylize("dim", match.start(), match.end())
    for match in _PLAIN_LEVEL_RE.finditer(text):
        result.stylize(_level_style(match.group(1)), match.start(1), match.end(1))
    return result


def format_log_line(text: str, *, formatted: bool) -> Text:
    """Render a log line as structured or plain text.

    When ``formatted`` is ``True`` and ``text`` parses as a JSON object, the
    line is rendered with:

    - ``level`` value coloured (error/fatal → red, warn/warning → yellow,
      info → green, else dim)
    - ``ts`` / ``time`` / ``timestamp`` value dim
    - ``msg`` / ``message`` value bold
    - remaining ``key=value`` pairs dim
    - order: level, ts, msg, then the rest in original dict order

    Non-JSON input with ``formatted=True`` gets lightweight plain-text
    styling: standalone level words colored, ISO timestamps dim. Valid
    JSON that is not an object (arrays, scalars) and ``formatted=False``
    always return an unstyled ``Text(text)``.
    """
    if not formatted:
        return Text(text)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _build_plain_text(text)
    if not isinstance(data, dict):
        return Text(text)
    return _build_structured_text(data)
