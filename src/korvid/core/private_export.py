"""Owner-private, collision-safe text file exports."""

from __future__ import annotations

import os
import re
from pathlib import Path

_SAFE_STEM = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_SAFE_SUFFIX = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9_-]*")
_MAX_COLLISION_SUFFIX = 1000


def default_payload_export_dir() -> Path:
    """Directory for explicit provider-payload exports."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "korvid" / "agent-payloads"


def write_private_text(directory: Path, stem: str, suffix: str, content: str) -> Path:
    """Exclusively create a private UTF-8 text file and return its path.

    A numeric collision suffix is inserted before *suffix* so existing exports
    are never overwritten.

    Raises:
        ValueError: If *stem* or *suffix* is not a safe filename fragment.
        OSError: If the directory or file cannot be created.
    """
    if _SAFE_STEM.fullmatch(stem) is None or ".." in stem or _SAFE_SUFFIX.fullmatch(suffix) is None:
        raise ValueError("stem and suffix must be safe filename fragments")

    directory.mkdir(parents=True, exist_ok=True)
    for attempt in range(_MAX_COLLISION_SUFFIX):
        collision = "" if attempt == 0 else f"-{attempt}"
        path = directory / f"{stem}{collision}{suffix}"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with open(fd, "w", encoding="utf-8", newline="") as file:
                file.write(content)
        except FileExistsError:
            continue
        return path
    raise OSError(f"could not find a free export filename under {directory}")
