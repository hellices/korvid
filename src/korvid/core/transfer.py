"""Pure helpers for pod file transfer (issue #47).

The transfer itself rides the exec API as a tar stream (see
``korvid.k8s.transfer``); everything here is side-effect-light plumbing that
can be unit-tested without a cluster: the tar argv builders, spec validation,
and local tar packing/extraction.
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import tarfile
import tempfile
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

#: Bytes copied per read while extracting a downloaded archive, and per
#: stdin frame while uploading.
_COPY_CHUNK = 64 * 1024

#: Exec channel numbers (v4.channel.k8s.io): every websocket frame is
#: prefixed with one byte naming the stream it belongs to.
_STDIN_CHANNEL = 0
_STDOUT_CHANNEL = 1
_STDERR_CHANNEL = 2
_ERROR_CHANNEL = 3

#: How long to keep draining server frames after the last stdin byte was
#: sent: long enough to catch an extraction error the remote tar reports
#: immediately, without stalling every successful upload.
_UPLOAD_DRAIN_GRACE = 0.5

#: Cap on buffered remote stderr; only the tail matters for diagnostics.
_STDERR_CAP = 4096

Direction = Literal["download", "upload"]

#: Opens an exec websocket for (command, stdin?) against a fixed pod —
#: bound by the caller (``KubeClient.open_pod_exec``), faked in tests.
OpenExec = Callable[[list[str], bool], AbstractAsyncContextManager[Any]]

#: Byte-count callback invoked as the transfer advances.
Progress = Callable[[int], None]


class TransferError(Exception):
    """A transfer failed; ``str(exc)`` is the user-facing message."""


@dataclass(frozen=True, slots=True)
class TransferSpec:
    """One requested transfer, as entered in the UI dialog.

    ``remote_path`` is always the file path inside the container;
    ``local_path`` is the file path on the machine running korvid
    (``~`` is expanded during validation/execution).
    """

    direction: Direction
    remote_path: str
    local_path: str


def validate_spec(spec: TransferSpec) -> str | None:
    """Return a user-facing error string, or None when the spec is runnable.

    Kept deliberately local-only: remote-side problems (missing file, missing
    tar binary, permissions) surface from the stream itself with the server's
    own message, which is always more accurate than a client-side guess.
    """
    remote = spec.remote_path.strip()
    if not remote:
        return "remote path is required"
    if not posixpath.isabs(remote):
        return "remote path must be absolute (e.g. /var/log/app.log)"
    if remote.endswith("/"):
        return "remote path must name a file, not a directory"
    if not spec.local_path.strip():
        return "local path is required"
    local = Path(spec.local_path).expanduser()
    if spec.direction == "upload":
        if not local.exists():
            return f"local file not found: {local}"
        if not local.is_file():
            return f"not a regular file: {local}"
        return None
    parent = local.parent
    if not parent.is_dir():
        return f"local directory does not exist: {parent}"
    return None


def download_command(remote_path: str) -> list[str]:
    """tar argv producing a single-file archive of ``remote_path`` on stdout.

    ``-C dir base`` keeps the archive member name to the basename so the
    local extraction never depends on the remote directory layout.
    """
    directory, base = posixpath.split(remote_path)
    return ["tar", "cf", "-", "-C", directory or "/", base]


def upload_command(remote_path: str) -> list[str]:
    """tar argv extracting an archive from stdin into ``remote_path``'s parent."""
    directory = posixpath.dirname(remote_path) or "/"
    return ["tar", "xf", "-", "-C", directory]


def default_local_path(remote_path: str) -> str:
    """Default download destination: ``~/Downloads/<basename>``."""
    base = posixpath.basename(remote_path.rstrip("/")) or "download"
    return str(Path("~/Downloads").expanduser() / base)


def pack_file(local_path: Path, arcname: str, archive_path: Path) -> int:
    """Write a single-member tar of ``local_path`` (named ``arcname``) to
    ``archive_path``; returns the packed file's size in bytes."""
    with tarfile.open(archive_path, "w") as tf:
        tf.add(str(local_path), arcname=arcname, recursive=False)
    return local_path.stat().st_size


def extract_single_file(archive_path: Path, dest: Path) -> int:
    """Extract the first regular-file member of ``archive_path`` into ``dest``.

    Member names are never used as filesystem paths — the bytes are streamed
    straight into the caller-chosen ``dest`` — so hostile names like
    ``../../evil`` in a compromised container's tar output are inert.

    Returns the number of bytes written; raises ValueError when the archive
    contains no regular file.
    """
    with tarfile.open(archive_path) as tf:
        for member in tf:
            if not member.isfile():
                continue
            src = tf.extractfile(member)
            if src is None:  # pragma: no cover - isfile() guarantees a stream
                continue
            written = 0
            with src, dest.open("wb") as out:
                while chunk := src.read(_COPY_CHUNK):
                    out.write(chunk)
                    written += len(chunk)
            return written
    raise ValueError("archive contains no file")


def _as_bytes(data: object) -> bytes:
    """Frame payloads arrive as BINARY (bytes) or TEXT (str) messages."""
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8", errors="replace")
    return b""


def _parse_error_channel(payload: bytes) -> str | None:
    """Return the failure message from a channel-3 status, None on success."""
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        status: dict[str, Any] = json.loads(text)
    except ValueError:
        return text
    if status.get("status") == "Success":
        return None
    message = status.get("message") or status.get("reason") or text
    return str(message)


class _FrameSink:
    """Accumulates stderr and the error-channel verdict across frames."""

    def __init__(self) -> None:
        self.stderr = b""
        self.failure: str | None = None
        self._error_seen = False

    def feed(self, data: object) -> bytes:
        """Consume one frame; returns any stdout payload it carried."""
        frame = _as_bytes(data)
        if len(frame) < 2:
            return b""
        channel, payload = frame[0], frame[1:]
        if channel == _STDOUT_CHANNEL:
            return payload
        if channel == _STDERR_CHANNEL:
            self.stderr = (self.stderr + payload)[-_STDERR_CAP:]
        elif channel == _ERROR_CHANNEL:
            self._error_seen = True
            self.failure = _parse_error_channel(payload)
        return b""

    def error_message(self, fallback: str) -> str:
        """User-facing failure text: server verdict first, stderr for detail."""
        parts = [self.failure or fallback]
        if self.stderr:
            parts.append(self.stderr.decode("utf-8", errors="replace").strip())
        return "\n".join(p for p in parts if p)


async def download(
    open_exec: OpenExec,
    remote_path: str,
    local_path: Path,
    progress: Progress | None = None,
) -> int:
    """Stream ``remote_path`` out of the container into ``local_path``.

    The remote side runs ``tar cf -`` (see ``download_command``); the archive
    is spooled to a temp file next to the destination and extracted only
    after the server reported success, so a failed transfer never leaves a
    corrupt half-written destination file. Returns the file's byte count.
    """
    sink = _FrameSink()
    total = 0
    fd, spool_name = tempfile.mkstemp(
        dir=local_path.parent, prefix=f".{local_path.name}.", suffix=".part"
    )
    spool_path = Path(spool_name)
    try:
        with os.fdopen(fd, "wb") as spool:
            async with open_exec(download_command(remote_path), False) as ws:
                async for msg in ws:
                    payload = sink.feed(msg.data)
                    if payload:
                        spool.write(payload)
                        total += len(payload)
                        if progress is not None:
                            progress(total)
        if sink.failure is not None:
            raise TransferError(sink.error_message("transfer failed"))
        if total == 0:
            raise TransferError(
                sink.error_message(f"no data received for {remote_path} — does the file exist?")
            )
        return await asyncio.to_thread(extract_single_file, spool_path, local_path)
    finally:
        spool_path.unlink(missing_ok=True)


async def upload(
    open_exec: OpenExec,
    local_path: Path,
    remote_path: str,
    progress: Progress | None = None,
) -> int:
    """Stream ``local_path`` into the container as ``remote_path``.

    The remote side runs ``tar xf - -C <parent>``; the local file is packed
    into a single-member archive (named after the remote basename) and sent
    over the stdin channel. The v4 exec protocol has no stdin half-close, so
    completion is signalled by closing the websocket after a short drain
    window — the same contract kubectl-cp-style tools rely on. Returns the
    file's byte count.
    """
    arcname = posixpath.basename(remote_path)
    with tempfile.NamedTemporaryFile(suffix=".tar") as archive:
        archive_path = Path(archive.name)
        size = await asyncio.to_thread(pack_file, local_path, arcname, archive_path)
        async with open_exec(upload_command(remote_path), True) as ws:
            sink = _FrameSink()

            async def _drain() -> None:
                async for msg in ws:
                    sink.feed(msg.data)

            reader = asyncio.create_task(_drain())
            try:
                try:
                    await _send_archive(ws, archive_path, size, progress)
                except OSError as exc:
                    # The connection usually drops because the remote command
                    # died; drain what the server managed to say, then prefer
                    # its verdict over the raw transport error.
                    await asyncio.wait({reader}, timeout=_UPLOAD_DRAIN_GRACE)
                    raise TransferError(sink.error_message(f"connection lost: {exc}")) from exc
                # The remote tar only reports late errors (disk full, bad
                # permissions) after receiving the data; give it a moment.
                await asyncio.wait({reader}, timeout=_UPLOAD_DRAIN_GRACE)
            finally:
                reader.cancel()
                # Cancellation must be observed before the websocket context
                # closes, or the reader dies mid-iteration with a warning.
                await asyncio.gather(reader, return_exceptions=True)
            if sink.failure is not None:
                raise TransferError(sink.error_message("upload failed"))
    return size


async def _send_archive(
    ws: Any,
    archive_path: Path,
    size: int,
    progress: Progress | None,
) -> None:
    """Send the archive as channel-0 frames; transport failures propagate as
    OSError for the caller to translate (after draining the server's side)."""
    sent = 0
    with archive_path.open("rb") as src:
        while chunk := src.read(_COPY_CHUNK):
            await ws.send_bytes(bytes([_STDIN_CHANNEL]) + chunk)
            sent += len(chunk)
            if progress is not None:
                progress(min(sent, size))
