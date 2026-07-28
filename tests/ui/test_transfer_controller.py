"""Unit tests for TransferController (issue #91 U3a).

The controller owns the transfer execution lifecycle — serialization,
uid re-verification, fail-closed intent audit, stream with progress,
outcome audit — and is testable here without the full app: every
dependency is a constructor-injected callable.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

from korvid.core.audit import AuditLog
from korvid.core.transfer import TransferSpec
from korvid.ui.transfer import TransferController, TransferProgress

SUCCESS = json.dumps({"metadata": {}, "status": "Success"}).encode()


class FakeProgress:
    """Stands in for TransferProgressScreen."""

    def __init__(self) -> None:
        self.counts: list[int] = []

    def update_progress(self, count: int) -> None:
        self.counts.append(count)


class FakeMsg:
    def __init__(self, data: bytes) -> None:
        self.data = data


class FakeExecSession:
    """Yields pre-baked stdout frames like an exec websocket would."""

    def __init__(self, frames: list[bytes], stall: bool = False) -> None:
        self._frames = frames
        self._stall = stall

    def __aiter__(self) -> FakeExecSession:
        return self

    async def __anext__(self) -> FakeMsg:
        if self._frames:
            return FakeMsg(self._frames.pop(0))
        if self._stall:
            await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def send_bytes(self, data: bytes) -> None:  # uploads
        pass

    async def close(self) -> None:
        pass


class Harness:
    """Builds a controller wired to recording fakes."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        audit: AuditLog | None = None,
        opener: object = "default",
        uid_ok: bool = True,
        frames: list[bytes] | None = None,
        stall: bool = False,
    ) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.progress_screens: list[FakeProgress] = []
        self.closed: list[FakeProgress] = []
        self.exec_calls: list[tuple[str, str, str | None]] = []
        self.uid_checks: list[tuple[str, str, str]] = []
        self._uid_ok = uid_ok
        self._audit = audit
        self._frames = (
            frames
            if frames is not None
            else [b"\x01" + _tar_bytes("payload.txt", b"hello"), b"\x03" + SUCCESS]
        )
        self._stall = stall
        if opener == "default":
            opener = self._open_exec
        self._opener = opener
        self.controller = TransferController(
            notify=self._notify,
            open_pod_exec=lambda: self._opener,  # type: ignore[arg-type,return-value]  # duck-typed fake
            audit=lambda: self._audit,
            pod_uid_unchanged=self._uid_unchanged,
            show_progress=self._show_progress,
            close_progress=self._close_progress,
        )

    def _notify(self, message: str, *, severity: str = "information") -> None:
        self.notifications.append((message, severity))

    def _open_exec(
        self, namespace: str, name: str, container: str | None, command: list[str], *, stdin: bool
    ) -> contextlib.AbstractAsyncContextManager[Any]:
        self.exec_calls.append((namespace, name, container))
        session = FakeExecSession(list(self._frames), stall=self._stall)

        @contextlib.asynccontextmanager
        async def _cm() -> Any:
            yield session

        return _cm()

    async def _uid_unchanged(self, namespace: str, name: str, uid: str, *, action: str) -> bool:
        self.uid_checks.append((namespace, name, uid))
        if not self._uid_ok:
            self._notify(f"{action} cancelled - pod {name} was replaced", severity="warning")
        return self._uid_ok

    async def _show_progress(self, label: str) -> FakeProgress:
        screen = FakeProgress()
        self.progress_screens.append(screen)
        return screen

    def _close_progress(self, screen: TransferProgress) -> None:
        assert isinstance(screen, FakeProgress)
        self.closed.append(screen)


def _tar_bytes(name: str, payload: bytes) -> bytes:
    """A tar archive holding one file, as `tar cf - ...` would emit it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _spec(tmp_path: Path, direction: str = "download") -> TransferSpec:
    return TransferSpec(
        direction=direction,  # type: ignore[arg-type]  # test literal
        remote_path="/data/payload.txt",
        local_path=str(tmp_path / "out"),
    )


def _entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def _wait_until(cond: Any, timeout: float = 5.0) -> None:
    """Poll `cond()` with real sleeps; the stream setup crosses threads
    (`asyncio.to_thread` audit), so bare `sleep(0)` loops are not enough."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


async def test_download_success_audits_intent_and_success(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(tmp_path, audit=AuditLog(audit_path, context="test"))
    await h.controller.run("ns", "api-1", None, _spec(tmp_path), "uid-1")
    entries = _entries(audit_path)
    assert [e["outcome"] for e in entries] == ["intent", "success"]
    assert entries[0]["action"] == "transfer_download"
    assert h.uid_checks == [("ns", "api-1", "uid-1")]
    assert h.closed == h.progress_screens  # progress closed
    assert any("downloaded" in m for m, _ in h.notifications)
    assert not h.controller.in_flight


async def test_no_audit_blocks_transfer_fail_closed(tmp_path: Path) -> None:
    h = Harness(tmp_path, audit=None)
    await h.controller.run("ns", "api-1", None, _spec(tmp_path), None)
    assert h.exec_calls == []
    assert any("no audit log configured" in m for m, s in h.notifications if s == "error")


async def test_intent_audit_failure_blocks_transfer(tmp_path: Path) -> None:
    class BoomAudit:
        def append(self, **kwargs: Any) -> None:
            raise OSError("disk full")

    h = Harness(tmp_path, audit=BoomAudit())  # type: ignore[arg-type]  # duck-typed fake
    await h.controller.run("ns", "api-1", None, _spec(tmp_path), None)
    assert h.exec_calls == []
    assert any("audit log unavailable" in m for m, s in h.notifications if s == "error")


async def test_replaced_pod_uid_cancels_before_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(tmp_path, audit=AuditLog(audit_path, context="test"), uid_ok=False)
    await h.controller.run("ns", "api-1", None, _spec(tmp_path), "uid-1")
    assert h.exec_calls == []
    assert not audit_path.exists()


async def test_second_run_refused_while_in_flight(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(tmp_path, audit=AuditLog(audit_path, context="test"), frames=[b"\x01x"], stall=True)
    first = asyncio.create_task(h.controller.run("ns", "api-1", None, _spec(tmp_path), None))
    await _wait_until(lambda: h.controller.task is not None)
    assert h.controller.in_flight
    await h.controller.run("ns", "api-2", None, _spec(tmp_path), None)
    assert any("already in progress" in m for m, _ in h.notifications)
    h.controller.cancel()
    await first
    assert not h.controller.in_flight


async def test_cancel_records_cancelled_outcome_with_bytes(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(
        tmp_path, audit=AuditLog(audit_path, context="test"), frames=[b"\x01abc"], stall=True
    )
    task = asyncio.create_task(h.controller.run("ns", "api-1", None, _spec(tmp_path), None))
    await _wait_until(lambda: h.controller.task is not None)
    h.controller.cancel()
    await task
    entries = _entries(audit_path)
    assert [e["outcome"] for e in entries] == ["intent", "cancelled"]
    assert "bytes=" in entries[1]["detail"]
    assert h.controller.task is None
    assert h.closed == h.progress_screens


async def test_transfer_error_audits_error_outcome(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    # An empty exec session yields no tar payload -> TransferError.
    h = Harness(tmp_path, audit=AuditLog(audit_path, context="test"), frames=[])
    await h.controller.run("ns", "api-1", None, _spec(tmp_path), None)
    entries = _entries(audit_path)
    assert entries[1]["outcome"].startswith("error:")
    assert any(s == "error" for _, s in h.notifications)


async def test_missing_exec_opener_returns_silently(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(tmp_path, audit=AuditLog(audit_path, context="test"), opener=None)
    await h.controller.run("ns", "api-1", None, _spec(tmp_path), None)
    assert not audit_path.exists()
    assert h.notifications == []
