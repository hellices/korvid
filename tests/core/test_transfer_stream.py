"""Tests for core/transfer.py (stream half) — exec tar streaming over channel frames (issue #47)."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import json
import os
import tarfile
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from korvid.core.transfer import (
    RemoteEntry,
    TransferError,
    _await_thread,
    download,
    list_remote_dir,
    upload,
)

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


class TestPermissionErrors:
    """Issue #123: late permission failures are TransferErrors with the
    destination path and, remotely, an actionable hint — never a raw errno
    leaking the staging file name."""

    async def test_upload_permission_denied_appends_hint(self, tmp_path: Path) -> None:
        src = tmp_path / "f"
        src.write_bytes(b"x")
        failure = json.dumps(
            {"status": "Failure", "message": "command terminated with non-zero exit code"}
        ).encode()
        ws = FakeWs(
            [
                b"\x02tar: f: Cannot open: Permission denied\n",
                b"\x03" + failure,
            ]
        )
        with pytest.raises(TransferError, match="Permission denied") as excinfo:
            await upload(FakeExec(ws), src, "/app/f")
        message = str(excinfo.value)
        assert "hint:" in message
        assert "/app" in message
        assert "/tmp" in message

    async def test_upload_unrelated_failure_gets_no_hint(self, tmp_path: Path) -> None:
        src = tmp_path / "f"
        src.write_bytes(b"x")
        ws = FakeWs([b"\x03" + NOT_FOUND])
        with pytest.raises(TransferError, match="executable file not found") as excinfo:
            await upload(FakeExec(ws), src, "/tmp/f")
        assert "hint:" not in str(excinfo.value)

    @pytest.mark.skipif(
        os.name == "nt" or os.geteuid() == 0,
        reason="POSIX permission bits are not meaningful here (Windows or root)",
    )
    async def test_download_unwritable_directory_is_a_transfer_error(self, tmp_path: Path) -> None:
        # validate_spec catches this up front, but a directory can lose its
        # write bit between the dialog and the stream: the late failure must
        # still name the destination directory, never the staging file.
        restricted = tmp_path / "restricted"
        restricted.mkdir(mode=0o500)
        dest = restricted / "app.log"
        ws = FakeWs([b"\x01" + tar_bytes("app.log", b"data"), b"\x03" + SUCCESS])
        with pytest.raises(TransferError, match="not writable") as excinfo:
            await download(FakeExec(ws), "/var/log/app.log", dest)
        message = str(excinfo.value)
        assert str(restricted) in message
        assert ".part" not in message

    async def test_download_enospc_is_not_labelled_unwritable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mkstemp can fail for reasons other than permissions (ENOSPC, EMFILE,
        # ENAMETOOLONG); those must not be misreported as "not writable".
        def full_disk(**_kwargs: object) -> tuple[int, str]:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("korvid.core.transfer.tempfile.mkstemp", full_disk)
        dest = tmp_path / "app.log"
        ws = FakeWs([b"\x01" + tar_bytes("app.log", b"data"), b"\x03" + SUCCESS])
        with pytest.raises(TransferError, match="No space left on device") as excinfo:
            await download(FakeExec(ws), "/var/log/app.log", dest)
        message = str(excinfo.value)
        assert "not writable" not in message
        assert str(tmp_path) in message
        assert ".part" not in message

    async def test_upload_permission_denied_without_verdict_still_hints(
        self, tmp_path: Path
    ) -> None:
        # The server can emit the permission stderr and drop the connection
        # before any channel-3 verdict; the no-verdict failure path must
        # carry the same hint as the verdict path.
        src = tmp_path / "f"
        src.write_bytes(b"x")
        ws = FakeWs([b"\x02tar: f: Cannot open: Permission denied\n"])
        with pytest.raises(TransferError, match="Permission denied") as excinfo:
            await upload(FakeExec(ws), src, "/app/f")
        message = str(excinfo.value)
        assert "hint:" in message
        assert "/app" in message

    @pytest.mark.skipif(
        os.name == "nt" or os.geteuid() == 0,
        reason="POSIX permission bits are not meaningful here (Windows or root)",
    )
    async def test_directory_losing_write_bit_mid_stream_keeps_transfer_error(
        self, tmp_path: Path
    ) -> None:
        # The spool is created while the directory is writable; the write bit
        # flips before extraction. The extraction failure is normalized to a
        # TransferError — and the spool-cleanup failure in the finally block
        # must not replace it with a raw PermissionError naming the .part
        # staging file.
        locked = tmp_path / "d"
        locked.mkdir()
        dest = locked / "app.log"
        archive = tar_bytes("app.log", b"data")
        ws = FakeWs([b"\x01" + archive, b"\x03" + SUCCESS])

        def lock(_count: int) -> None:
            locked.chmod(0o500)

        try:
            with pytest.raises(TransferError, match="cannot write") as excinfo:
                await download(FakeExec(ws), "/var/log/app.log", dest, progress=lock)
            assert ".part" not in str(excinfo.value)
        finally:
            locked.chmod(0o700)


class TestListRemoteDir:
    """Issue #124: one exec round-trip listing a container directory."""

    async def test_lists_entries_dirs_first(self) -> None:
        listing = b"app.log\nconfig/\n.hidden\nlib/\n"
        ws = FakeWs([b"\x01" + listing, b"\x03" + SUCCESS])
        entries = await list_remote_dir(FakeExec(ws), "/srv")
        assert entries == [
            RemoteEntry("config", True),
            RemoteEntry("lib", True),
            RemoteEntry(".hidden", False),
            RemoteEntry("app.log", False),
        ]

    async def test_runs_ls_with_option_terminator(self) -> None:
        # `--` so a directory name starting with "-" is never read as an
        # option; -1Ap gives one name per line, hidden files, dir markers.
        # A trailing slash makes the operand directory-only: `ls file`
        # succeeds and echoes the operand, which force-open (`o`) would
        # otherwise render as a pseudo-directory containing itself.
        ws = FakeWs([b"\x01" + b"f\n", b"\x03" + SUCCESS])
        exec_ = FakeExec(ws)
        await list_remote_dir(exec_, "/srv")
        assert exec_.calls == [(["ls", "-1Ap", "--", "/srv/"], False)]

    async def test_root_listing_keeps_single_slash(self) -> None:
        ws = FakeWs([b"\x01" + b"f\n", b"\x03" + SUCCESS])
        exec_ = FakeExec(ws)
        await list_remote_dir(exec_, "/")
        assert exec_.calls == [(["ls", "-1Ap", "--", "/"], False)]

    async def test_non_utf8_names_reject_listing(self) -> None:
        # errors="replace" would collapse an invalid-UTF-8 name and a real
        # U+FFFD name to the same entry; selecting the former would then
        # transfer the latter's path. Exec arguments are strings, so such
        # names can never round-trip — degrade to manual entry instead.
        ws = FakeWs([b"\x01" + b"ok.log\n\xff\xfebad\n", b"\x03" + SUCCESS])
        with pytest.raises(TransferError, match="non-UTF-8"):
            await list_remote_dir(FakeExec(ws), "/srv")

    async def test_duplicate_names_reject_listing(self) -> None:
        # An embedded-LF name can alias a real sibling: "decoy\nlogs" plus a
        # real "logs/" parses into both a bare "logs" file entry and a
        # "logs" directory — picking the "file" would hand tar the real
        # directory. Real ls never emits duplicates, so an ambiguous listing
        # degrades to manual entry.
        ws = FakeWs([b"\x01" + b"decoy\nlogs\nlogs/\n", b"\x03" + SUCCESS])
        with pytest.raises(TransferError, match="ambiguous"):
            await list_remote_dir(FakeExec(ws), "/srv")

    async def test_empty_directory(self) -> None:
        ws = FakeWs([b"\x03" + SUCCESS])
        assert await list_remote_dir(FakeExec(ws), "/empty") == []

    async def test_failure_verdict_raises_transfer_error(self) -> None:
        failure = json.dumps(
            {"status": "Failure", "message": "command terminated with non-zero exit code"}
        ).encode()
        ws = FakeWs([b"\x02ls: /nope: No such file or directory\n", b"\x03" + failure])
        with pytest.raises(TransferError, match="No such file or directory"):
            await list_remote_dir(FakeExec(ws), "/nope")

    async def test_missing_ls_raises_transfer_error(self) -> None:
        # Distroless images have tar but often no ls: the caller degrades to
        # manual path entry, so the failure must be a typed TransferError.
        ws = FakeWs([b"\x03" + NOT_FOUND])
        with pytest.raises(TransferError, match="executable file not found"):
            await list_remote_dir(FakeExec(ws), "/srv")

    async def test_no_verdict_raises_transfer_error(self) -> None:
        # Connection dropped before channel 3: the listing may be truncated,
        # never present it as complete.
        ws = FakeWs([b"\x01" + b"partial\n"])
        with pytest.raises(TransferError, match="without reporting an outcome"):
            await list_remote_dir(FakeExec(ws), "/srv")

    async def test_symlink_marker_stripped_as_file(self) -> None:
        # -p suffixes only real directories; a dangling `@`-free symlink to a
        # dir shows bare. Names ending in "/" are dirs, everything else files.
        ws = FakeWs([b"\x01" + b"link\nreal/\n", b"\x03" + SUCCESS])
        entries = await list_remote_dir(FakeExec(ws), "/srv")
        assert entries == [RemoteEntry("real", True), RemoteEntry("link", False)]

    async def test_transport_error_normalized_to_transfer_error(self) -> None:
        # open_pod_exec propagates HTTP/connection failures from __aenter__;
        # the picker's degradation contract only catches TransferError, so
        # transport errors must be normalized here.
        class BrokenExec:
            def __call__(
                self, command: list[str], stdin: bool
            ) -> contextlib.AbstractAsyncContextManager[Any]:
                @contextlib.asynccontextmanager
                async def _cm() -> AsyncIterator[FakeWs]:
                    raise ConnectionError("HTTP 403: exec forbidden")
                    yield FakeWs([])  # pragma: no cover - unreachable

                return _cm()

        with pytest.raises(TransferError, match="exec forbidden"):
            await list_remote_dir(BrokenExec(), "/srv")

    async def test_cancellation_propagates(self) -> None:
        # Cancellation is not a listing failure: it must never be swallowed
        # into a TransferError.
        class HangingExec:
            def __call__(
                self, command: list[str], stdin: bool
            ) -> contextlib.AbstractAsyncContextManager[Any]:
                @contextlib.asynccontextmanager
                async def _cm() -> AsyncIterator[FakeWs]:
                    await asyncio.sleep(3600)
                    yield FakeWs([])  # pragma: no cover - unreachable

                return _cm()

        task = asyncio.create_task(list_remote_dir(HangingExec(), "/srv"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_control_characters_in_names_are_not_split(self) -> None:
        # The protocol separates records with LF only; splitlines() would
        # also split on VT/FF/U+0085 inside a valid filename, producing
        # phantom entries that cannot be selected.
        ws = FakeWs([b"\x01" + b"weird\x0bname\n", b"\x03" + SUCCESS])
        entries = await list_remote_dir(FakeExec(ws), "/srv")
        assert entries == [RemoteEntry("weird\x0bname", False)]

    async def test_oversized_listing_raises_transfer_error(self) -> None:
        # The listing is cluster-controlled: an enormous directory must not
        # accumulate unbounded stdout (and one UI option per entry) — past
        # the cap the read stops and the picker degrades to manual entry.
        chunk = b"\x01" + b"x" * (256 * 1024) + b"\n"
        frames: list[bytes | str] = [chunk] * 5
        frames.append(b"\x03" + SUCCESS)
        ws = FakeWs(frames)
        with pytest.raises(TransferError, match="too large"):
            await list_remote_dir(FakeExec(ws), "/srv")

    async def test_too_many_entries_raises_transfer_error(self) -> None:
        # The byte cap alone does not bound the picker: 1 MiB of short
        # names is hundreds of thousands of entries, each becoming a UI
        # option synchronously. Degrade before constructing them.
        listing = "".join(f"f{i}\n" for i in range(10_001)).encode()
        ws = FakeWs([b"\x01" + listing, b"\x03" + SUCCESS])
        with pytest.raises(TransferError, match="too many entries"):
            await list_remote_dir(FakeExec(ws), "/srv")
