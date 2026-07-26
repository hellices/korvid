"""Tests for core/logexport.py — saving log buffers to disk (issue #43)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from korvid.core.logexport import default_log_export_dir, export_log_lines
from korvid.k8s.logs import LogLine


def _line(
    text: str,
    pod: str = "myapp",
    container: str = "main",
    ts: datetime | None = None,
) -> LogLine:
    return LogLine(pod=pod, container=container, text=text, timestamp=ts)


def test_export_writes_lines_and_returns_path(tmp_path: Path) -> None:
    lines = [_line("hello"), _line("world")]
    now = datetime(2026, 7, 26, 10, 30, 45, tzinfo=UTC)

    path = export_log_lines(lines, tmp_path, now=now)

    assert path.parent == tmp_path
    assert path.name == "korvid-myapp-20260726-103045.log"
    assert path.read_text() == "hello\nworld\n"


def test_export_multi_source_prefixes_pod_container(tmp_path: Path) -> None:
    lines = [
        _line("a", pod="web", container="nginx"),
        _line("b", pod="api", container="app"),
    ]
    now = datetime(2026, 7, 26, 10, 30, 45, tzinfo=UTC)

    path = export_log_lines(lines, tmp_path, now=now)

    assert path.name == "korvid-logs-20260726-103045.log"
    assert path.read_text() == "web/nginx a\napi/app b\n"


def test_export_includes_timestamp_when_present(tmp_path: Path) -> None:
    ts = datetime(2026, 7, 26, 9, 0, 1, tzinfo=UTC)
    lines = [_line("stamped", ts=ts), _line("bare")]
    now = datetime(2026, 7, 26, 10, 30, 45, tzinfo=UTC)

    path = export_log_lines(lines, tmp_path, now=now)

    content = path.read_text()
    assert "2026-07-26T09:00:01+00:00 stamped\n" in content
    assert "\nbare\n" in content


def test_export_creates_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir"
    now = datetime(2026, 7, 26, 10, 30, 45, tzinfo=UTC)

    path = export_log_lines([_line("x")], target, now=now)

    assert path.is_file()
    assert path.parent == target


def test_export_empty_lines_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no log lines"):
        export_log_lines([], tmp_path)


def test_export_sanitizes_pod_name(tmp_path: Path) -> None:
    """Pod names are DNS labels, but never trust interpolated file names."""
    lines = [_line("x", pod="../evil")]
    now = datetime(2026, 7, 26, 10, 30, 45, tzinfo=UTC)

    path = export_log_lines(lines, tmp_path, now=now)

    assert path.parent == tmp_path
    assert "/" not in path.name
    assert ".." not in path.name


def test_default_export_dir_honors_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_log_export_dir() == tmp_path / "xdg" / "korvid" / "logs"


def test_default_export_dir_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = Path.home() / ".local" / "share" / "korvid" / "logs"
    assert default_log_export_dir() == expected
