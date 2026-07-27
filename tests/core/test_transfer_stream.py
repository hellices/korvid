"""Tests for core/transfer.py (stream half) — exec tar streaming over channel frames (issue #47)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tarfile
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from korvid.core.transfer import TransferError, _await_thread, download, upload

SUCCESS = json.dumps({"metadata": {}, "status": "Success"}).encode()
NOT_FOUND = json.dumps(
    {
        "metadata": {},
        "status": "Failure",
        "message": 'exec: "tar": executable file not found in $PATH',
        "reason": "InternalError",
    }
).encode()


def tar_bytes(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class FakeMsg:
    def __init__(self, data: bytes | str) -> None:
        self.data = data


class FakeWs:
    """Duck-typed aiohttp websocket: iterate frames, record sent bytes."""

    def __init__(self, frames: list[bytes | str], *, fail_send: bool = False) -> None:
        self._frames = list(frames)
        self.sent: list[bytes] = []
        self.closed = False
        self._fail_send = fail_send

    def __aiter__(self) -> FakeWs:
        return self

    async def __anext__(self) -> FakeMsg:
        if not self._frames:
            raise StopAsyncIteration
        return FakeMsg(self._frames.pop(0))

    async def send_bytes(self, data: bytes) -> None:
        if self._fail_send:
            raise ConnectionResetError("Cannot write to closing transport")
        self.sent.append(data)


class FakeExec:
    def __init__(self, ws: FakeWs) -> None:
        self.ws = ws
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(
        self, command: list[str], stdin: bool
    ) -> contextlib.AbstractAsyncContextManager[Any]:
        self.calls.append((command, stdin))

        @contextlib.asynccontextmanager
        async def _cm() -> AsyncIterator[FakeWs]:
            yield self.ws

        return _cm()


class TestDownload:
    async def test_streams_stdout_to_local_file(self, tmp_path: Path) -> None:
        archive = tar_bytes("app.log", b"line1\nline2\n")
        mid = len(archive) // 2
        ws = FakeWs(
            [
                b"\x01" + archive[:mid],
                b"\x01" + archive[mid:],
                b"\x03" + SUCCESS,
            ]
        )
        open_exec = FakeExec(ws)
        dest = tmp_path / "app.log"
        written = await download(open_exec, "/var/log/app.log", dest)
        assert written == len(b"line1\nline2\n")
        assert dest.read_bytes() == b"line1\nline2\n"
        assert open_exec.calls == [(["tar", "cf", "-", "-C", "/var/log", "app.log"], False)]

    async def test_reports_progress(self, tmp_path: Path) -> None:
        archive = tar_bytes("f", b"x" * 100)
        ws = FakeWs([b"\x01" + archive, b"\x03" + SUCCESS])
        seen: list[int] = []
        await download(FakeExec(ws), "/f", tmp_path / "f", progress=seen.append)
        assert seen == [len(archive)]

    async def test_error_channel_failure_raises(self, tmp_path: Path) -> None:
        ws = FakeWs([b"\x03" + NOT_FOUND])
        with pytest.raises(TransferError, match="executable file not found"):
            await download(FakeExec(ws), "/f", tmp_path / "f")

    async def test_stderr_included_in_failure(self, tmp_path: Path) -> None:
        failure = json.dumps(
            {"status": "Failure", "message": "command terminated with non-zero exit code"}
        ).encode()
        ws = FakeWs(
            [
                b"\x02" + b"tar: /nope: No such file or directory\n",
                b"\x03" + failure,
            ]
        )
        with pytest.raises(TransferError, match="No such file"):
            await download(FakeExec(ws), "/nope", tmp_path / "f")

    async def test_no_data_raises(self, tmp_path: Path) -> None:
        ws = FakeWs([b"\x03" + SUCCESS])
        with pytest.raises(TransferError, match="no data"):
            await download(FakeExec(ws), "/f", tmp_path / "f")

    async def test_text_frames_are_handled(self, tmp_path: Path) -> None:
        # aiohttp may deliver the error channel as a TEXT frame (str data).
        archive = tar_bytes("f", b"ok")
        ws = FakeWs([b"\x01" + archive, "\x03" + SUCCESS.decode()])
        assert await download(FakeExec(ws), "/f", tmp_path / "f") == 2

    async def test_leaves_no_temp_files(self, tmp_path: Path) -> None:
        archive = tar_bytes("f", b"ok")
        ws = FakeWs([b"\x01" + archive, b"\x03" + SUCCESS])
        dest = tmp_path / "f"
        await download(FakeExec(ws), "/f", dest)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]

    async def test_temp_file_removed_on_failure(self, tmp_path: Path) -> None:
        ws = FakeWs([b"\x01" + b"partial", b"\x03" + NOT_FOUND])
        with pytest.raises(TransferError, match="executable file not found"):
            await download(FakeExec(ws), "/f", tmp_path / "f")
        assert list(tmp_path.iterdir()) == []

    async def test_eof_without_verdict_raises(self, tmp_path: Path) -> None:
        # A complete-looking archive without a channel-3 status means the
        # exec outcome is unknown (e.g. a proxy dropped the connection);
        # never extract or report success on it.
        archive = tar_bytes("app.log", b"data")
        ws = FakeWs([b"\x01" + archive])  # no status frame
        dest = tmp_path / "app.log"
        with pytest.raises(TransferError, match="without reporting an outcome"):
            await download(FakeExec(ws), "/var/log/app.log", dest)
        assert not dest.exists()


class TestUpload:
    async def test_sends_tar_on_stdin_channel(self, tmp_path: Path) -> None:
        src = tmp_path / "dbg.sh"
        src.write_bytes(b"echo hi\n")
        ws = FakeWs([b"\x03" + SUCCESS])
        open_exec = FakeExec(ws)
        sent_bytes = await upload(open_exec, src, "/opt/tools/dbg.sh")
        assert sent_bytes == len(b"echo hi\n")
        assert open_exec.calls == [(["tar", "xf", "-", "-C", "/opt/tools"], True)]
        assert all(frame[:1] == b"\x00" for frame in ws.sent)
        payload = b"".join(frame[1:] for frame in ws.sent)
        with tarfile.open(fileobj=io.BytesIO(payload)) as tf:
            member = tf.getmembers()[0]
            assert member.name == "dbg.sh"
            extracted = tf.extractfile(member)
            assert extracted is not None
            assert extracted.read() == b"echo hi\n"

    async def test_spool_archive_removed_even_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The spool must be created with a *closed* handle (pack_file reopens
        # it by name — an open NamedTemporaryFile forbids that on Windows)
        # and unlinked afterwards, success or not.
        spool_dir = tmp_path / "spool"
        spool_dir.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(spool_dir))
        src = tmp_path / "f"
        src.write_bytes(b"x")
        await upload(FakeExec(FakeWs([b"\x03" + SUCCESS])), src, "/tmp/f")
        assert list(spool_dir.iterdir()) == []
        with pytest.raises(TransferError, match="executable file not found"):
            await upload(FakeExec(FakeWs([b"\x03" + NOT_FOUND])), src, "/tmp/f")
        assert list(spool_dir.iterdir()) == []

    async def test_error_channel_failure_raises(self, tmp_path: Path) -> None:
        src = tmp_path / "f"
        src.write_bytes(b"x")
        ws = FakeWs([b"\x03" + NOT_FOUND])
        with pytest.raises(TransferError, match="executable file not found"):
            await upload(FakeExec(ws), src, "/tmp/f")

    async def test_send_failure_surfaces_remote_error(self, tmp_path: Path) -> None:
        # The connection drops mid-send because the remote command died; the
        # error the server reported must win over the raw transport error.
        src = tmp_path / "f"
        src.write_bytes(b"x" * 10)
        failure = json.dumps({"status": "Failure", "message": "tar: /missing: not found"}).encode()
        ws = FakeWs([b"\x02tar: /missing: not found\n", b"\x03" + failure], fail_send=True)
        with pytest.raises(TransferError, match="/missing"):
            await upload(FakeExec(ws), src, "/missing/f")

    async def test_reports_progress(self, tmp_path: Path) -> None:
        src = tmp_path / "f"
        src.write_bytes(b"y" * 100)
        seen: list[int] = []
        await upload(FakeExec(FakeWs([b"\x03" + SUCCESS])), src, "/tmp/f", progress=seen.append)
        assert seen
        assert seen == sorted(seen)

    async def test_eof_without_verdict_raises(self, tmp_path: Path) -> None:
        # The websocket closing without a channel-3 status leaves the exec
        # outcome unknown; report failure rather than a blind success.
        src = tmp_path / "f"
        src.write_bytes(b"x")
        with pytest.raises(TransferError, match="without reporting an outcome"):
            await upload(FakeExec(FakeWs([])), src, "/tmp/f")


class TestAwaitThread:
    async def test_cancellation_waits_for_the_thread(self) -> None:
        # A worker thread cannot be interrupted: cancelling the awaiting task
        # must not return until the thread has finished, or the caller's
        # cleanup (unlinking the tar the thread has open) would race it.
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def work() -> None:
            started.set()
            release.wait(timeout=5)
            finished.set()

        task = asyncio.ensure_future(_await_thread(work))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "cancellation must be deferred until the thread ends"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
