"""Unit tests for TransferController (issue #91 U3a, extended by Deep Task 9).

The controller owns the whole ctrl+t journey: the selection and its
guards, the container pick, the transfer dialog with its remote-path
listing, the upload approval, and then the execution lifecycle —
serialization, uid re-verification, fail-closed intent audit, stream with
progress, outcome audit. None of it needs the app: the Textual surface,
the view and the write perimeter arrive as injected interfaces, and the
perimeter here is the real `WriteCoordinator`.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from korvid.core.audit import AuditLog
from korvid.core.transfer import RemoteEntry, TransferError, TransferSpec
from korvid.k8s.errors import ApiStatusError
from korvid.ui.transfer import TransferController, TransferScreens
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.transfer_screen import TransferScreen

from .test_write_coordinator import FakeView, make_env

SUCCESS = json.dumps({"metadata": {}, "status": "Success"}).encode()


class FakeScreens(TransferScreens):
    """Records the progress modals the lifecycle pops."""

    def __init__(self) -> None:
        self.dismissed: list[Any] = []

    def dismiss_if_current(self, screen: Any) -> None:
        self.dismissed.append(screen)


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
        self.env = make_env(tmp_path, audit="none")
        self.ui = self.env.ui
        self.screens = FakeScreens()
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
            ui=self.ui,
            view=self.env.view,
            writes=self.env.coordinator,
            screens=self.screens,
            open_pod_exec=lambda: self._opener,  # type: ignore[arg-type,return-value]  # duck-typed fake
            audit=lambda: self._audit,
            pod_containers=lambda ns, name: ("app",),
            target_uid=self._target_uid,
            pod_uid_unchanged=self._uid_unchanged,
        )

    @property
    def notifications(self) -> list[tuple[str, str]]:
        return self.ui.notifications

    @property
    def progress_screens(self) -> list[Any]:
        return [screen for screen, _callback in self.ui.screens]

    @property
    def closed(self) -> list[Any]:
        return self.screens.dismissed

    async def _target_uid(self, kind: str, namespace: str | None, name: str) -> str | None:
        return "uid-1"

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
            self.ui.notify(f"{action} cancelled - pod {name} was replaced", severity="warning")
        return self._uid_ok


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


# ---------------------------------------------------------------------------
# The user-facing half (Deep Task 9): selection, dialogs, listing, approval
# ---------------------------------------------------------------------------


class FlowHarness:
    """The controller over a real `WriteCoordinator` and fake surfaces.

    The perimeter is real so "an upload cannot reach the cluster without the
    approval dialog and the reservation" is observed, not asserted about a
    double.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        kind: str = "pods",
        selected: tuple[str | None, str | None] = ("default", "api-1"),
        containers: tuple[str, ...] = ("app",),
        uid: str | None = "uid-1",
        readonly: bool = False,
        opener: object = "default",
        current_uid: str | None = "uid-1",
        uid_error: BaseException | None = None,
        entries: list[RemoteEntry] | None = None,
    ) -> None:
        self.env = make_env(
            tmp_path,
            view=FakeView(kind=kind, selected=selected, uid=uid, readonly=readonly),
        )
        self.ui = self.env.ui
        self.screens = FakeScreens()
        self.containers = containers
        self.current_uid = current_uid
        self.uid_error = uid_error
        self.uid_checks: list[tuple[str, str, str]] = []
        self.exec_calls: list[tuple[str, str, str | None]] = []
        self.listed: list[str] = []
        self._entries = entries if entries is not None else []
        self.ran: list[tuple[str, str, str | None, TransferSpec, str | None]] = []
        if opener == "default":
            opener = self._open_exec
        self.opener = opener
        self.controller = TransferController(
            ui=self.ui,
            view=self.env.view,
            writes=self.env.coordinator,
            screens=self.screens,
            open_pod_exec=lambda: self.opener,  # type: ignore[arg-type,return-value]  # duck-typed fake
            audit=lambda: self.env.audit,
            pod_containers=lambda ns, name: self.containers,
            target_uid=self._target_uid,
            pod_uid_unchanged=self._uid_unchanged,
        )
        self.controller.run = self._record_run  # type: ignore[method-assign]

    def _open_exec(
        self, namespace: str, name: str, container: str | None, command: list[str], *, stdin: bool
    ) -> contextlib.AbstractAsyncContextManager[Any]:
        self.exec_calls.append((namespace, name, container))

        @contextlib.asynccontextmanager
        async def _cm() -> Any:
            yield FakeExecSession([])

        return _cm()

    async def _target_uid(self, kind: str, namespace: str | None, name: str) -> str | None:
        if self.uid_error is not None:
            raise self.uid_error
        return self.current_uid

    async def _uid_unchanged(self, namespace: str, name: str, uid: str, *, action: str) -> bool:
        self.uid_checks.append((namespace, name, uid))
        return True

    async def _record_run(
        self,
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        uid: str | None,
    ) -> None:
        self.ran.append((namespace, name, container, spec, uid))

    def screen(self) -> Any:
        return self.ui.screens[-1][0]

    def answer(self, result: Any) -> None:
        _screen, callback = self.ui.screens[-1]
        assert callback is not None
        callback(result)


async def test_start_rejects_a_non_pods_view(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path, kind="nodes")
    h.controller.start()
    assert h.ui.screens == []
    assert "File transfer is only available for pods" in h.ui.messages()


async def test_start_reports_a_missing_exec_client(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path, opener=None)
    h.controller.start()
    assert h.ui.screens == []
    assert any("no cluster connection" in message for message in h.ui.messages())


async def test_start_refuses_while_a_transfer_is_in_flight(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path)
    h.controller._in_flight = True
    h.controller.start()
    assert h.ui.screens == []
    assert "A transfer is already in progress" in h.ui.messages()


async def test_start_refuses_during_a_context_switch(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path)
    h.env.context.reads = False
    h.controller.start()
    assert h.ui.screens == []


async def test_start_without_a_selection_opens_nothing(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path, selected=(None, None))
    h.controller.start()
    assert h.ui.screens == []


async def test_start_opens_the_transfer_dialog_for_a_single_container(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path)
    h.controller.start()
    screen = h.screen()
    assert isinstance(screen, TransferScreen)
    assert screen._target == "default/api-1 (app)"


async def test_start_picks_the_container_first_when_the_pod_has_several(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path, containers=("app", "sidecar"))
    h.controller.start()
    assert isinstance(h.screen(), PickScreen)
    h.answer("sidecar")
    assert isinstance(h.screen(), TransferScreen)
    assert h.screen()._target == "default/api-1 (sidecar)"


async def test_a_dismissed_container_pick_opens_no_dialog(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path, containers=("app", "sidecar"))
    h.controller.start()
    h.answer(None)
    assert len(h.ui.screens) == 1


async def test_download_runs_without_an_approval_dialog(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path)
    h.controller.start_transfer(
        "default", "api-1", "app", _spec(tmp_path), "uid-1", h.env.context.epoch()
    )
    await h.ui.settle()
    assert h.ran == [("default", "api-1", "app", _spec(tmp_path), "uid-1")]
    assert h.ui.screens == []


async def test_upload_is_blocked_in_read_only_mode(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path, readonly=True)
    h.controller.start_transfer(
        "default", "api-1", "app", _spec(tmp_path, "upload"), "uid-1", h.env.context.epoch()
    )
    await h.ui.settle()
    assert h.ran == []
    assert "Upload disabled in read-only mode" in h.ui.messages()


async def test_upload_passes_the_approval_gate(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path)
    spec = _spec(tmp_path, "upload")
    h.controller.start_transfer("default", "api-1", "app", spec, "uid-1", h.env.context.epoch())
    assert isinstance(h.screen(), ConfirmScreen)
    assert h.ran == []
    h.answer(True)
    await h.ui.settle()
    assert h.ran == [("default", "api-1", "app", spec, "uid-1")]


async def test_declined_upload_never_runs(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path)
    h.controller.start_transfer(
        "default", "api-1", "app", _spec(tmp_path, "upload"), "uid-1", h.env.context.epoch()
    )
    h.answer(False)
    await h.ui.settle()
    assert h.ran == []


async def test_start_transfer_is_cancelled_by_a_context_switch(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path)
    stale = h.env.context.epoch()
    h.env.context.value += 1
    h.controller.start_transfer("default", "api-1", "app", _spec(tmp_path), "uid-1", stale)
    await h.ui.settle()
    assert h.ran == []
    assert any("the kube context" in message for message in h.ui.messages())


async def test_an_approval_left_open_across_a_switch_is_refused(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path)
    epoch = h.env.context.epoch()
    h.controller.start_transfer(
        "default", "api-1", "app", _spec(tmp_path, "upload"), "uid-1", epoch
    )
    h.env.context.value += 1  # the switch completed while the dialog was open
    h.answer(True)
    await h.ui.settle()
    assert h.ran == []
    assert any("the kube context" in message for message in h.ui.messages())


async def test_remote_lister_is_unavailable_without_an_exec_client(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path, opener=None)
    assert h.controller.remote_lister("default", "api-1", "app", uid=None, epoch=0) is None


async def test_remote_lister_lists_over_the_exec_api(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path)
    lister = h.controller.remote_lister(
        "default", "api-1", "app", uid=None, epoch=h.env.context.epoch()
    )
    assert lister is not None
    with patch(
        "korvid.ui.transfer.list_remote_dir",
        new=_fake_list_remote_dir([RemoteEntry("etc", True)]),
    ):
        assert await lister("/") == [RemoteEntry("etc", True)]


async def test_remote_lister_refuses_after_a_context_switch(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path)
    lister = h.controller.remote_lister(
        "default", "api-1", "app", uid=None, epoch=h.env.context.epoch()
    )
    assert lister is not None
    h.env.context.value += 1
    with pytest.raises(TransferError, match="the kube context changed"):
        await lister("/")


async def test_remote_lister_refuses_a_replaced_pod(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path, current_uid="uid-2")
    lister = h.controller.remote_lister(
        "default", "api-1", "app", uid="uid-1", epoch=h.env.context.epoch()
    )
    assert lister is not None
    with pytest.raises(TransferError, match="was replaced"):
        await lister("/")


async def test_remote_lister_refuses_an_unverifiable_pod(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path, current_uid=None)
    lister = h.controller.remote_lister(
        "default", "api-1", "app", uid="uid-1", epoch=h.env.context.epoch()
    )
    assert lister is not None
    with pytest.raises(TransferError, match="could not be verified"):
        await lister("/")


async def test_remote_lister_reports_a_vanished_pod(tmp_path: Path) -> None:
    h = FlowHarness(tmp_path, uid_error=ApiStatusError(404, "NotFound", "gone"))
    lister = h.controller.remote_lister(
        "default", "api-1", "app", uid="uid-1", epoch=h.env.context.epoch()
    )
    assert lister is not None
    with pytest.raises(TransferError, match="no longer exists"):
        await lister("/")


def _fake_list_remote_dir(entries: list[RemoteEntry]) -> Any:
    async def _list(open_exec: Any, path: str) -> list[RemoteEntry]:
        async with open_exec(["ls"], False):
            pass
        return entries

    return _list
