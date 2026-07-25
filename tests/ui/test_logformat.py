"""Pure-unit tests for logformat.format_log_line — no Textual required."""

from __future__ import annotations

import json

from rich.text import Text

from korvid.ui.logformat import format_log_line

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _styles_at(text: Text, substr: str) -> list[str]:
    """Return style strings for all spans that overlap *substr* in *text*."""
    start = text.plain.index(substr)
    end = start + len(substr)
    return [str(span.style) for span in text.spans if span.start < end and span.end > start]


# ---------------------------------------------------------------------------
# formatted=True — JSON object rendering
# ---------------------------------------------------------------------------


def test_json_object_level_info_green_msg_bold() -> None:
    line = json.dumps({"level": "info", "msg": "hello world"})
    result = format_log_line(line, formatted=True)
    assert "info" in result.plain
    assert "hello world" in result.plain
    assert any("green" in s for s in _styles_at(result, "info"))
    assert any("bold" in s for s in _styles_at(result, "hello world"))


def test_json_level_warn_yellow() -> None:
    line = json.dumps({"level": "warn", "msg": "careful"})
    result = format_log_line(line, formatted=True)
    assert any("yellow" in s for s in _styles_at(result, "warn"))


def test_json_level_warning_yellow() -> None:
    line = json.dumps({"level": "warning", "msg": "careful"})
    result = format_log_line(line, formatted=True)
    assert any("yellow" in s for s in _styles_at(result, "warning"))


def test_json_level_error_red() -> None:
    line = json.dumps({"level": "error", "msg": "boom"})
    result = format_log_line(line, formatted=True)
    assert any("red" in s for s in _styles_at(result, "error"))


def test_json_level_fatal_red() -> None:
    line = json.dumps({"level": "fatal", "msg": "crash"})
    result = format_log_line(line, formatted=True)
    assert any("red" in s for s in _styles_at(result, "fatal"))


def test_json_level_debug_dim() -> None:
    line = json.dumps({"level": "debug", "msg": "trace"})
    result = format_log_line(line, formatted=True)
    assert any("dim" in s for s in _styles_at(result, "debug"))


# ---------------------------------------------------------------------------
# formatted=True — fallback to plain
# ---------------------------------------------------------------------------


def test_invalid_json_returns_plain() -> None:
    result = format_log_line("not json at all", formatted=True)
    assert result.plain == "not json at all"
    assert not result.spans


def test_json_array_returns_plain() -> None:
    raw = "[1, 2, 3]"
    result = format_log_line(raw, formatted=True)
    assert result.plain == raw
    assert not result.spans


def test_json_scalar_returns_plain() -> None:
    raw = '"just a string"'
    result = format_log_line(raw, formatted=True)
    assert result.plain == raw


def test_json_null_returns_plain() -> None:
    result = format_log_line("null", formatted=True)
    assert result.plain == "null"


# ---------------------------------------------------------------------------
# formatted=False — always plain
# ---------------------------------------------------------------------------


def test_formatted_false_returns_plain_for_valid_json() -> None:
    line = json.dumps({"level": "info", "msg": "hello"})
    result = format_log_line(line, formatted=False)
    assert result.plain == line
    assert not result.spans


def test_formatted_false_returns_plain_for_regular_text() -> None:
    result = format_log_line("plain text line", formatted=False)
    assert result.plain == "plain text line"
    assert not result.spans


# ---------------------------------------------------------------------------
# Key ordering: level → ts → msg → rest
# ---------------------------------------------------------------------------


def test_key_order_level_ts_msg_rest() -> None:
    line = json.dumps(
        {
            "message": "hi there",
            "level": "info",
            "ts": "2024-01-01T00:00:00Z",
            "extra": "xval",
        }
    )
    result = format_log_line(line, formatted=True)
    plain = result.plain
    assert plain.index("info") < plain.index("2024-01-01T00:00:00Z")
    assert plain.index("2024-01-01T00:00:00Z") < plain.index("hi there")
    assert plain.index("hi there") < plain.index("xval")


def test_ts_key_variants() -> None:
    for ts_key in ("ts", "time", "timestamp"):
        line = json.dumps({"level": "info", ts_key: "t-value", "msg": "m"})
        result = format_log_line(line, formatted=True)
        assert "t-value" in result.plain, f"ts_key={ts_key!r} not rendered"


def test_msg_key_variants() -> None:
    for msg_key in ("msg", "message"):
        line = json.dumps({"level": "info", msg_key: "m-value"})
        result = format_log_line(line, formatted=True)
        assert "m-value" in result.plain, f"msg_key={msg_key!r} not rendered"


def test_remaining_keys_rendered_as_key_eq_value() -> None:
    line = json.dumps({"level": "info", "msg": "m", "code": "404"})
    result = format_log_line(line, formatted=True)
    assert "code=404" in result.plain


def test_missing_level_no_crash() -> None:
    line = json.dumps({"msg": "hello"})
    result = format_log_line(line, formatted=True)
    assert "hello" in result.plain


def test_missing_msg_no_crash() -> None:
    line = json.dumps({"level": "info"})
    result = format_log_line(line, formatted=True)
    assert "info" in result.plain


def test_returns_text_instance() -> None:
    result = format_log_line("anything", formatted=False)
    assert isinstance(result, Text)


# ---------------------------------------------------------------------------
# formatted=True — plain-text (non-JSON) level detection
# ---------------------------------------------------------------------------


def _span_styles(result: Text) -> list[tuple[str, str]]:
    """Return (substring, style) for each span in the Text."""
    return [(result.plain[s.start : s.end], str(s.style)) for s in result.spans]


def test_plain_error_level_word_red() -> None:
    result = format_log_line("2026-07-25T10:00:00Z ERROR failed to connect", formatted=True)
    assert result.plain == "2026-07-25T10:00:00Z ERROR failed to connect"
    assert ("ERROR", "red") in _span_styles(result)


def test_plain_warn_level_word_yellow() -> None:
    result = format_log_line("WARN disk usage above 80%", formatted=True)
    assert ("WARN", "yellow") in _span_styles(result)


def test_plain_info_level_word_green() -> None:
    result = format_log_line("INFO server started", formatted=True)
    assert ("INFO", "green") in _span_styles(result)


def test_plain_lowercase_level_detected() -> None:
    result = format_log_line("level=error msg=boom", formatted=True)
    assert ("error", "red") in _span_styles(result)


def test_plain_timestamp_dimmed() -> None:
    result = format_log_line("2026-07-25T10:00:00Z INFO ok", formatted=True)
    assert ("2026-07-25T10:00:00Z", "dim") in _span_styles(result)


def test_plain_level_inside_word_not_matched() -> None:
    result = format_log_line("information about the request", formatted=True)
    assert not result.spans


def test_formatted_false_skips_plain_level_detection() -> None:
    result = format_log_line("ERROR failed", formatted=False)
    assert not result.spans
