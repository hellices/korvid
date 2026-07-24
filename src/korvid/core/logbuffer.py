"""Ring buffer for log lines with overflow tracking and search."""

from __future__ import annotations

from collections import deque

from korvid.k8s.logs import LogLine


class LogBuffer:
    """Ring buffer for log lines with overflow tracking and substring search."""

    def __init__(self, max_lines: int = 5000) -> None:
        """Initialize LogBuffer with max capacity.

        Args:
            max_lines: Maximum number of lines to retain. Defaults to 5000.
        """
        self._buffer: deque[LogLine] = deque(maxlen=max_lines)
        self._overflowed: bool = False

    def append(self, line: LogLine) -> None:
        """Append a log line to the buffer.

        If buffer is at capacity, the oldest line is dropped and
        overflowed flag is set to True.

        Args:
            line: LogLine to append.
        """
        if len(self._buffer) == self._buffer.maxlen:
            self._overflowed = True
        self._buffer.append(line)

    def lines(self) -> list[LogLine]:
        """Return all lines in the buffer as a list.

        Returns:
            List of LogLine objects in order.
        """
        return list(self._buffer)

    @property
    def overflowed(self) -> bool:
        """Return True if the buffer has ever dropped lines.

        Returns:
            True if any lines were dropped due to overflow, False otherwise.
        """
        return self._overflowed

    def search(self, pattern: str) -> list[int]:
        """Search for case-insensitive substring in log text.

        Args:
            pattern: Substring to search for. Empty pattern returns [].

        Returns:
            List of indices into lines() where pattern matches (case-insensitive).
        """
        if not pattern:
            return []

        pattern_lower = pattern.lower()
        matches: list[int] = []

        for i, line in enumerate(self._buffer):
            if pattern_lower in line.text.lower():
                matches.append(i)

        return matches

    def clear(self) -> None:
        """Clear all lines and reset overflow flag."""
        self._buffer.clear()
        self._overflowed = False
