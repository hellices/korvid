"""Audit log for cluster write operations (spec §6.2)."""

import json
from pathlib import Path

import pytest

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
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append(action="delete", kind="pods", namespace="default", name="w")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_audit_mode_enforced_on_existing_file(tmp_path: Path) -> None:
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
