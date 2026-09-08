"""Cancellable stdin for the SDK's framing loop, without abandoned reader threads."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import TextIO

import anyio
from anyio.abc import ByteReceiveStream, Process

_CHUNK_SIZE = 65536
_CREATION_FLAGS = 0
if sys.platform == "win32":
    _CREATION_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP

_RELAY = """
import sys
while chunk := sys.stdin.buffer.read1(65536):
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
"""


class _Input(anyio.AsyncFile[str]):
    def __init__(self, source: TextIO, receive: Callable[[], Awaitable[bytes]]) -> None:
        super().__init__(source)
        self._receive = receive
        self._buffer = bytearray()

    async def readline(self) -> str:
        # StreamReader.readline has a 64 KiB limit; SDK input has no such limit.
        start = 0
        while (end := self._buffer.find(b"\n", start)) < 0:
            start = len(self._buffer)
            chunk = await self._receive()
            if not chunk:
                end = len(self._buffer) - 1
                break
            self._buffer.extend(chunk)
        line = self._buffer[: end + 1]
        del self._buffer[: end + 1]
        return line.decode("utf-8", errors="replace")


async def _receive_fd(fd: int) -> bytes:
    while True:
        await anyio.wait_readable(fd)
        try:
            return os.read(fd, _CHUNK_SIZE)
        except BlockingIOError:
            continue


async def _receive_relay(process: Process, reader: ByteReceiveStream) -> bytes:
    try:
        return await reader.receive(_CHUNK_SIZE)
    except anyio.EndOfStream:
        if await process.wait() != 0:
            raise OSError("MCP stdin relay failed.") from None
        return b""


@asynccontextmanager
async def _relay_stdin(source: TextIO) -> AsyncIterator[anyio.AsyncFile[str]]:
    # Windows inherited stdin pipes are synchronous handles, not IOCP handles.
    # A byte-only child owns that blocking read; its stdout is an asyncio pipe.
    # Unlike abandoning a worker thread, killing and waiting reclaims the reader.
    process = await anyio.open_process(
        [sys.executable, "-I", "-S", "-c", _RELAY],
        stdin=source,
        stdout=subprocess.PIPE,
        stderr=None,
        creationflags=_CREATION_FLAGS,
        start_new_session=sys.platform != "win32",
    )
    try:
        reader = process.stdout
        if reader is None:
            raise OSError("MCP stdin relay has no output pipe.")
        yield _Input(source, lambda: _receive_relay(process, reader))
    finally:
        with anyio.CancelScope(shield=True):
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            # AnyIO also closes paused pipe transports before waiting for exit.
            await process.aclose()


@asynccontextmanager
async def cancellable_stdin(source: TextIO) -> AsyncIterator[anyio.AsyncFile[str]]:
    """Adapt host stdin while leaving SDK JSON parsing and stdout ownership intact."""
    if sys.platform == "win32":
        async with _relay_stdin(source) as stream:
            yield stream
        return

    with os.fdopen(os.dup(source.fileno()), "r", encoding="utf-8", errors="replace") as owned:
        fd = owned.fileno()
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or owned.isatty()):
            # Regular files and /dev/null do not wait for a host to send EOF.
            yield anyio.wrap_file(owned)
            return
        blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
        try:
            yield _Input(owned, lambda: _receive_fd(fd))
        finally:
            os.set_blocking(fd, blocking)
