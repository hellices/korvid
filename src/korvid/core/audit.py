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
    """JSONL appender; one line per write operation."""

    def __init__(self, path: Path) -> None:
        self._path = path

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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
