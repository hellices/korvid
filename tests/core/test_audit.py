"""Audit log for cluster write operations (spec §6.2)."""

import json
import os
from pathlib import Path

import pytest

import korvid.core.audit as audit_module
from korvid.core.audit import AuditLog


def test_append_writes_jsonl_entry(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(action="delete", kind="pods", namespace="default", name="web-1")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "delete"
    assert entry["kind"] == "pods"
    assert entry["namespace"] == "default"
    assert entry["name"] == "web-1"
    assert entry["outcome"] == "success"
    assert entry["timestamp"]  # ISO timestamp present
    assert entry["group"] == ""  # GVR fields always present (core group = "")
    assert entry["version"] == ""


def test_append_records_target_gvr(tmp_path: Path) -> None:
    """kind alone is ambiguous across API groups: entries carry the full GVR."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(
        action="scale",
        kind="deployments",
        group="apps",
        version="v1",
        namespace="default",
        name="web",
    )
    entry = json.loads((tmp_path / "audit.jsonl").read_text())
    assert entry["group"] == "apps"
    assert entry["version"] == "v1"


def test_append_accumulates_lines(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(
        action="scale", kind="deployments", namespace="default", name="web", detail="replicas -> 5"
    )
    log.append(
        action="rollout_restart",
        kind="deployments",
        namespace="default",
        name="web",
        outcome="error: 403 Forbidden",
    )
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["detail"] == "replicas -> 5"
    assert json.loads(lines[1])["outcome"] == "error: 403 Forbidden"


def test_append_creates_parent_dirs(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "deep" / "nested" / "audit.jsonl")
    log.append(action="delete", kind="pods", namespace=None, name="x")
    assert (tmp_path / "deep" / "nested" / "audit.jsonl").exists()
    entry = json.loads((tmp_path / "deep" / "nested" / "audit.jsonl").read_text())
    assert entry["namespace"] is None


def test_audit_file_created_with_0600(tmp_path: Path) -> None:
    """Design contract: the audit log is created with 0600 permissions,
    regardless of the process umask."""
    if os.name == "nt":  # pragma: no cover
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append(action="delete", kind="pods", namespace="default", name="w")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_audit_mode_enforced_on_existing_file(tmp_path: Path) -> None:
    if os.name == "nt":  # pragma: no cover
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    path = tmp_path / "audit.jsonl"
    path.touch()
    path.chmod(0o644)
    AuditLog(path).append(action="delete", kind="pods", namespace="default", name="w")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_rotates_at_size_cap(tmp_path: Path) -> None:
    """Design contract: size-based rotation (default 50 MB; small cap here)."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=200, backups=2)
    for i in range(10):
        log.append(action="delete", kind="pods", namespace="default", name=f"pod-{i}")
    assert path.exists()
    assert (tmp_path / "audit.jsonl.1").exists()
    assert path.stat().st_size < 400  # rotation kept the live file bounded
    # every line everywhere is still valid JSON
    for f in tmp_path.iterdir():
        for line in f.read_text().splitlines():
            json.loads(line)


def test_rotation_hardens_backup_permissions(tmp_path: Path) -> None:
    """Rename preserves the source mode, so a permissive live file (e.g.
    0644 from a pre-0600 version) must be chmodded before it becomes a
    backup - the 0600 guarantee covers rotated files too."""
    if os.name == "nt":  # pragma: no cover
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    path = tmp_path / "audit.jsonl"
    path.write_text('{"legacy": true}\n' * 20)
    path.chmod(0o644)  # simulate a log written before the 0600 guarantee
    AuditLog(path, max_bytes=1, backups=2).append(
        action="delete", kind="pods", namespace="default", name="w"
    )
    backup = tmp_path / "audit.jsonl.1"
    assert backup.exists()
    assert (backup.stat().st_mode & 0o777) == 0o600
    assert (path.stat().st_mode & 0o777) == 0o600


def test_rotation_drops_oldest_backup(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=1, backups=2)  # rotate on every append
    for i in range(6):
        log.append(action="delete", kind="pods", namespace="default", name=f"pod-{i}")
    names = sorted(p.name for p in tmp_path.iterdir() if not p.name.endswith(".lock"))
    assert names == ["audit.jsonl", "audit.jsonl.1", "audit.jsonl.2"]
    # oldest backup holds the oldest surviving entry, not pod-0 (dropped)
    assert "pod-0" not in (tmp_path / "audit.jsonl.2").read_text()


def test_constructor_does_not_touch_filesystem(tmp_path: Path) -> None:
    """A bad audit path must not abort startup (e.g. --readonly sessions);
    it only blocks writes when append() is actually called."""
    bad = tmp_path / "not-a-dir"
    bad.write_text("file, not a directory")
    log = AuditLog(bad / "audit.jsonl")  # must not raise
    # macOS raises FileExistsError, Linux NotADirectoryError for the mkdir
    with pytest.raises((FileExistsError, NotADirectoryError)):
        log.append(action="delete", kind="pods", namespace="default", name="w")


def test_entry_records_kube_context(tmp_path: Path) -> None:
    """Entries from different clusters must be distinguishable: the shared
    default audit file records the kubeconfig context of every write."""
    path = tmp_path / "audit.jsonl"
    AuditLog(path, context="prod-cluster").append(
        action="delete", kind="pods", namespace="default", name="w"
    )
    assert json.loads(path.read_text())["context"] == "prod-cluster"


def test_entry_context_defaults_to_none(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append(action="delete", kind="pods", namespace="default", name="w")
    assert json.loads(path.read_text())["context"] is None


def test_zero_backups_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="backups"):
        AuditLog(tmp_path / "audit.jsonl", backups=0)


def test_concurrent_appends_do_not_race_rotation(tmp_path: Path) -> None:
    """append() runs in worker threads (asyncio.to_thread); concurrent calls
    at the size threshold must not race through unlink/rename."""
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=300, backups=2)

    def _write(i: int) -> None:
        log.append(action="delete", kind="pods", namespace="default", name=f"pod-{i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write, range(80)))  # raises if any rename/unlink races
    files = [p for p in tmp_path.iterdir() if not p.name.endswith(".lock")]
    assert path in files
    for f in files:
        for line in f.read_text().splitlines():
            json.loads(line)


def test_concurrent_appends_across_instances(tmp_path: Path) -> None:
    """Independent AuditLog instances (separate korvid sessions sharing the
    default path) serialize rotate+append through the sidecar file lock, not
    the per-instance threading.Lock."""
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "audit.jsonl"
    logs = [AuditLog(path, max_bytes=300, backups=2) for _ in range(4)]

    def _write(i: int) -> None:
        logs[i % len(logs)].append(
            action="delete", kind="pods", namespace="default", name=f"pod-{i}"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write, range(80)))  # raises if rotation races across instances
    files = [p for p in tmp_path.iterdir() if not p.name.endswith(".lock")]
    assert path in files
    for f in files:
        for line in f.read_text().splitlines():
            json.loads(line)  # interleaved writes would corrupt a line


def test_append_fsyncs_before_returning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The intent record must be durable (not just buffered) before the
    caller proceeds to mutate the cluster."""
    synced: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(action="delete", kind="pods", namespace="default", name="web-1")
    # at least the log file and its parent directory were synced
    assert len(synced) >= 2


def test_append_fails_closed_when_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_fsync(fd: int) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(OSError, match="disk gone"):
        log.append(action="delete", kind="pods", namespace="default", name="web-1")


def test_append_fails_closed_when_dir_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory-entry durability failures propagate like file fsync ones:
    a record in a file that may not survive a crash is not persisted."""

    def failing_open(path: object, flags: int, *args: object) -> int:
        if flags == os.O_RDONLY:  # only the directory sync opens read-only
            raise OSError("cannot open directory")
        return real_open(path, flags, *args)  # type: ignore[arg-type]  # passthrough to the real os.open

    real_open = os.open
    monkeypatch.setattr(os, "open", failing_open)
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(OSError, match="cannot open directory"):
        log.append(action="delete", kind="pods", namespace="default", name="web-1")


def test_append_syncs_parents_of_created_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mkdir -p alone leaves the new directory entries unpersisted: each
    created directory's entry lives in its parent, so every containing parent
    must be fsynced or a crash after the intent append can drop the whole
    audit tree (fail-closed durability invariant)."""
    synced: list[Path] = []
    real_sync = audit_module._sync_dir

    def recording_sync(path: Path) -> None:
        synced.append(path)
        real_sync(path)

    monkeypatch.setattr(audit_module, "_sync_dir", recording_sync)
    log = AuditLog(tmp_path / "state" / "korvid" / "audit.jsonl")
    log.append(
        action="delete",
        kind="pods",
        group="",
        version="v1",
        namespace="default",
        name="web-1",
        detail="",
        outcome="intent",
    )
    # 'state' was created inside tmp_path and 'korvid' inside 'state': both
    # containing parents must have been synced (plus the leaf for the file).
    assert tmp_path in synced
    assert tmp_path / "state" in synced
    assert tmp_path / "state" / "korvid" in synced
