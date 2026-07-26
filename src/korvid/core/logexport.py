"""Export log buffers to plain-text files (issue #43, ``ctrl+s``).

No Textual imports — pure file I/O so the UI layer only wires the action.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path

from korvid.k8s.logs import LogLine

# Anything outside a conservative filename alphabet is replaced; pod names
# are DNS labels in practice, but interpolated file names are never trusted.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def default_log_export_dir() -> Path:
    """Directory for saved log files: ``$XDG_DATA_HOME/korvid/logs`` or
    ``~/.local/share/korvid/logs``."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "korvid" / "logs"


def export_log_lines(
    lines: list[LogLine],
    directory: Path,
    *,
    now: datetime | None = None,
) -> Path:
    """Write *lines* to a generated file under *directory* and return its path.

    The filename is ``korvid-<pod>-<YYYYmmdd-HHMMSS>.log`` for a single-pod
    buffer and ``korvid-logs-<...>.log`` when lines span multiple sources.
    Multi-source buffers prefix each line with ``pod/container`` so the file
    stays attributable; kubelet timestamps are written in ISO form when
    present.

    Raises:
        ValueError: If *lines* is empty (nothing to save).
        OSError: If the directory or file cannot be written.
    """
    if not lines:
        raise ValueError("no log lines to export")

    sources = {(line.pod, line.container) for line in lines}
    multi_source = len(sources) > 1
    stem = "logs" if multi_source else _sanitize(lines[0].pod)

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"korvid-{stem}-{stamp}.log"

    rendered: list[str] = []
    for line in lines:
        parts: list[str] = []
        if multi_source:
            parts.append(f"{line.pod}/{line.container}")
        if line.timestamp is not None:
            parts.append(line.timestamp.isoformat())
        parts.append(line.text)
        rendered.append(" ".join(parts))
    path.write_text("\n".join(rendered) + "\n")
    return path


def _sanitize(name: str) -> str:
    """Reduce *name* to a safe filename fragment (never empty)."""
    cleaned = _UNSAFE_CHARS.sub("_", name)
    # No path-traversal fragments and no hidden-file leading dot.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "_")
    return cleaned.lstrip(".") or "logs"
