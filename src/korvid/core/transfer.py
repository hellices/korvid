"""Pure helpers for pod file transfer (issue #47).

The transfer itself rides the exec API as a tar stream — the websocket
session is opened by `KubeClient.open_pod_exec` in `korvid.k8s.client` and
injected here as an `OpenExec` callable. Everything else in this module is
side-effect-light plumbing that can be unit-tested without a cluster: the
tar argv builders, spec validation, and local tar packing/extraction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import tarfile
import tempfile
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypeVar

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

#: Bytes copied per read while extracting a downloaded archive, and per
#: stdin frame while uploading.
_COPY_CHUNK = 64 * 1024

#: Exec channel numbers (v4.channel.k8s.io): every websocket frame is
#: prefixed with one byte naming the stream it belongs to.
_STDIN_CHANNEL = 0
_STDOUT_CHANNEL = 1
_STDERR_CHANNEL = 2
_ERROR_CHANNEL = 3

#: How long to keep draining server frames after a mid-send transport
#: failure: long enough to catch the error the remote tar reported (its
#: verdict beats the raw transport error), without stalling the failure path.
_UPLOAD_DRAIN_GRACE = 0.5

#: How long to wait for the channel-3 status after the whole archive was
#: sent. The remote tar exits on the archive's end-of-archive marker (no
#: stdin EOF needed), so the verdict normally arrives promptly; a tar that
#: never reports leaves the outcome unknown and the upload is failed.
_UPLOAD_VERDICT_TIMEOUT = 10.0

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
    remote_error = _validate_remote_path(spec.remote_path.strip())
    if remote_error is not None:
        return remote_error
    if not spec.local_path.strip():
        return "local path is required"
    try:
        local = Path(spec.local_path).expanduser()
    except RuntimeError:
        # e.g. "~no_such_user/f": expansion failure must surface as a
        # validation toast, not an exception out of the dialog handler.
        return f"cannot expand local path: {spec.local_path}"
    if spec.direction == "upload":
        if not local.exists():
            return f"local file not found: {local}"
        if not local.is_file():
            return f"not a regular file: {local}"
        if not os.access(local, os.R_OK):
            return f"local file is not readable: {local}"
        return None
    return _validate_download_destination(local)


def _validate_download_destination(local: Path) -> str | None:
    """Local-side checks for a download destination path."""
    parent = local.parent
    if not parent.is_dir():
        return f"local directory does not exist: {parent}"
    # Writability is checked here too (issue #123): failing after the dialog
    # would leave an intent audit entry and a raw errno for a foreseeable
    # condition. Creating the staging file needs both the write and the
    # search bit — and the search bit must be checked *before* any stat of
    # ``local`` below, which would itself raise PermissionError without it.
    # The stream re-checks — the bits can flip in between.
    if not os.access(parent, os.W_OK | os.X_OK):
        return f"local directory is not writable: {parent}"
    if local.is_dir():
        # Caught here: after the stream this would only surface as an
        # IsADirectoryError from the extraction step, long after the bytes
        # were transferred.
        return f"local path is a directory, expected a file path: {local}"
    if local.exists() and not os.access(local, os.W_OK):
        return f"local file is not writable: {local}"
    return None


def _validate_remote_path(remote: str) -> str | None:
    if not remote:
        return "remote path is required"
    if not posixpath.isabs(remote):
        return "remote path must be absolute (e.g. /var/log/app.log)"
    if remote.endswith("/"):
        return "remote path must name a file, not a directory"
    if posixpath.basename(remote) in (".", ".."):
        # "/tmp/." would hand tar the whole directory (recursive transfer is
        # out of scope); "/tmp/.." an even larger parent tree.
        return "remote path must name a file, not a directory"
    return None


class RemoteEntry(NamedTuple):
    """One name in a remote directory listing (issue #124)."""

    name: str
    is_dir: bool


def list_dir_command(path: str) -> list[str]:
    """ls argv listing ``path`` one name per line with directory markers.

    `-1Ap`: one entry per line, hidden files without `.`/`..`, a trailing
    slash on directories. `--` keeps a leading-dash path from being read as
    an option. The operand's own trailing slash makes it directory-only:
    `ls file` succeeds and echoes the operand, which force-open (`o`) would
    otherwise render as a pseudo-directory containing itself; a symlink to
    a directory is still traversed.
    """
    return ["ls", "-1Ap", "--", path.rstrip("/") + "/"]


# The listing is cluster-controlled input: cap accumulation so a huge (or
# adversarial) directory cannot exhaust TUI memory — 1 MiB is thousands of
# entries, far beyond what a picker can usefully present.
_LIST_MAX_BYTES = 1 << 20


async def list_remote_dir(open_exec: OpenExec, path: str) -> list[RemoteEntry]:
    """List a container directory over the exec API (issue #124).

    A single read-only round-trip: `ls -1Ap` on the remote side, stdout
    parsed by the trailing-slash directory marker, directories first and
    alphabetical within each group. Raises TransferError when the listing
    is unavailable (no `ls` in the image, exec forbidden, non-zero exit,
    connection dropped before a verdict) or larger than `_LIST_MAX_BYTES`,
    so callers can degrade to manual path entry.

    The output is file *names* only — no resource payloads — which is why
    it does not go through the sensitive-read masking pipeline; it is also
    user-driven only and never registered as an agent tool.

    Filenames containing an embedded LF are out of scope: the `ls -1`
    protocol separates records with LF, so such a name is indistinguishable
    from two entries. Picking a resulting phantom entry only puts a
    nonexistent path in the input field — `validate_spec` re-checks every
    transfer, and the listing itself is read-only.
    """
    sink = _FrameSink()
    # bytearray: += on bytes copies the accumulated listing per frame.
    stdout = bytearray()
    try:
        async with open_exec(list_dir_command(path), False) as ws:
            async for msg in ws:
                stdout += sink.feed(msg.data)
                if len(stdout) > _LIST_MAX_BYTES:
                    # Leaving the `async with` closes the stream: the read
                    # is bounded, not merely the parse.
                    raise TransferError(f"directory listing for {path} is too large to browse")
    except TransferError:
        raise
    except Exception as exc:
        # open_pod_exec propagates transport/API failures (HTTP 403, broken
        # connection) untyped; normalize them so callers can degrade to
        # manual path entry. CancelledError is a BaseException and passes.
        raise TransferError(f"cannot list {path}: {exc}") from exc
    if sink.failure is not None:
        raise TransferError(sink.error_message(f"cannot list {path}"))
    if not sink.verdict:
        raise TransferError(
            sink.error_message(f"connection closed without reporting an outcome for {path}")
        )
    entries = []
    # LF only: splitlines() would also split on VT/FF/U+0085, which are
    # unusual but valid filename characters, producing phantom entries.
    for line in stdout.decode("utf-8", errors="replace").split("\n"):
        if not line:
            continue
        is_dir = line.endswith("/")
        entries.append(RemoteEntry(line.rstrip("/") if is_dir else line, is_dir))
    entries.sort(key=lambda e: (not e.is_dir, e.name))
    return entries


def download_command(remote_path: str) -> list[str]:
    """tar argv producing a single-file archive of ``remote_path`` on stdout.

    ``-C dir base`` keeps the archive member name to the basename so the
    local extraction never depends on the remote directory layout. A basename
    beginning with ``-`` is prefixed with ``./`` so tar treats it as an
    operand, never as an option (the member name is irrelevant locally:
    ``extract_single_file`` streams bytes into the caller-chosen path).
    """
    directory, base = posixpath.split(remote_path)
    if base.startswith("-"):
        base = f"./{base}"
    return ["tar", "cf", "-", "-C", directory or "/", base]


def upload_command(remote_path: str) -> list[str]:
    """tar argv extracting an archive from stdin into ``remote_path``'s parent."""
    directory = posixpath.dirname(remote_path) or "/"
    return ["tar", "xf", "-", "-C", directory]


#: Lowercased stderr fragments that identify a *permission* failure on the
#: remote side. Deliberately narrow: "Cannot open" alone also covers plain
#: missing files, which must never get a permission hint.
_PERMISSION_FRAGMENTS = ("permission denied", "read-only file system")


def permission_hint(message: str, remote_path: str) -> str | None:
    """One actionable hint line for a remote permission failure, else None.

    The server's verbatim message is always kept (it is the accurate part);
    this only *adds* direction for the common non-root-image /
    ``readOnlyRootFilesystem`` case (issue #123).
    """
    lowered = message.lower()
    if not any(fragment in lowered for fragment in _PERMISSION_FRAGMENTS):
        return None
    directory = posixpath.dirname(remote_path) or "/"
    return (
        f"hint: the container user cannot write to {directory} — "
        "try /tmp or a volume mount, or check readOnlyRootFilesystem"
    )


def default_local_path(remote_path: str) -> str:
    """Default download destination: ``~/Downloads/<basename>``.

    Falls back to ``~/<basename>`` when the ``Downloads`` directory does not
    exist or is not writable, so the default always survives
    ``validate_spec``'s parent checks.
    """
    base = posixpath.basename(remote_path.rstrip("/")) or "download"
    downloads = Path("~/Downloads").expanduser()
    usable = downloads.is_dir() and os.access(downloads, os.W_OK | os.X_OK)
    directory = downloads if usable else Path("~").expanduser()
    return str(directory / base)


def pack_file(local_path: Path, arcname: str, archive_path: Path) -> int:
    """Write a single-member tar of ``local_path`` (named ``arcname``) to
    ``archive_path``; returns the packed file's size in bytes.

    The source is deliberately dereferenced: validation follows symlinks
    (``Path.is_file()``), so a symlink source must pack the target's *bytes*
    as a regular file — never a symlink entry the remote tar would recreate
    as a (possibly dangling) link in the container.
    """
    stat = local_path.stat()
    info = tarfile.TarInfo(arcname)
    info.size = stat.st_size
    info.mtime = int(stat.st_mtime)
    info.mode = stat.st_mode & 0o777
    with tarfile.open(archive_path, "w") as tf, local_path.open("rb") as src:
        tf.addfile(info, src)
    return stat.st_size


def extract_single_file(archive_path: Path, dest: Path) -> int:
    """Extract the sole regular-file member of ``archive_path`` into ``dest``.

    ``download_command`` archives exactly one file, so anything else means
    the remote path was not the requested single file: a directory would
    make the remote tar recursively emit the whole tree, and silently
    extracting its first child would report the wrong file as a success.

    Member names are never used as filesystem paths — the bytes are streamed
    straight into the caller-chosen ``dest`` — so hostile names like
    ``../../evil`` in a compromised container's tar output are inert. The
    bytes are staged in a temp file next to ``dest`` and atomically renamed
    over it only on success, so a truncated member or local write error can
    never truncate or partially overwrite an existing destination.

    Returns the number of bytes written; raises ValueError when the archive
    does not contain exactly one regular file.
    """
    with tarfile.open(archive_path) as tf:
        members = iter(tf)
        member = next(members, None)
        if member is None:
            raise ValueError("archive contains no file")
        if not member.isfile():
            raise ValueError(
                f"remote path is not a regular file ({member.name!r}) — "
                "directories cannot be transferred"
            )
        src = tf.extractfile(member)
        if src is None:  # pragma: no cover - isfile() guarantees a stream
            raise ValueError("archive contains no file")
        fd, staging_name = tempfile.mkstemp(
            dir=dest.parent, prefix=f".{dest.name}.", suffix=".extract"
        )
        staging = Path(staging_name)
        try:
            written = 0
            with src, os.fdopen(fd, "wb") as out:
                while chunk := src.read(_COPY_CHUNK):
                    out.write(chunk)
                    written += len(chunk)
            if next(members, None) is not None:
                raise ValueError(
                    "archive contains more than one member — "
                    "the remote path must name a single file"
                )
            os.replace(staging, dest)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
        return written


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
        #: True once a channel-3 status frame arrived. ``failure is None``
        #: alone cannot distinguish an explicit Success from a connection
        #: that closed before the server reported anything.
        self.verdict = False

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
            self.verdict = True
            self.failure = _parse_error_channel(payload)
        return b""

    def error_message(self, fallback: str) -> str:
        """User-facing failure text: server verdict first, stderr for detail."""
        parts = [self.failure or fallback]
        if self.stderr:
            parts.append(self.stderr.decode("utf-8", errors="replace").strip())
        return "\n".join(p for p in parts if p)


async def _await_thread(func: Callable[..., _T], /, *args: Any) -> _T:
    """Run ``func`` in a worker thread, surviving caller cancellation.

    A thread cannot be interrupted: when the awaiting task is cancelled the
    thread keeps running, and a caller's cleanup (unlinking the tar file the
    thread still has open) would race it. Cancellation is therefore deferred
    until the thread has actually finished.
    """
    inner = asyncio.ensure_future(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        try:
            await inner
        except Exception:
            # Cancellation wins, but keep the thread's own failure findable.
            logger.debug("thread work failed while awaiting cancellation", exc_info=True)
        raise


def _create_spool(local_path: Path) -> tuple[int, Path]:
    """Create the download staging file next to the destination.

    OSErrors are normalized to TransferError naming the destination
    directory — never the internal staging file (issue #123). validate_spec
    pre-checks writability, but the bit can flip between the dialog and the
    stream. Only permission failures get the "not writable" wording: mkstemp
    can also fail with ENOSPC, EMFILE, ENAMETOOLONG and the like.
    """
    try:
        fd, spool_name = tempfile.mkstemp(
            dir=local_path.parent, prefix=f".{local_path.name}.", suffix=".part"
        )
    except OSError as exc:
        if isinstance(exc, PermissionError):
            detail = f"local directory is not writable: {local_path.parent}"
        else:
            detail = f"cannot create download staging file in {local_path.parent}"
        raise TransferError(f"{detail} ({exc.strerror or exc})") from exc
    return fd, Path(spool_name)


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
    fd, spool_path = _create_spool(local_path)
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
        if not sink.verdict:
            # No channel-3 status at all: the exec outcome is unknown (e.g.
            # a proxy dropped the connection). Never extract what may be a
            # truncated archive and never audit it as a success.
            raise TransferError(
                sink.error_message(
                    f"connection closed without reporting an outcome for {remote_path}"
                )
            )
        if total == 0:
            raise TransferError(
                sink.error_message(f"no data received for {remote_path} — does the file exist?")
            )
        try:
            return await _await_thread(extract_single_file, spool_path, local_path)
        except OSError as exc:
            # Same late-writability story as the spool above: report the
            # destination, not the staging temp file the extraction opened.
            raise TransferError(f"cannot write {local_path}: {exc.strerror or exc}") from exc
    finally:
        # The unlink needs the directory's write bit too: when that is what
        # just failed, a raw PermissionError here would replace the
        # normalized TransferError and expose the .part staging name. A
        # cleanup failure after a *successful* extraction is only logged —
        # the download itself did land.
        try:
            spool_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove download spool %s", spool_path)


def _with_permission_hint(message: str, remote_path: str) -> str:
    """Append the permission hint to an upload failure message when it fits."""
    hint = permission_hint(message, remote_path)
    return f"{message}\n{hint}" if hint else message


async def upload(
    open_exec: OpenExec,
    local_path: Path,
    remote_path: str,
    progress: Progress | None = None,
) -> int:
    """Stream ``local_path`` into the container as ``remote_path``.

    The remote side runs ``tar xf - -C <parent>``; the local file is packed
    into a single-member archive (named after the remote basename) and sent
    over the stdin channel. tar exits once it reads the archive's
    end-of-archive marker, after which the server reports the exec outcome
    on the error channel — an explicit Success verdict is required before
    the upload is reported (and audited) as successful. Returns the file's
    byte count.
    """
    arcname = posixpath.basename(remote_path)
    # mkstemp + close: pack_file reopens the path by name, which a
    # still-open NamedTemporaryFile handle would forbid on Windows.
    fd, archive_name = tempfile.mkstemp(suffix=".tar")
    os.close(fd)
    archive_path = Path(archive_name)
    try:
        size = await _await_thread(pack_file, local_path, arcname, archive_path)
        async with open_exec(upload_command(remote_path), True) as ws:
            sink = _FrameSink()

            async def _drain() -> None:
                async for msg in ws:
                    sink.feed(msg.data)
                    if sink.verdict:
                        return

            reader = asyncio.create_task(_drain())
            try:
                try:
                    await _send_archive(ws, archive_path, size, progress)
                except OSError as exc:
                    # The connection usually drops because the remote command
                    # died; drain what the server managed to say, then prefer
                    # its verdict over the raw transport error.
                    await asyncio.wait({reader}, timeout=_UPLOAD_DRAIN_GRACE)
                    raise TransferError(
                        _with_permission_hint(
                            sink.error_message(f"connection lost: {exc}"), remote_path
                        )
                    ) from exc
                # Wait for the server's verdict: tar exits on the archive's
                # end-of-archive marker, then the status frame arrives and
                # _drain returns (or the server closes the websocket).
                await asyncio.wait({reader}, timeout=_UPLOAD_VERDICT_TIMEOUT)
            finally:
                reader.cancel()
                # Cancellation must be observed before the websocket context
                # closes, or the reader dies mid-iteration with a warning.
                await asyncio.gather(reader, return_exceptions=True)
            if sink.failure is not None:
                raise TransferError(
                    _with_permission_hint(sink.error_message("upload failed"), remote_path)
                )
            if not sink.verdict:
                # The stderr may still carry the permission story even when
                # the server dropped before any channel-3 verdict.
                raise TransferError(
                    _with_permission_hint(
                        sink.error_message(
                            "upload sent, but the connection closed without reporting an outcome"
                        ),
                        remote_path,
                    )
                )
    finally:
        archive_path.unlink(missing_ok=True)
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
