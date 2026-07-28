"""Unit tests for `DebugController` (issue #97 U3c).

The controller owns the post-approval half of the debug fallback — readonly
and audit gates, uid re-check, the suspended kubectl debug run with pull
monitoring, outcome audit, and the retry hand-off. Everything arrives as
narrow callables, so it is tested here without an app.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from korvid.core.audit import AuditLog
from korvid.ui.debug import DebugController


class Harness:
    def __init__(
        self,
        *,
        audit: AuditLog | None = None,
        readonly: bool = False,
        uid_ok: bool = True,
        process_result: tuple[int | None, str | None] = (0, None),
    ) -> None:
        self.readonly = readonly
        self.audit_log = audit
        self._uid_ok = uid_ok
        self._process_result = process_result
        self.notifications: list[tuple[str, str]] = []
        self.suspends = 0
        self.refreshes = 0
        self.processes: list[list[str]] = []
        self.retries: list[tuple[str, str]] = []
        self.controller = DebugController(
            notify=self._notify,
            audit=lambda: self.audit_log,
            readonly=lambda: self.readonly,
            kube_context=lambda: None,
            pod_uid_unchanged=self._uid_unchanged,
            suspend=self._suspend,
            refresh=self._refresh,
            offer_pull_retry=self._offer_retry,
        )
        self.controller.run_process = self._run_process  # type: ignore[method-assign]  # unit tests stub the subprocess engine

    def _notify(self, message: str, *, severity: str = "information") -> None:
        self.notifications.append((message, severity))

    async def _uid_unchanged(self, namespace: str, name: str, uid: str, *, action: str) -> bool:
        if not self._uid_ok:
            self._notify(f"{action} cancelled - pod {name} was replaced", severity="warning")
        return self._uid_ok

    @contextlib.contextmanager
    def _suspend(self) -> Iterator[None]:
        self.suspends += 1
        yield

    def _refresh(self) -> None:
        self.refreshes += 1

    def _run_process(
        self,
        argv: list[str],
        banner: str,
        namespace: str,
        name: str,
        image: str,
        approved_uid: str | None,
    ) -> tuple[int | None, str | None]:
        self.processes.append(argv)
        return self._process_result

    def _offer_retry(
        self,
        namespace: str,
        name: str,
        container: str | None,
        approved_uid: str | None,
        image: str,
        reason: str,
    ) -> None:
        self.retries.append((image, reason))


def _entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_readonly_blocks_without_any_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path), readonly=True)
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    assert h.notifications == [
        ("Read-only mode: cluster writes are disabled", "warning"),
    ]
    assert not h.processes
    assert not audit_path.exists()


async def test_missing_audit_log_blocks_the_debug() -> None:
    h = Harness(audit=None)
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    assert h.notifications == [
        ("Writes disabled: no audit log configured", "warning"),
    ]
    assert not h.processes


async def test_replaced_pod_cancels_before_the_intent_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path), uid_ok=False)
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    assert not h.processes
    assert not audit_path.exists()


async def test_intent_audit_failure_blocks_the_run(tmp_path: Path) -> None:
    class ExplodingAudit(AuditLog):
        def append(self, **kwargs: Any) -> None:
            raise OSError("disk full")

    h = Harness(audit=ExplodingAudit(tmp_path / "audit.jsonl"))
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    assert ("Write blocked: audit log unavailable", "error") in h.notifications
    assert not h.processes


async def test_successful_run_audits_intent_then_success(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path))
    await h.controller.run("default", "api-1", "app", "u1", "busybox:1.36")
    assert h.suspends == 1
    assert h.refreshes == 1
    assert len(h.processes) == 1
    assert "--image=busybox:1.36" in " ".join(h.processes[0])
    outcomes = [e["outcome"] for e in _entries(audit_path)]
    assert outcomes == ["intent", "success"]
    assert not h.retries


async def test_pull_failure_audits_error_and_offers_the_retry(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path), process_result=(1, "ErrImagePull"))
    await h.controller.run("default", "api-1", None, "u1", "koolkits:jvm")
    outcomes = [e["outcome"] for e in _entries(audit_path)]
    assert outcomes == ["intent", "error: image pull failed (ErrImagePull)"]
    assert h.retries == [("koolkits:jvm", "ErrImagePull")]


async def test_replaced_pod_at_attach_time_audits_and_warns(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path), process_result=(None, None))
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    outcomes = [e["outcome"] for e in _entries(audit_path)]
    assert outcomes == ["intent", "error: pod replaced before attach"]
    assert any("was replaced since the prompt" in msg for msg, _sev in h.notifications)
    assert not h.retries


async def test_nonzero_exit_audits_error_and_hints_at_rbac(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path), process_result=(42, None))
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    outcomes = [e["outcome"] for e in _entries(audit_path)]
    assert outcomes == ["intent", "error: exit 42"]
    assert any("check RBAC" in msg for msg, _sev in h.notifications)


async def test_outcome_audit_failure_only_warns(tmp_path: Path) -> None:
    """The mutation already happened: a failed outcome append must warn, not
    raise out of the worker."""

    class FailAfterIntent(AuditLog):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.appends = 0

        def append(self, **kwargs: Any) -> None:
            self.appends += 1
            if self.appends > 1:
                raise OSError("disk full")
            super().append(**kwargs)

    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=FailAfterIntent(audit_path))
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    assert ("Audit write failed for the executed debug", "warning") in h.notifications
    assert [e["outcome"] for e in _entries(audit_path)] == ["intent"]


async def test_intent_audit_runs_off_the_event_loop(tmp_path: Path) -> None:
    """Audit appends fsync; a blocking append must not stall the loop."""
    blocked = asyncio.Event()

    class SlowAudit(AuditLog):
        def append(self, **kwargs: Any) -> None:
            time.sleep(0.05)  # simulates the fsync stall (worker thread)
            super().append(**kwargs)

    h = Harness(audit=SlowAudit(tmp_path / "audit.jsonl"))

    async def _heartbeat() -> None:
        blocked.set()

    task = asyncio.create_task(h.controller.run("default", "api-1", None, "u1", "busybox:1.36"))
    heartbeat = asyncio.create_task(_heartbeat())
    await asyncio.wait_for(blocked.wait(), timeout=1.0)  # loop stayed responsive
    await task
    await heartbeat
    assert h.processes
