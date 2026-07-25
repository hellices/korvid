"""Audit log for cluster write operations (spec §6.2)."""

import json
from pathlib import Path

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
