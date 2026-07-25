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

    The file is created (and kept) with 0600 permissions and rotated by size
    per the design contract (default 50 MB, configurable backup retention).
    ``append`` is synchronous file I/O — call it via ``asyncio.to_thread``
    from async contexts. All filesystem work happens inside ``append`` so a
    bad audit path never aborts startup; it only blocks writes (fail-closed).
    """

    def __init__(self, path: Path, *, max_bytes: int = 50 * 1024 * 1024, backups: int = 3) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._backups = backups

    def _rotate_if_needed(self) -> None:
        """Rename audit.jsonl -> .1 -> .2 ... when the size cap is hit,
        dropping the oldest backup beyond the retention count."""
        try:
            if self._path.stat().st_size < self._max_bytes:
                return
        except FileNotFoundError:
            return
        oldest = self._path.with_name(f"{self._path.name}.{self._backups}")
        oldest.unlink(missing_ok=True)
        for i in range(self._backups - 1, 0, -1):
            src = self._path.with_name(f"{self._path.name}.{i}")
            if src.exists():
                src.rename(self._path.with_name(f"{self._path.name}.{i + 1}"))
        if self._backups > 0:
            self._path.rename(self._path.with_name(f"{self._path.name}.1"))
        else:
            self._path.unlink(missing_ok=True)

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
        self._rotate_if_needed()
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
