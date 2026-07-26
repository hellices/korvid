"""Append-only audit log for cluster write operations (spec §6.2).

Every write — user keybinding or agent tool, success or failure — is
recorded as JSON lines so operators can reconstruct exactly what korvid
changed in a cluster and when. A write typically produces two events: an
``intent`` record persisted *before* the mutation (fail-closed: if it cannot
be written, the write is blocked) and an outcome record
(``success``/``error: ...``) after.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import logging
import os
import threading
from collections.abc import Iterator
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

logger = logging.getLogger(__name__)


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


@contextlib.contextmanager
def interprocess_lock(lock_path: Path) -> Iterator[None]:
    """Exclusive cross-process mutex backed by ``lock_path``.

    Used wherever multiple korvid processes coordinate around a shared
    state file (audit log, MCP endpoint discovery record). The lock file
    itself is left in place - deleting it would reintroduce the race.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as fh:
        _lock_file(fh.fileno())
        try:
            yield
        finally:
            _unlock_file(fh.fileno())


def _sync_dir(path: Path) -> None:
    """fsync a directory so entry changes (create/rename) are durable.

    On POSIX a failure propagates - losing the directory entry can lose the
    just-synced record, so it must fail closed like the file fsync. Windows
    cannot open directories with ``os.open``, but NTFS metadata operations
    are journaled there, so skipping is safe.
    """
    if os.name == "nt":  # pragma: no cover - Windows-only branch; CI runs on POSIX
        return
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _mkdirs_durable(path: Path) -> None:
    """``mkdir -p`` that also fsyncs the parent of every directory it creates.

    A new directory's entry lives in its *parent* directory: the later
    ``_sync_dir(self._path.parent)`` in ``_locked_append`` only persists the
    log file's entry inside the leaf. Without syncing each containing parent
    here, the first append (the normal default-path case) could return with
    the whole audit directory tree still unpersisted - a crash would then
    lose the intent record after the cluster mutation started, breaking the
    fail-closed invariant.
    """
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:  # filesystem root; nothing above to create
            break
        current = parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        _sync_dir(directory.parent)


def default_audit_path() -> Path:
    """XDG state dir (falls back to ~/.local/state) / korvid/audit.jsonl."""
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "korvid" / "audit.jsonl"


def _default_actor() -> str:
    """The OS user running korvid — audit entries must answer *who*."""
    try:
        return getpass.getuser()
    except OSError:  # no passwd entry / no env hints (containers, CI)
        return "unknown"


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
    clusters would be indistinguishable. ``actor`` records *who* performed
    each action (issue #39 requires reveal records to answer who/when/which
    key); it defaults to the OS user running korvid.
    """

    def __init__(
        self,
        path: Path,
        *,
        context: str | None = None,
        actor: str | None = None,
        max_bytes: int = 50 * 1024 * 1024,
        backups: int = 3,
    ) -> None:
        if backups < 1:
            raise ValueError("backups must be >= 1: rotation must never discard audit history")
        self._path = path
        self._context = context
        self._actor = actor if actor is not None else _default_actor()
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
        version: str = "",
        detail: str = "",
        outcome: str = "success",
    ) -> None:
        entry = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "context": self._context,
            "actor": self._actor,
            "action": action,
            "kind": kind,
            # kind alone is ambiguous: a custom-group resource can share its
            # plural with a built-in, so record the full GVR of the target.
            "group": group,
            "version": version,
            "namespace": namespace,
            "name": name,
            "detail": detail,
            "outcome": outcome,
        }
        _mkdirs_durable(self._path.parent)
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
        # A crash or ENOSPC mid-append can leave a torn final record; the new
        # record must never be concatenated onto it, or the supposedly
        # persisted intent would not be a valid JSONL entry. Repair failures
        # propagate: the write stays blocked (fail-closed).
        self._repair_torn_tail()
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
            # The record must be durable before the caller proceeds to mutate
            # the cluster: a buffered write alone can be lost to a crash and
            # silently break the fail-closed audit invariant. Any fsync
            # failure propagates so the write stays blocked.
            f.flush()
            os.fsync(f.fileno())
        # File creation and rotation renames live in the directory entry, so
        # sync the parent too - otherwise the freshly synced data can belong
        # to a file that does not survive the crash.
        _sync_dir(self._path.parent)

    def _repair_torn_tail(self) -> None:
        """Terminate a torn final record with a newline (caller holds the lock).

        A previous append that crashed between ``write`` and a completed
        ``fsync`` (or hit ENOSPC mid-write) can leave the live log without a
        trailing newline. Writing the terminator makes the torn tail an
        explicit, self-contained invalid line that readers can flag, and
        guarantees the next record starts a valid JSONL entry. Failures
        propagate so the pending write stays blocked.
        """
        try:
            fd = os.open(self._path, os.O_RDWR)
        except FileNotFoundError:
            return  # nothing to repair; the append below creates the file
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            if size == 0:
                return
            os.lseek(fd, size - 1, os.SEEK_SET)
            if os.read(fd, 1) == b"\n":
                return
            logger.warning(
                "audit log %s ended mid-record (crash or full disk); terminating the torn tail",
                self._path,
            )
            os.write(fd, b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
