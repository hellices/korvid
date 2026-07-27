"""Tests for pod file transfer UI — ctrl+t dialog, gates, audit (issue #47)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.k8s.models import PodSummary
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.transfer_screen import TransferProgressScreen, TransferScreen
from tests.ui.test_app import make_app
from tests.ui.waits import until

SUCCESS = json.dumps({"metadata": {}, "status": "Success"}).encode()


def _pod(name: str, containers: tuple[str, ...] = ("app",)) -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        qos="-",
        containers=containers,
    )


def tar_bytes(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class FakeMsg:
    def __init__(self, data: bytes) -> None:
        self.data = data


class FakeWs:
    def __init__(self, frames: list[bytes], *, stall: bool = False) -> None:
        self._frames = list(frames)
        self._stall = stall
        self.sent: list[bytes] = []

    def __aiter__(self) -> FakeWs:
        return self

    async def __anext__(self) -> FakeMsg:
        if self._frames:
            return FakeMsg(self._frames.pop(0))
        if self._stall:
            await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


class FakeExecOpener:
    """Stands in for KubeClient.open_pod_exec."""

    def __init__(self, frames: list[bytes] | None = None, *, stall: bool = False) -> None:
        self._frames = frames or []
        self._stall = stall
        self.calls: list[dict[str, Any]] = []
        self.ws: FakeWs | None = None

    def __call__(
        self,
        namespace: str,
        pod: str,
        container: str | None,
        command: list[str],
        *,
        stdin: bool,
    ) -> contextlib.AbstractAsyncContextManager[Any]:
        self.calls.append(
            {
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "command": command,
                "stdin": stdin,
            }
        )
        self.ws = FakeWs(list(self._frames), stall=self._stall)

        @contextlib.asynccontextmanager
        async def _cm() -> AsyncIterator[FakeWs]:
            assert self.ws is not None
            yield self.ws

        return _cm()


def _dialog(app: object) -> TransferScreen:
    screen = app.screen  # type: ignore[attr-defined]  # KorvidApp in tests
    assert isinstance(screen, TransferScreen)
    return screen


def audit_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def test_ctrl_t_requires_pods_kind() -> None:
    app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener())
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        app.current_kind = "deployments"
        await pilot.press("ctrl+t")
        await until(
            pilot,
            lambda: any("only available for pods" in str(n.message) for n in app._notifications),
            label="warning toast",
        )
        assert not isinstance(app.screen, TransferScreen)


async def test_ctrl_t_unavailable_without_exec_support() -> None:
    app = make_app([_pod("api-1")])  # no open_pod_exec injected
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(
            pilot,
            lambda: any("unavailable" in str(n.message) for n in app._notifications),
            label="unavailable toast",
        )
        assert not isinstance(app.screen, TransferScreen)


async def test_ctrl_t_opens_dialog_with_pod_target() -> None:
    app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener())
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        assert "api-1" in str(_dialog(app).query_one(".transfer-title", Static).render())


async def test_escape_closes_dialog_without_transfer() -> None:
    opener = FakeExecOpener()
    app = make_app([_pod("api-1")], open_pod_exec=opener)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, TransferScreen), label="closed")
        assert opener.calls == []


async def test_dialog_validation_error_keeps_dialog_open() -> None:
    opener = FakeExecOpener()
    app = make_app([_pod("api-1")], open_pod_exec=opener)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        # Submit with an empty remote path: dialog stays, transfer never runs.
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("remote path" in str(n.message) for n in app._notifications),
            label="validation toast",
        )
        assert isinstance(app.screen, TransferScreen)
        assert opener.calls == []


async def test_download_writes_file_and_audits(tmp_path: Path) -> None:
    payload = b"heap dump bytes"
    opener = FakeExecOpener([b"\x01" + tar_bytes("app.log", payload), b"\x03" + SUCCESS])
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1")],
        open_pod_exec=opener,
        audit=AuditLog(audit_path, context="test"),
    )
    dest = tmp_path / "app.log"
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        remote = _dialog(app).query_one("#transfer-remote", Input)
        remote.value = "/var/log/app.log"
        local = _dialog(app).query_one("#transfer-local", Input)
        local.value = str(dest)
        await pilot.press("enter")
        await until(pilot, lambda: dest.exists(), label="file downloaded")
        await until(
            pilot,
            lambda: any("downloaded" in str(n.message).lower() for n in app._notifications),
            label="success toast",
        )
    assert dest.read_bytes() == payload
    assert opener.calls == [
        {
            "namespace": "default",
            "pod": "api-1",
            "container": "app",
            "command": ["tar", "cf", "-", "-C", "/var/log", "app.log"],
            "stdin": False,
        }
    ]
    entries = audit_entries(audit_path)
    assert [e["outcome"] for e in entries] == ["intent", "success"]
    assert entries[0]["action"] == "transfer_download"
    assert entries[0]["name"] == "api-1"
    assert "/var/log/app.log" in entries[0]["detail"]
    assert f"bytes={len(payload)}" in entries[1]["detail"]


async def test_download_blocked_without_audit_log(tmp_path: Path) -> None:
    opener = FakeExecOpener([b"\x01" + tar_bytes("f", b"x"), b"\x03" + SUCCESS])
    app = make_app([_pod("api-1")], open_pod_exec=opener, audit=None)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).query_one("#transfer-remote", Input).value = "/f"
        _dialog(app).query_one("#transfer-local", Input).value = str(tmp_path / "f")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("audit" in str(n.message).lower() for n in app._notifications),
            label="blocked toast",
        )
    assert opener.calls == []


async def test_download_failure_notifies_and_audits_error(tmp_path: Path) -> None:
    failure = json.dumps(
        {"status": "Failure", "message": 'exec: "tar": executable file not found in $PATH'}
    ).encode()
    opener = FakeExecOpener([b"\x03" + failure])
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1")],
        open_pod_exec=opener,
        audit=AuditLog(audit_path, context="test"),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).query_one("#transfer-remote", Input).value = "/f"
        _dialog(app).query_one("#transfer-local", Input).value = str(tmp_path / "f")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("not found" in str(n.message) for n in app._notifications),
            label="error toast",
        )
    entries = audit_entries(audit_path)
    assert [e["outcome"] for e in entries[:1]] == ["intent"]
    assert entries[-1]["outcome"].startswith("error")


async def test_upload_requires_approval_then_transfers(tmp_path: Path) -> None:
    src = tmp_path / "dbg.sh"
    src.write_bytes(b"echo hi\n")
    opener = FakeExecOpener()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1")],
        open_pod_exec=opener,
        audit=AuditLog(audit_path, context="test"),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).select_upload()
        _dialog(app).query_one("#transfer-remote", Input).value = "/opt/dbg.sh"
        _dialog(app).query_one("#transfer-local", Input).value = str(src)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert opener.calls == []  # nothing sent before approval
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("uploaded" in str(n.message).lower() for n in app._notifications),
            label="success toast",
        )
    assert opener.calls[0]["command"] == ["tar", "xf", "-", "-C", "/opt"]
    assert opener.calls[0]["stdin"] is True
    assert opener.ws is not None
    payload = b"".join(frame[1:] for frame in opener.ws.sent)
    with tarfile.open(fileobj=io.BytesIO(payload)) as tf:
        assert tf.getmembers()[0].name == "dbg.sh"
    entries = audit_entries(audit_path)
    assert entries[0]["action"] == "transfer_upload"
    assert [e["outcome"] for e in entries] == ["intent", "success"]


async def test_upload_denied_approval_does_not_transfer(tmp_path: Path) -> None:
    src = tmp_path / "f"
    src.write_bytes(b"x")
    opener = FakeExecOpener()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1")],
        open_pod_exec=opener,
        audit=AuditLog(audit_path, context="test"),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).select_upload()
        _dialog(app).query_one("#transfer-remote", Input).value = "/tmp/f"
        _dialog(app).query_one("#transfer-local", Input).value = str(src)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await pilot.press("n")
        await until(pilot, lambda: not isinstance(app.screen, ConfirmScreen), label="closed")
    assert opener.calls == []
    assert audit_entries(audit_path) == []


async def test_upload_blocked_in_readonly_mode(tmp_path: Path) -> None:
    src = tmp_path / "f"
    src.write_bytes(b"x")
    opener = FakeExecOpener()
    app = make_app(
        [_pod("api-1")],
        open_pod_exec=opener,
        audit=AuditLog(tmp_path / "audit.jsonl", context="test"),
        config=KorvidConfig(namespace="default", readonly=True),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).select_upload()
        _dialog(app).query_one("#transfer-remote", Input).value = "/tmp/f"
        _dialog(app).query_one("#transfer-local", Input).value = str(src)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("read-only" in str(n.message) for n in app._notifications),
            label="readonly toast",
        )
        assert not isinstance(app.screen, ConfirmScreen)
    assert opener.calls == []


async def test_download_default_local_path_from_remote_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    payload = b"data"
    opener = FakeExecOpener([b"\x01" + tar_bytes("app.log", payload), b"\x03" + SUCCESS])
    app = make_app(
        [_pod("api-1")],
        open_pod_exec=opener,
        audit=AuditLog(tmp_path / "audit.jsonl", context="test"),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).query_one("#transfer-remote", Input).value = "/var/log/app.log"
        # local path left empty: defaults to ~/Downloads/<basename>
        await pilot.press("enter")
        dest = tmp_path / "Downloads" / "app.log"
        await until(pilot, lambda: dest.exists(), label="downloaded to default path")
    assert dest.read_bytes() == payload


async def test_progress_screen_escape_cancels_transfer(tmp_path: Path) -> None:
    # A stalled stream keeps the progress screen up; escape cancels the
    # worker and audits the aborted transfer.
    opener = FakeExecOpener([b"\x01" + b"partial"], stall=True)
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1")],
        open_pod_exec=opener,
        audit=AuditLog(audit_path, context="test"),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).query_one("#transfer-remote", Input).value = "/big.bin"
        _dialog(app).query_one("#transfer-local", Input).value = str(tmp_path / "big.bin")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, TransferProgressScreen), label="progress")
        await pilot.press("escape")
        await until(
            pilot,
            lambda: any("cancelled" in str(n.message).lower() for n in app._notifications),
            label="cancelled toast",
        )
        await until(
            pilot,
            lambda: not isinstance(app.screen, TransferProgressScreen),
            label="progress closed",
        )
    entries = audit_entries(audit_path)
    assert [e["outcome"] for e in entries] == ["intent", "cancelled"]
    assert not (tmp_path / "big.bin").exists()


async def test_multi_container_pod_shows_picker_first() -> None:
    opener = FakeExecOpener()
    app = make_app([_pod("api-1", containers=("app", "sidecar"))], open_pod_exec=opener)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        from korvid.ui.widgets.pick_screen import PickScreen

        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="picker")
        await pilot.press("enter")  # pick first container
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        assert "app" in str(_dialog(app).query_one(".transfer-title", Static).render())
