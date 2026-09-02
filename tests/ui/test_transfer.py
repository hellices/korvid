"""Tests for pod file transfer UI — ctrl+t dialog, gates, audit (issue #47)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tarfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.transfer import TransferSpec
from korvid.k8s.models import PodSummary
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.transfer_screen import TransferProgressScreen, TransferScreen
from tests.ui.test_app import make_app
from tests.ui.waits import until

SUCCESS = json.dumps({"metadata": {}, "status": "Success"}).encode()


def _pod(name: str, containers: tuple[str, ...] = ("app",), uid: str = "") -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        qos="-",
        containers=containers,
        uid=uid,
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


def _raising_manifest(
    failure_factory: Callable[[], Exception],
) -> Callable[[str, str | None, str], Any]:
    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        del kind, ns, name
        raise failure_factory()

    return get_manifest


async def test_ctrl_t_requires_pods_kind() -> None:
    app = make_app([_pod("api-1")], open_pod_exec=FakeExecOpener())
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        app.current_kind = "deployments"
        # Off the pods view the binding is gated (issue #114): the key is
        # inert — no dialog, no warning.
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert not isinstance(app.screen, TransferScreen)
        assert not any("only available for pods" in str(n.message) for n in app._notifications)
        # A direct invocation (bypassing the key gate) still explains itself.
        app.action_transfer()
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
        await until(
            pilot,
            lambda: any(e.get("outcome") == "success" for e in audit_entries(audit_path)),
            label="success audit",
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


async def test_download_preserves_trailing_whitespace_in_paths(tmp_path: Path) -> None:
    # Picker-selected paths must round-trip verbatim: if both "report" and
    # "report " exist, stripping the field would silently transfer the
    # former while the user selected the latter.
    payload = b"bytes"
    opener = FakeExecOpener([b"\x01" + tar_bytes("report ", payload), b"\x03" + SUCCESS])
    app = make_app(
        [_pod("api-1")],
        open_pod_exec=opener,
        audit=AuditLog(tmp_path / "audit.jsonl", context="test"),
    )
    dest = tmp_path / "report "
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).query_one("#transfer-remote", Input).value = "/srv/report "
        _dialog(app).query_one("#transfer-local", Input).value = str(dest)
        await pilot.press("enter")
        await until(pilot, lambda: dest.exists(), label="file downloaded")
    assert dest.read_bytes() == payload
    assert opener.calls[0]["command"] == ["tar", "cf", "-", "-C", "/srv", "report "]


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


class _ExplodingAudit(AuditLog):
    """AuditLog whose persistence always fails."""

    def append(self, **kwargs: Any) -> None:
        raise OSError("disk full")


async def test_download_blocked_when_audit_append_fails(tmp_path: Path) -> None:
    # Fail-closed invariant: a configured audit log that cannot persist the
    # intent must block the transfer before any exec session is opened.
    opener = FakeExecOpener([b"\x01" + tar_bytes("f", b"x"), b"\x03" + SUCCESS])
    audit = _ExplodingAudit(tmp_path / "audit.jsonl", context="test")
    app = make_app([_pod("api-1")], open_pod_exec=opener, audit=audit)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).query_one("#transfer-remote", Input).value = "/f"
        _dialog(app).query_one("#transfer-local", Input).value = str(tmp_path / "f")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("audit log unavailable" in str(n.message) for n in app._notifications),
            label="blocked toast",
        )
    assert opener.calls == []
    assert not (tmp_path / "f").exists()


async def test_second_transfer_refused_while_one_in_flight(tmp_path: Path) -> None:
    # `TransferController.task` is a single slot, so a second worker must be refused
    # for the whole lifecycle of the first (launch through outcome audit) —
    # otherwise escape could cancel the wrong stream.
    opener = FakeExecOpener([b"\x01" + b"partial"], stall=True)
    app = make_app(
        [_pod("api-1")],
        open_pod_exec=opener,
        audit=AuditLog(tmp_path / "audit.jsonl", context="test"),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).query_one("#transfer-remote", Input).value = "/big.bin"
        _dialog(app).query_one("#transfer-local", Input).value = str(tmp_path / "big.bin")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, TransferProgressScreen), label="progress")
        # The keybinding path refuses immediately…
        app.action_transfer()
        # …and so does a worker launched directly into the pre-modal window.
        spec = TransferSpec("download", "/other.bin", str(tmp_path / "other.bin"))
        await app._transfer.run("default", "api-1", "app", spec, None)
        await until(
            pilot,
            lambda: sum("already in progress" in str(n.message) for n in app._notifications) == 2,
            label="refusal toasts",
        )
        await pilot.press("escape")  # release the stalled stream
    assert len(opener.calls) == 1


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
        await until(
            pilot,
            lambda: any(
                e.get("outcome", "").startswith("error") for e in audit_entries(audit_path)
            ),
            label="error audit",
        )
    entries = audit_entries(audit_path)
    assert [e["outcome"] for e in entries[:1]] == ["intent"]
    assert entries[-1]["outcome"].startswith("error")


async def test_upload_requires_approval_then_transfers(tmp_path: Path) -> None:
    src = tmp_path / "dbg.sh"
    src.write_bytes(b"echo hi\n")
    opener = FakeExecOpener([b"\x03" + SUCCESS])
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
        await until(
            pilot,
            lambda: any(e.get("outcome") == "success" for e in audit_entries(audit_path)),
            label="success audit",
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


async def test_upload_blocked_when_pod_replaced_after_approval(tmp_path: Path) -> None:
    """TOCTOU guard: approval binds to the pod *incarnation* (uid), and the uid
    is re-verified just before the exec — a same-named replacement created
    while the dialogs were open must never receive the bytes."""
    src = tmp_path / "f"
    src.write_bytes(b"x")
    opener = FakeExecOpener()
    audit_path = tmp_path / "audit.jsonl"

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return {"metadata": {"uid": "uid-replacement"}}

    app = make_app(
        [_pod("api-1", uid="uid-approved")],
        open_pod_exec=opener,
        audit=AuditLog(audit_path, context="test"),
        get_manifest=get_manifest,
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
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("replaced" in str(n.message) for n in app._notifications),
            label="replaced toast",
        )
    assert opener.calls == []
    assert audit_entries(audit_path) == []


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize(
    "failure_factory",
    [
        pytest.param(TimeoutError, id="timeout"),
        pytest.param(lambda: RuntimeError("api unavailable"), id="runtime-error"),
    ],
)
async def test_transfer_blocked_when_final_uid_lookup_unavailable(
    tmp_path: Path,
    direction: str,
    failure_factory: Callable[[], Exception],
) -> None:
    opener = FakeExecOpener()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1", uid="uid-approved")],
        open_pod_exec=opener,
        audit=AuditLog(audit_path, context="test"),
        get_manifest=_raising_manifest(failure_factory),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        if direction == "upload":
            source = tmp_path / "source"
            source.write_bytes(b"x")
            _dialog(app).select_upload()
            _dialog(app).query_one("#transfer-local", Input).value = str(source)
            _dialog(app).query_one("#transfer-remote", Input).value = "/tmp/source"
        else:
            _dialog(app).query_one("#transfer-remote", Input).value = "/tmp/source"
            _dialog(app).query_one("#transfer-local", Input).value = str(tmp_path / "source")
        await pilot.press("enter")
        if direction == "upload":
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
            await pilot.press("y")
        await until(
            pilot,
            lambda: any("could not be verified" in str(n.message) for n in app._notifications),
            label="retryable verification warning",
        )

    assert opener.calls == []
    assert audit_entries(audit_path) == []
    messages = [str(notification.message) for notification in app._notifications]
    assert any("Retry" in message for message in messages)
    assert all(
        "no longer exists" not in message and "was replaced" not in message for message in messages
    )


async def test_download_blocked_when_pod_replaced(tmp_path: Path) -> None:
    opener = FakeExecOpener()
    audit_path = tmp_path / "audit.jsonl"

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return {"metadata": {"uid": "uid-replacement"}}

    app = make_app(
        [_pod("api-1", uid="uid-approved")],
        open_pod_exec=opener,
        audit=AuditLog(audit_path, context="test"),
        get_manifest=get_manifest,
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        _dialog(app).query_one("#transfer-remote", Input).value = "/var/log/app.log"
        _dialog(app).query_one("#transfer-local", Input).value = str(tmp_path / "app.log")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("replaced" in str(n.message) for n in app._notifications),
            label="replaced toast",
        )
    assert opener.calls == []
    assert audit_entries(audit_path) == []


async def test_upload_proceeds_when_uid_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "f"
    src.write_bytes(b"x")
    opener = FakeExecOpener([b"\x03" + SUCCESS])

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return {"metadata": {"uid": "uid-approved"}}

    app = make_app(
        [_pod("api-1", uid="uid-approved")],
        open_pod_exec=opener,
        audit=AuditLog(tmp_path / "audit.jsonl", context="test"),
        get_manifest=get_manifest,
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
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("uploaded" in str(n.message).lower() for n in app._notifications),
            label="success toast",
        )
    assert len(opener.calls) == 1


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
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
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
        await until(
            pilot,
            lambda: any(e.get("outcome") == "cancelled" for e in audit_entries(audit_path)),
            label="cancelled audit",
        )
    entries = audit_entries(audit_path)
    assert [e["outcome"] for e in entries] == ["intent", "cancelled"]
    # Partial transfers stay auditable: the outcome records what was moved.
    assert "bytes=7" in entries[1]["detail"]
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
