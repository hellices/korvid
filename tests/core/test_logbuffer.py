"""Tests for LogBuffer ring buffer with overflow and search."""

from korvid.core.logbuffer import LogBuffer
from korvid.k8s.logs import LogLine


def test_append_preserves_order() -> None:
    """Append preserves order; lines() returns list."""
    buf = LogBuffer(max_lines=5)
    line1 = LogLine(pod="pod1", container="c1", text="line1")
    line2 = LogLine(pod="pod2", container="c2", text="line2")
    line3 = LogLine(pod="pod3", container="c3", text="line3")

    buf.append(line1)
    buf.append(line2)
    buf.append(line3)

    result = buf.lines()
    assert isinstance(result, list)
    assert len(result) == 3
    assert result[0] == line1
    assert result[1] == line2
    assert result[2] == line3


def test_ring_drop_beyond_max_lines() -> None:
    """Appending beyond max_lines drops oldest and sets overflowed=True."""
    buf = LogBuffer(max_lines=3)
    line1 = LogLine(pod="pod1", container="c1", text="first")
    line2 = LogLine(pod="pod2", container="c2", text="second")
    line3 = LogLine(pod="pod3", container="c3", text="third")
    line4 = LogLine(pod="pod4", container="c4", text="fourth")

    buf.append(line1)
    buf.append(line2)
    buf.append(line3)
    assert not buf.overflowed

    buf.append(line4)
    assert buf.overflowed
    assert len(buf.lines()) == 3
    assert buf.lines()[0] == line2
    assert buf.lines()[1] == line3
    assert buf.lines()[2] == line4


def test_overflowed_stays_true() -> None:
    """Once overflowed is True, it stays True even if buffer shrinks below max."""
    buf = LogBuffer(max_lines=3)
    line1 = LogLine(pod="pod1", container="c1", text="first")
    line2 = LogLine(pod="pod2", container="c2", text="second")
    line3 = LogLine(pod="pod3", container="c3", text="third")
    line4 = LogLine(pod="pod4", container="c4", text="fourth")

    buf.append(line1)
    buf.append(line2)
    buf.append(line3)
    assert not buf.overflowed

    buf.append(line4)
    assert buf.overflowed
    assert len(buf.lines()) == 3

    buf.clear()
    assert not buf.overflowed


def test_search_case_insensitive() -> None:
    """search: case-insensitive substring, returns correct indices."""
    buf = LogBuffer(max_lines=10)
    line1 = LogLine(pod="pod1", container="c1", text="Hello World")
    line2 = LogLine(pod="pod2", container="c2", text="hello earth")
    line3 = LogLine(pod="pod3", container="c3", text="HELLO sky")
    line4 = LogLine(pod="pod4", container="c4", text="goodbye world")

    buf.append(line1)
    buf.append(line2)
    buf.append(line3)
    buf.append(line4)

    # Search for "hello" should match lines 0, 1, 2
    result = buf.search("hello")
    assert result == [0, 1, 2]

    # Search for "world" should match lines 0, 3
    result = buf.search("world")
    assert result == [0, 3]

    # Search for "EARTH" should match line 1
    result = buf.search("EARTH")
    assert result == [1]


def test_search_empty_pattern() -> None:
    """Empty pattern returns []."""
    buf = LogBuffer(max_lines=10)
    buf.append(LogLine(pod="pod1", container="c1", text="some text"))
    assert buf.search("") == []


def test_search_no_match() -> None:
    """No match returns []."""
    buf = LogBuffer(max_lines=10)
    buf.append(LogLine(pod="pod1", container="c1", text="hello"))
    buf.append(LogLine(pod="pod2", container="c2", text="world"))
    assert buf.search("xyz") == []


def test_clear_resets_lines_and_overflowed() -> None:
    """clear resets lines and overflowed flag."""
    buf = LogBuffer(max_lines=2)
    buf.append(LogLine(pod="pod1", container="c1", text="line1"))
    buf.append(LogLine(pod="pod2", container="c2", text="line2"))
    buf.append(LogLine(pod="pod3", container="c3", text="line3"))
    assert len(buf.lines()) == 2
    assert buf.overflowed

    buf.clear()
    assert len(buf.lines()) == 0
    assert not buf.overflowed
