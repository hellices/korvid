"""Append-only audit log for cluster write operations (spec §6.2).

Every executed write — user keybinding or agent tool, success or failure —
is appended as one JSON line so operators can reconstruct exactly what
korvid changed in a cluster and when.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


def default_audit_path() -> Path:
    """XDG state dir (falls back to ~/.local/state) / korvid/audit.jsonl."""
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "korvid" / "audit.jsonl"


class AuditLog:
    """JSONL appender; one line per write operation.

    The file is created (and kept) with 0600 permissions per the design
    contract. ``append`` is synchronous file I/O — call it via
    ``asyncio.to_thread`` from async contexts.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        action: str,
        kind: str,
        namespace: str | None,
        name: str,
        detail: str = "",
        outcome: str = "success",
    ) -> None:
        entry = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "action": action,
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "detail": detail,
            "outcome": outcome,
        }
        # O_CREAT with 0600 is umask-filtered, so enforce the mode on the
        # open descriptor - it must hold for pre-existing files too.
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            f = os.fdopen(fd, "a", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with f:
            f.write(json.dumps(entry) + "\n")
