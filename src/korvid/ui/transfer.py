"""The file-transfer journey, extracted from the app (issue #91 U3a, and
the user-facing half in Deep Task 9).

`TransferController` owns the whole ctrl+t flow: the selected pod and the
guards that decide whether a transfer may start at all, the container
pick, the transfer dialog with its read-only remote-path listing, the
upload approval, and then execution — serializing transfers, re-verifying
the approved pod incarnation, fail-closed intent auditing, streaming the
tar over the exec API with progress, and auditing the outcome.

It owns no part of the write perimeter: an upload is approved and reserved
by the single `WriteCoordinator`, which this controller composes rather
than reimplements. Textual arrives as `UiSurface` plus the one-method
`TransferScreens` (popping the progress modal is a screen-stack action
`UiSurface` deliberately does not expose), and the selection as
`ViewState`; `run_worker` ownership stays with the app.

The dependency getters (`open_pod_exec`, `audit`) are read at run time
because a `:ctx` switch retargets the exec client after construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from textual.screen import Screen

from korvid.core.audit import AuditLog
from korvid.core.transfer import (
    RemoteEntry,
    TransferError,
    TransferSpec,
    list_remote_dir,
)
from korvid.core.transfer import (
    download as transfer_download,
)
from korvid.core.transfer import (
    upload as transfer_upload,
)
from korvid.k8s.errors import ApiStatusError
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.transfer_screen import TransferProgressScreen, TransferScreen
from korvid.ui.write_coordinator import WriteCoordinator

logger = logging.getLogger(__name__)


class TransferProgress(Protocol):
    """The slice of TransferProgressScreen the controller drives."""

    def update_progress(self, count: int) -> None:
        """Render the running byte count."""


class TransferScreens(ABC):
    """The one screen-stack action the transfer lifecycle needs.

    `UiSurface` can push a modal and answer whether one is still on top,
    but popping it is deliberately not on that surface - a controller that
    could pop screens could unwind dialogs it never opened. The progress
    modal is this controller's own, so it gets exactly the narrow way to
    close it and nothing more.
    """

    @abstractmethod
    def dismiss_if_current(self, screen: Screen[Any]) -> None:
        """Pop *screen* when it is still the screen on top; ignore it otherwise."""


class TransferController:
    """Owns the transfer journey: selection, dialogs, approval, and the stream."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        view: ViewState,
        writes: WriteCoordinator,
        screens: TransferScreens,
        open_pod_exec: Callable[
            [], Callable[..., contextlib.AbstractAsyncContextManager[Any]] | None
        ],
        audit: Callable[[], AuditLog | None],
        pod_containers: Callable[[str, str], tuple[str, ...]],
        target_uid: Callable[[str, str | None, str], Awaitable[str | None]],
        pod_uid_unchanged: Callable[..., Awaitable[bool]],
    ) -> None:
        self._ui = ui
        self._view = view
        self._writes = writes
        self._screens = screens
        self._open_pod_exec = open_pod_exec
        self._audit = audit
        self._pod_containers = pod_containers
        self._target_uid = target_uid
        self._pod_uid_unchanged = pod_uid_unchanged
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

    # ------------------------------------------------------------------
    # The user-facing half: selection, container pick, dialog, approval
    # ------------------------------------------------------------------

    def start(self) -> None:
        """`ctrl+t` — resolve the selected pod and open the transfer dialog.

        Every refusal the keypress can hit lives here: transfers are a pods
        flow, they need an exec client, the single task slot allows one at a
        time, and a transfer must not start mid-`:ctx` (the stream would
        race the teardown/retarget and could address whichever cluster
        wins).

        The transfer is bound to this pod *incarnation*: the exec API
        addresses the pod by namespace/name only, so the uid captured here
        is re-verified right before streaming - a same-named replacement
        created while the dialogs are open must never receive the bytes.
        """
        if self._view.current_kind() != "pods":
            self._ui.notify("File transfer is only available for pods", severity="warning")
            return
        if self._open_pod_exec() is None:
            self._ui.notify("File transfer unavailable (no cluster connection)", severity="warning")
            return
        if self._in_flight:
            self._ui.notify("A transfer is already in progress", severity="warning")
            return
        if not self._writes.reads_allowed():
            return
        epoch = self._writes.epoch()
        namespace, name = self._view.selected_ns_name()
        if namespace is None or name is None:
            return
        containers = self._pod_containers(namespace, name)
        uid = self._view.selected_uid(namespace, name)
        if len(containers) > 1:

            def _on_pick(container: str | None) -> None:
                if container is not None:
                    self.open_dialog(namespace, name, container, uid, epoch)

            self._ui.push_screen(PickScreen(f"Container in {name}:", list(containers)), _on_pick)
            return
        self.open_dialog(namespace, name, containers[0] if containers else None, uid, epoch)

    def open_dialog(
        self, namespace: str, name: str, container: str | None, uid: str | None, epoch: int
    ) -> None:
        """Open the direction/path dialog for one pod container."""
        target = f"{namespace}/{name}" + (f" ({container})" if container else "")

        def _on_spec(spec: TransferSpec | None) -> None:
            if spec is not None:
                self.start_transfer(namespace, name, container, spec, uid, epoch)

        self._ui.push_screen(
            TransferScreen(
                target,
                remote_lister=self.remote_lister(namespace, name, container, uid=uid, epoch=epoch),
            ),
            _on_spec,
        )

    def remote_lister(
        self, namespace: str, name: str, container: str | None, *, uid: str | None, epoch: int
    ) -> Callable[[str], Awaitable[list[RemoteEntry]]] | None:
        """Directory-listing callable for the ctrl+o remote path picker.

        A read-only `ls` over the exec API (issue #124): names only, so the
        masking pipeline does not apply, and it is never exposed to the
        agent — browsing is user-driven like the transfer itself.

        Bound to the dialog's context *epoch* and pod *uid*: a :ctx switch
        retargets the shared exec client, and a same-named replacement pod
        does not change the epoch — either way the listing would come from
        somewhere other than what the dialog shows. Raises TransferError
        (the picker's degradation path) when either binding is stale,
        checked before each exec and again after the await so a listing
        that raced the change is discarded.
        """
        open_pod_exec = self._open_pod_exec()
        if open_pod_exec is None:
            return None

        async def _guard() -> None:
            def check_epoch() -> None:
                if self._writes.switching() or epoch != self._writes.epoch():
                    raise TransferError(
                        f"the kube context changed while the dialog for {namespace}/{name} was open"
                    )

            check_epoch()
            await self._verify_listing_pod(namespace, name, uid)
            # The uid lookup awaited the manifest source: a switch that
            # completed during that await retargeted the shared exec client,
            # so re-check before any exec follows.
            check_epoch()

        async def _list(path: str) -> list[RemoteEntry]:
            await _guard()

            def open_exec(
                command: list[str], stdin: bool
            ) -> contextlib.AbstractAsyncContextManager[Any]:
                return open_pod_exec(namespace, name, container, command, stdin=stdin)

            entries = await list_remote_dir(open_exec, path)
            # Re-check: a listing that raced a switch or a pod replacement
            # must never be presented under the old selection.
            await _guard()
            return entries

        return _list

    async def _verify_listing_pod(self, namespace: str, name: str, uid: str | None) -> None:
        """Raise TransferError unless pod `uid` is still the incarnation the
        transfer dialog was opened for. Fails open when no uid was captured
        (matching the transfer's own uid gate below); with a captured uid an
        unverifiable lookup fails closed — browsing is optional, so degrading
        to manual entry beats listing a same-named replacement pod."""
        if uid is None:
            return
        try:
            current = await self._target_uid("pods", namespace, name)
        except ApiStatusError as exc:
            raise TransferError(f"pod {name} no longer exists") from exc
        if current is None:
            raise TransferError(f"pod {name} could not be verified — enter the path manually")
        if current != uid:
            raise TransferError(f"pod {name} was replaced since the dialog was opened")

    def start_transfer(
        self,
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        uid: str | None,
        epoch: int,
    ) -> None:
        """Gate then launch: uploads write into the container filesystem, so
        they are blocked in read-only mode and pass the approval dialog."""
        if self._writes.switching() or epoch != self._writes.epoch():
            # The picker/transfer dialogs stayed open across a context
            # switch: the pod selection (and its uid, which fails open when
            # missing) belongs to the old cluster while the shared exec
            # client now targets the new one.
            self._ui.notify(
                f"transfer to {namespace}/{name} cancelled - the kube context"
                " changed while the dialog was open",
                severity="warning",
            )
            return
        if spec.direction == "upload":
            if self._view.readonly():
                self._ui.notify("Upload disabled in read-only mode", severity="warning")
                return

            def _approved(approved: bool | None) -> None:
                if not approved:
                    return
                if self._writes.switching() or epoch != self._writes.epoch():
                    self._ui.notify(
                        f"transfer to {namespace}/{name} cancelled - the kube"
                        " context changed while the approval was open",
                        severity="warning",
                    )
                    return
                self._launch(namespace, name, container, spec, uid)

            self._ui.push_screen(
                self._writes.confirm_screen(
                    f"Upload file to {namespace}/{name}",
                    f"{spec.local_path} → {container or 'pod'}:{spec.remote_path}\n"
                    "This writes into the container filesystem.",
                ),
                _approved,
            )
            return
        self._launch(namespace, name, container, spec, uid)

    def _launch(
        self,
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        uid: str | None,
    ) -> None:
        """Run the transfer as a reserved app worker.

        `WriteCoordinator.reserved` counts it as an in-flight cluster write,
        synchronously and before `run_worker` starts anything, so a `:ctx`
        queued in that gap already refuses to switch.
        """
        self._ui.run_worker(
            self._writes.reserved(lambda: self.run(namespace, name, container, spec, uid))
        )

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
            self._ui.notify("A transfer is already in progress", severity="warning")
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
                self._ui.notify("Transfer blocked: no audit log configured", severity="error")
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
            self._ui.notify("Transfer blocked: audit log unavailable", severity="error")
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
            self._screens.dismiss_if_current(progress)
        try:
            await asyncio.to_thread(
                self._audit_entry, audit, action, namespace, name, detail, outcome
            )
        except Exception:
            logger.exception("audit append failed after transfer")
            self._ui.notify("Audit write failed for the executed transfer", severity="warning")

    async def _show_progress(self, label: str) -> TransferProgressScreen:
        """Open the cancellable progress modal for a running transfer."""
        screen = TransferProgressScreen(label)
        await self._ui.push_screen(screen)
        return screen

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
            self._ui.notify("Transfer cancelled")
            return "cancelled", f" bytes={latest}"
        except TransferError as exc:
            self._ui.notify(f"Transfer failed: {exc}", severity="error")
            return f"error: {exc}", f" bytes={latest}"
        except Exception as exc:  # transport errors from aiohttp/OS surface untyped
            logger.exception("transfer failed")
            self._ui.notify(f"Transfer failed: {exc}", severity="error")
            return f"error: {exc}", f" bytes={latest}"
        verb = "downloaded" if spec.direction == "download" else "uploaded"
        self._ui.notify(f"{verb} {count:,} bytes ({spec.remote_path})")
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
