"""Transfer execution lifecycle, extracted from the app (issue #91 U3a).

`TransferController` owns the post-approval half of the ctrl+t flow:
serializing transfers, re-verifying the approved pod incarnation,
fail-closed intent auditing, streaming the tar over the exec API with
progress, and auditing the outcome. The dialogs, approval gate,
context-epoch checks, and `run_worker` ownership stay on the app — it
hands this controller narrow callables instead of itself.

The dependency getters (`open_pod_exec`, `audit`) are read at run time
because a `:ctx` switch retargets the exec client after construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from korvid.core.audit import AuditLog
from korvid.core.transfer import (
    TransferError,
    TransferSpec,
)
from korvid.core.transfer import (
    download as transfer_download,
)
from korvid.core.transfer import (
    upload as transfer_upload,
)

logger = logging.getLogger(__name__)


class TransferProgress(Protocol):
    """The slice of TransferProgressScreen the controller drives."""

    def update_progress(self, count: int) -> None:
        """Render the running byte count."""


class TransferController:
    """Owns transfer serialization, auditing, and the cancellable stream."""

    def __init__(
        self,
        *,
        notify: Callable[..., None],
        open_pod_exec: Callable[
            [], Callable[..., contextlib.AbstractAsyncContextManager[Any]] | None
        ],
        audit: Callable[[], AuditLog | None],
        pod_uid_unchanged: Callable[..., Awaitable[bool]],
        show_progress: Callable[[str], Awaitable[TransferProgress]],
        close_progress: Callable[[TransferProgress], None],
    ) -> None:
        self._notify = notify
        self._open_pod_exec = open_pod_exec
        self._audit = audit
        self._pod_uid_unchanged = pod_uid_unchanged
        self._show_progress = show_progress
        self._close_progress = close_progress
        self._task: asyncio.Task[int] | None = None
        self._in_flight = False

    @property
    def in_flight(self) -> bool:
        """True for the whole lifecycle: launch through outcome audit."""
        return self._in_flight

    @property
    def task(self) -> asyncio.Task[int] | None:
        """The live stream task; what escape on the progress screen cancels."""
        return self._task

    def cancel(self) -> None:
        """Cancel the stream task — never the surrounding worker, which
        still has auditing left to do."""
        if self._task is not None:
            self._task.cancel()

    async def run(
        self,
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        uid: str | None,
    ) -> None:
        """Serialize transfers, then audit (fail-closed), stream, audit outcome.

        The guard spans the whole lifecycle — uid re-check, intent audit,
        stream, outcome audit — because the task slot is single: a
        concurrently launched worker would overwrite it and escape could
        cancel the wrong stream.
        """
        if self._in_flight:
            self._notify("A transfer is already in progress", severity="warning")
            return
        self._in_flight = True
        try:
            await self._run_guarded(namespace, name, container, spec, uid)
        finally:
            self._in_flight = False

    async def _run_guarded(
        self,
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        uid: str | None,
    ) -> None:
        """Audit (fail-closed), stream with progress, audit the outcome."""
        open_pod_exec = self._open_pod_exec()
        audit = self._audit()
        if open_pod_exec is None or audit is None:
            if audit is None:
                # Fail-closed for downloads too: the transfer *event* is the
                # audit requirement (issue #47), not just the write direction.
                self._notify("Transfer blocked: no audit log configured", severity="error")
            return
        if uid is not None and not await self._pod_uid_unchanged(
            namespace, name, uid, action="Transfer"
        ):
            return
        action = f"transfer_{spec.direction}"
        detail = f"container={container or '-'} remote={spec.remote_path} local={spec.local_path}"
        try:
            await asyncio.to_thread(
                self._audit_entry, audit, action, namespace, name, detail, "intent"
            )
        except Exception:
            logger.exception("audit append failed; blocking transfer")
            self._notify("Transfer blocked: audit log unavailable", severity="error")
            return

        downloading = spec.direction == "download"
        arrow = "↓ download" if downloading else "↑ upload"
        progress = await self._show_progress(f"{arrow}  {namespace}/{name}:{spec.remote_path}")
        try:
            outcome, extra = await self._outcome(
                open_pod_exec, namespace, name, container, spec, progress
            )
            detail += extra
        finally:
            self._close_progress(progress)
        try:
            await asyncio.to_thread(
                self._audit_entry, audit, action, namespace, name, detail, outcome
            )
        except Exception:
            logger.exception("audit append failed after transfer")
            self._notify("Audit write failed for the executed transfer", severity="warning")

    async def _outcome(
        self,
        open_pod_exec: Callable[..., contextlib.AbstractAsyncContextManager[Any]],
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        progress_screen: TransferProgress,
    ) -> tuple[str, str]:
        """Stream and notify; returns (audit outcome, extra audit detail).

        Cancelled and failed transfers record the bytes moved so far, so a
        partial transfer stays auditable.
        """
        latest = 0

        def _progress(count: int) -> None:
            nonlocal latest
            latest = count
            progress_screen.update_progress(count)

        try:
            count = await self._stream(open_pod_exec, namespace, name, container, spec, _progress)
        except asyncio.CancelledError:
            self._notify("Transfer cancelled")
            return "cancelled", f" bytes={latest}"
        except TransferError as exc:
            self._notify(f"Transfer failed: {exc}", severity="error")
            return f"error: {exc}", f" bytes={latest}"
        except Exception as exc:  # transport errors from aiohttp/OS surface untyped
            logger.exception("transfer failed")
            self._notify(f"Transfer failed: {exc}", severity="error")
            return f"error: {exc}", f" bytes={latest}"
        verb = "downloaded" if spec.direction == "download" else "uploaded"
        self._notify(f"{verb} {count:,} bytes ({spec.remote_path})")
        return "success", f" bytes={count}"

    async def _stream(
        self,
        open_pod_exec: Callable[..., contextlib.AbstractAsyncContextManager[Any]],
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        progress: Callable[[int], None],
    ) -> int:
        """Run the tar stream as a cancellable task; returns the byte count."""

        def open_exec(
            command: list[str], stdin: bool
        ) -> contextlib.AbstractAsyncContextManager[Any]:
            return open_pod_exec(namespace, name, container, command, stdin=stdin)

        local = Path(spec.local_path).expanduser()
        if spec.direction == "download":
            coro = transfer_download(open_exec, spec.remote_path, local, progress)
        else:
            coro = transfer_upload(open_exec, local, spec.remote_path, progress)
        task = asyncio.create_task(coro)
        self._task = task
        try:
            return await task
        finally:
            self._task = None

    @staticmethod
    def _audit_entry(
        audit: AuditLog, action: str, namespace: str, name: str, detail: str, outcome: str
    ) -> None:
        audit.append(
            action=action,
            kind="pods",
            group="",  # transfers always target a core/v1 pod
            version="v1",
            namespace=namespace,
            name=name,
            detail=detail,
            outcome=outcome,
        )
