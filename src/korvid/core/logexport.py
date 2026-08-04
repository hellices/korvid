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

# Longest allowed sanitized stem. Keeps the whole filename (prefix, stem,
# timestamp, collision suffix, extension) far below the common 255-byte
# filesystem component limit even for 253-char Kubernetes pod names.
_MAX_STEM_LEN = 100

# Bounded scan for a free filename when saves collide within one second.
_MAX_COLLISION_SUFFIX = 1000


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

    The filename is ``korvid-<pod>-<YYYYmmdd-HHMMSS>.log`` when every line
    comes from one pod and ``korvid-logs-<...>.log`` when lines span multiple
    pods; a ``-N`` suffix is appended if the name is already taken so a save
    can never overwrite an earlier one.  Multi-source buffers prefix each
    line with ``pod/container`` so the file stays attributable; kubelet
    timestamps are written in ISO form when present.

    Raises:
        ValueError: If *lines* is empty (nothing to save).
        OSError: If the directory or file cannot be written.
    """
    if not lines:
        raise ValueError("no log lines to export")

    sources = {(line.pod, line.container) for line in lines}
    multi_source = len(sources) > 1
    pods = {line.pod for line in lines}
    stem = _sanitize(pods.pop()) if len(pods) == 1 else "logs"

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=True)

    rendered: list[str] = []
    for line in lines:
        parts: list[str] = []
        if multi_source:
            parts.append(f"{line.pod}/{line.container}")
        if line.timestamp is not None:
            parts.append(line.timestamp.isoformat())
        parts.append(line.text)
        rendered.append(" ".join(parts))
    content = "\n".join(rendered) + "\n"

    for attempt in range(_MAX_COLLISION_SUFFIX):
        suffix = "" if attempt == 0 else f"-{attempt}"
        path = directory / f"korvid-{stem}-{stamp}{suffix}.log"
        try:
            # Exclusive create (a same-second save picks the next suffix
            # instead of truncating the earlier export), owner-only mode
            # (cluster logs may be sensitive), and explicit UTF-8 so a
            # non-UTF-8 locale can't fail on non-ASCII log text.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with open(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
        except FileExistsError:
            continue
        return path
    raise OSError(f"could not find a free export filename under {directory}")


def _sanitize(name: str) -> str:
    """Reduce *name* to a safe, bounded filename fragment (never empty)."""
    cleaned = _UNSAFE_CHARS.sub("_", name)
    # No path-traversal fragments and no hidden-file leading dot.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "_")
    cleaned = cleaned.lstrip(".")[:_MAX_STEM_LEN]
    return cleaned or "logs"
