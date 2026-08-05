"""Tests for core/logexport.py — saving log buffers to disk (issue #43)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from korvid.core.logexport import default_log_export_dir, export_log_lines
from korvid.k8s.logs import LogLine
from tests.platforms import POSIX


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


def test_export_file_is_private(tmp_path: Path) -> None:
    """Exported cluster logs must not be readable by group/other users.

    On POSIX we can verify the effective mode bits. On Windows/NTFS, Python's
    POSIX-mode emulation (stat.st_mode) does not reflect true ACLs; the code
    passes 0o600 to os.open(O_CREAT|O_EXCL) which is the strongest portable
    guarantee available. We verify the file was created and exists.
    """
    path = export_log_lines([_line("secretish")], tmp_path)

    if POSIX:
        assert path.stat().st_mode & 0o077 == 0
    else:
        # Windows: file exists and was created atomically (O_EXCL); POSIX
        # mode bits are not enforced by NTFS — assert creation succeeded.
        assert path.is_file()


def test_export_writes_utf8(tmp_path: Path) -> None:
    """Non-ASCII log text is written as UTF-8 regardless of the locale."""
    path = export_log_lines([_line("한글 로그 ✓")], tmp_path)

    assert path.read_bytes().decode("utf-8") == "한글 로그 ✓\n"


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


def test_export_same_timestamp_does_not_overwrite(tmp_path: Path) -> None:
    """Two saves within the same second must produce two files."""
    now = datetime(2026, 7, 26, 10, 30, 45, tzinfo=UTC)

    first = export_log_lines([_line("first")], tmp_path, now=now)
    second = export_log_lines([_line("second")], tmp_path, now=now)

    assert first != second
    assert first.read_text() == "first\n"
    assert second.read_text() == "second\n"


def test_export_does_not_overwrite_preexisting_first_collision(tmp_path: Path) -> None:
    """The shared private-export helper must preserve log collision semantics."""
    now = datetime(2026, 7, 26, 10, 30, 45, tzinfo=UTC)
    existing = tmp_path / "korvid-myapp-20260726-103045.log"
    existing.write_text("existing\n", encoding="utf-8")

    path = export_log_lines([_line("new")], tmp_path, now=now)

    assert path.name == "korvid-myapp-20260726-103045-1.log"
    assert existing.read_text(encoding="utf-8") == "existing\n"
    assert path.read_text(encoding="utf-8") == "new\n"


def test_export_single_pod_multi_container_uses_pod_stem(tmp_path: Path) -> None:
    """One pod with several containers keeps the pod-name filename."""
    lines = [
        _line("a", pod="web", container="nginx"),
        _line("b", pod="web", container="sidecar"),
    ]
    now = datetime(2026, 7, 26, 10, 30, 45, tzinfo=UTC)

    path = export_log_lines(lines, tmp_path, now=now)

    assert path.name == "korvid-web-20260726-103045.log"
    # Lines still carry the pod/container prefix for attribution.
    assert path.read_text() == "web/nginx a\nweb/sidecar b\n"


def test_export_truncates_long_pod_name(tmp_path: Path) -> None:
    """A 253-char pod name must not push the filename past OS limits."""
    lines = [_line("x", pod="p" * 253)]
    now = datetime(2026, 7, 26, 10, 30, 45, tzinfo=UTC)

    path = export_log_lines(lines, tmp_path, now=now)

    assert path.is_file()
    assert len(path.name.encode()) <= 255
