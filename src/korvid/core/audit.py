"""Append-only audit log for cluster write operations (spec §6.2).

Every write — user keybinding or agent tool, success or failure — is
recorded as JSON lines so operators can reconstruct exactly what korvid
changed in a cluster and when. A write typically produces two events: an
``intent`` record persisted *before* the mutation (fail-closed: if it cannot
be written, the write is blocked) and an outcome record
(``success``/``error: ...``) after.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

try:  # POSIX interprocess lock
    import fcntl
except ImportError:  # pragma: no cover - fcntl is absent only on Windows; CI runs on POSIX
    fcntl = None  # type: ignore[assignment]  # absence selects the msvcrt path below

try:  # Windows interprocess lock
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]  # absent on POSIX, where flock is used


def _lock_file(fd: int) -> None:
    """Take an exclusive interprocess lock on ``fd``.

    Uses ``flock`` on POSIX and ``msvcrt.locking`` on Windows; on platforms
    with neither, callers fall back to in-process serialization only.
    """
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover - Windows-only branch; CI runs on POSIX
        # LK_LOCK retries once a second for ~10s, then raises OSError:
        # under pathological contention the audit write fails closed
        # instead of interleaving.
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)


def _unlock_file(fd: int) -> None:
    if fcntl is not None:
        return  # flock is released when the descriptor is closed
    if msvcrt is not None:  # pragma: no cover - Windows-only branch; CI runs on POSIX
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def default_audit_path() -> Path:
    """XDG state dir (falls back to ~/.local/state) / korvid/audit.jsonl."""
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "korvid" / "audit.jsonl"


class AuditLog:
    """JSONL appender; one line per audit event (writes usually emit two:
    intent before the mutation, outcome after - see the module docstring).

    The file is created (and kept) with 0600 permissions and rotated by size
    per the design contract (default 50 MB). Retention is size-bounded, not
    an infinite archive: rotation keeps ``backups`` numbered backups (at
    least one, so the most recent history always survives a rotation) and
    drops the oldest file beyond that count.
    Rotation and append are serialized with an in-process lock plus an
    interprocess lock (``flock`` on POSIX, ``msvcrt.locking`` on Windows) on
    a sidecar lock file, since several korvid sessions may share the default
    path. ``append`` is synchronous file I/O —
    call it via ``asyncio.to_thread`` from async contexts. All filesystem
    work happens inside ``append`` so a bad audit path never aborts startup;
    it only blocks writes (fail-closed).

    ``context`` identifies the kubeconfig context (cluster) the entries
    belong to; without it, writes to identically named objects in different
    clusters would be indistinguishable.
    """

    def __init__(
        self,
        path: Path,
        *,
        context: str | None = None,
        max_bytes: int = 50 * 1024 * 1024,
        backups: int = 3,
    ) -> None:
        if backups < 1:
            raise ValueError("backups must be >= 1: rotation must never discard audit history")
        self._path = path
        self._context = context
        self._max_bytes = max_bytes
        self._backups = backups
        self._lock = threading.Lock()

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
                # rename preserves the mode: harden files written before the
                # 0600 guarantee so backups never stay world-readable.
                src.chmod(0o600)
                src.rename(self._path.with_name(f"{self._path.name}.{i + 1}"))
        self._path.chmod(0o600)
        self._path.rename(self._path.with_name(f"{self._path.name}.1"))

    def append(
        self,
        *,
        action: str,
        kind: str,
        namespace: str | None,
        name: str,
        group: str = "",
        api_version: str = "",
        detail: str = "",
        outcome: str = "success",
    ) -> None:
        entry = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "context": self._context,
            "action": action,
            "kind": kind,
            # kind alone is ambiguous: a custom-group resource can share its
            # plural with a built-in, so record the full GVR of the target.
            "group": group,
            "apiVersion": api_version,
            "namespace": namespace,
            "name": name,
            "detail": detail,
            "outcome": outcome,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            # Interprocess lock on a sidecar file (the log itself gets renamed
            # by rotation) serializes rotate+append across korvid sessions.
            lock_fd = os.open(
                self._path.with_name(f"{self._path.name}.lock"),
                os.O_WRONLY | os.O_CREAT,
                0o600,
            )
            try:
                _lock_file(lock_fd)
                try:
                    self._locked_append(entry)
                finally:
                    _unlock_file(lock_fd)
            finally:
                os.close(lock_fd)

    def _locked_append(self, entry: dict[str, str | None]) -> None:
        self._rotate_if_needed()
        # O_CREAT with 0600 is umask-filtered, so enforce the mode on the
        # open descriptor - it must hold for pre-existing files too.
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if hasattr(os, "fchmod"):  # not available on Windows
                os.fchmod(fd, 0o600)
            f = os.fdopen(fd, "a", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with f:
            f.write(json.dumps(entry) + "\n")
