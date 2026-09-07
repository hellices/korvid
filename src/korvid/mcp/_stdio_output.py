"""Cancellable SDK output with exclusive, restored stdout descriptor ownership."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import TextIO

import anyio

from korvid.mcp._stdio_input import _CREATION_FLAGS, _RELAY

_STDOUT_CLAIM = threading.Lock()


class _Output(anyio.AsyncFile[str]):
    def __init__(self, source: TextIO, send: Callable[[bytes], Awaitable[None]]) -> None:
        super().__init__(source)
        self._send = send

    async def write(self, data: str) -> int:
        await self._send(data.encode("utf-8"))
        return len(data)

    async def flush(self) -> None:
        # write() sends bytes directly; there is no Python text buffer to flush.
        await anyio.lowlevel.checkpoint()


def _bind_native_stdout(fd: int) -> None:
    if sys.platform == "win32" and fd == 1:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        set_std_handle = kernel32.SetStdHandle
        set_std_handle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        set_std_handle.restype = wintypes.BOOL
        if not set_std_handle(-11, msvcrt.get_osfhandle(fd)):
            raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def _claim_stdout(source: TextIO) -> Iterator[TextIO]:
    if not _STDOUT_CLAIM.acquire(blocking=False):
        raise RuntimeError("Another MCP stdout transport is already running.")
    try:
        fd = source.fileno()
        inheritable = os.get_inheritable(fd)
        with os.fdopen(os.dup(fd), "w", encoding="utf-8") as owned:
            try:
                os.dup2(sys.stderr.fileno(), fd, inheritable=inheritable)
                _bind_native_stdout(fd)
                source.flush()
                yield owned
            finally:
                try:
                    # Buffered handler prints must go to stderr, not the restored wire.
                    source.flush()
                finally:
                    os.dup2(owned.fileno(), fd, inheritable=inheritable)
                    _bind_native_stdout(fd)
    finally:
        _STDOUT_CLAIM.release()


async def _send_fd(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        await anyio.lowlevel.checkpoint()
        try:
            written = os.write(fd, remaining)
        except BlockingIOError:
            await anyio.wait_writable(fd)
            continue
        if written == 0:
            raise OSError("MCP stdout write made no progress.")
        remaining = remaining[written:]


@asynccontextmanager
async def _posix_stdout(source: TextIO) -> AsyncIterator[anyio.AsyncFile[str]]:
    fd = source.fileno()
    blocking = os.get_blocking(fd)
    os.set_blocking(fd, False)
    try:
        yield _Output(source, lambda data: _send_fd(fd, data))
    finally:
        os.set_blocking(fd, blocking)


@asynccontextmanager
async def _relay_stdout(source: TextIO) -> AsyncIterator[anyio.AsyncFile[str]]:
    process = await anyio.open_process(
        [sys.executable, "-I", "-S", "-c", _RELAY],
        stdin=subprocess.PIPE,
        stdout=source,
        stderr=None,
        creationflags=_CREATION_FLAGS,
        start_new_session=sys.platform != "win32",
    )
    try:
        writer = process.stdin
        if writer is None:
            raise OSError("MCP stdout relay has no input pipe.")
        yield _Output(source, writer.send)
        # EOF drains accepted bytes on normal exit. Cancellation instead kills
        # the relay, including a synchronous write blocked by an unread host.
        await writer.aclose()
        if await process.wait() != 0:
            raise OSError("MCP stdout relay failed.")
    finally:
        with anyio.CancelScope(shield=True):
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.aclose()


@asynccontextmanager
async def cancellable_stdout(source: TextIO) -> AsyncIterator[anyio.AsyncFile[str]]:
    """Own the wire privately and divert both Python and fd-level handler output."""
    with _claim_stdout(source) as owned:
        transport = _relay_stdout if sys.platform == "win32" else _posix_stdout
        async with transport(owned) as output:
            yield output
