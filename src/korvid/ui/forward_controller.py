"""Port-forward session lifecycle, extracted from the app (issue #187).

`ForwardController` owns the whole life of a forward: resolving the target
workload, launching kubectl, tracking confirmations, reattaching after a
pod restart, polling liveness, and the audit queue that records starts and
stops without blocking the message pump.

Unlike the helm and OLM controllers this one also owns *state*. Eleven
fields moved with it - the broken set, the launching/reattaching/confirming
maps, the audit queue and its lock. They were only ever touched by these
methods, so leaving them on the app would have meant injecting eleven more
accessors to reach data nothing else reads.

The write perimeter, the view and the Textual surface arrive as the three
named interfaces; `run_worker` stays the app's, so a `:ctx` switch and app
shutdown still cancel everything this starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any, ClassVar

from textual.worker import Worker, get_current_worker

from korvid.core.audit import AuditLog
from korvid.core.portforward import (
    ForwardRecord,
    ForwardRegistry,
    ForwardSpec,
    candidate_remote_ports,
    controller_owner,
)
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.portforward import forward_target_gvr
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.port_forward_screen import ForwardListScreen
from korvid.ui.write_gate import WriteGate

logger = logging.getLogger(__name__)

#: How long a freshly launched forward gets to report ready before the
#: confirmation gives up and reports the failure instead.
_FORWARD_READY_SECONDS = 5.0


class ForwardController:
    """Owns port-forward sessions: launch, reattach, audit, and liveness."""

    def __init__(
        self,
        *,
        gate: WriteGate,
        view: ViewState,
        ui: UiSurface,
        forwards: Callable[[], ForwardRegistry | None],
        audit: Callable[[], AuditLog | None],
        get_manifest: Callable[[], Callable[[str, str | None, str], Any] | None],
    ) -> None:
        self._gate = gate
        self._view = view
        self._ui = ui
        self._forwards_registry = forwards
        self._audit_log = audit
        self._get_manifest_fn = get_manifest
        #: local ports whose forward died; the list screen marks them broken.
        self._broken_forwards: set[int] = set()
        #: audit records queued off the message pump - a slow disk must not
        #: stall the UI, and a stop audit must still survive app shutdown.
        self._forward_audit_queue: deque[dict[str, Any]] = deque()
        self._forward_audit_io_lock = threading.Lock()
        self._forward_audit_worker: Worker[None] | None = None
        #: confirmations in flight per local port, so a second keypress
        #: cannot stack dialogs on one forward.
        self._confirming_forwards: dict[int, list[Worker[None]]] = {}
        self._current_confirmations: dict[int, Worker[None]] = {}
        #: launches and reattaches keyed by the work that will finish them,
        #: so `:ctx` teardown can account for forwards that exist but have
        #: no registry entry yet.
        self._launching_forwards: dict[Worker[None], ForwardSpec] = {}
        self._reattaching_forwards: dict[asyncio.Event, ForwardSpec] = {}
        #: set during teardown: audits then enqueue directly instead of
        #: spawning a worker that the teardown would immediately cancel.
        self._forwards_closing = False
        self._deferred_stop_audits: dict[int, ForwardSpec] = {}

    _WORKLOAD_PLURALS: ClassVar[dict[str, str]] = {
        "Deployment": "deployments",
        "ReplicaSet": "replicasets",
        "ReplicationController": "replicationcontrollers",
        "StatefulSet": "statefulsets",
        "DaemonSet": "daemonsets",
        "Job": "jobs",
    }

    async def _forward_prefill_ports(
        self, kind: str, namespace: str, name: str
    ) -> tuple[list[int], bool]:
        """Declared TCP ports for the forward dialog, plus fetch success.

        The success flag lets the caller tell "no TCP ports declared"
        (reject a Service up front) apart from "manifest unavailable"
        (open the dialog unrestricted — kubectl has the final say).
        """
        get_manifest = self._get_manifest_fn()
        if get_manifest is None:
            return [], False
        try:
            manifest = await get_manifest(kind, namespace, name)
        except Exception as exc:  # prefill is a convenience — dialog works without it
            logger.debug("manifest fetch for port prefill failed: %s", exc)
            return [], False
        return candidate_remote_ports(kind, manifest), True

    async def _resolve_forward_workload(self, namespace: str, name: str) -> str | None:
        """The pod's owning workload as ``"<plural>/<name>"``, best effort.

        Captured when a forward starts so a later re-attach can follow the
        pod to its replacement (issue #38). ReplicaSets are resolved to their
        Deployment when they have one. Any failure yields None — the forward
        still works, only the follow-the-workload re-attach is unavailable.
        """
        get_manifest = self._get_manifest_fn()
        if get_manifest is None:
            return None
        try:
            owner = controller_owner(await get_manifest("pods", namespace, name))
        except Exception as exc:  # a convenience — never blocks the forward
            logger.debug("workload resolution for port-forward failed: %s", exc)
            return None
        if owner is not None and owner[0] == "ReplicaSet":
            # A failed chase (e.g. discovery has not learned replicasets yet)
            # keeps the ReplicaSet as the fallback target — the parent lookup
            # improves the target to a Deployment, it is not required.
            try:
                parent = controller_owner(await get_manifest("replicasets", namespace, owner[1]))
            except Exception as exc:
                logger.debug("deployment lookup failed; keeping replicaset owner: %s", exc)
                parent = None
            if parent is not None and parent[0] == "Deployment":
                owner = parent
        if owner is None:
            return None
        plural = self._WORKLOAD_PLURALS.get(owner[0])
        return f"{plural}/{owner[1]}" if plural is not None else None

    async def _start_forward(
        self,
        kind: str,
        namespace: str,
        name: str,
        *,
        local_port: int,
        remote_port: int,
        epoch: int,
    ) -> None:
        """Spawn a forward from the registry, audit it, and confirm to the user."""
        registry = self._forwards_registry()
        if registry is None:  # pragma: no cover - action guard already checked
            return
        if self._gate.switching() or epoch != self._gate.epoch():
            # The worker was scheduled just as a switch started: it is not
            # yet registered in _launching_forwards, so teardown could not
            # cancel it and it would spawn against the new cluster.
            self._ui.notify(
                f"port-forward to {name} cancelled - the kube context changed",
                severity="warning",
            )
            return
        workload = await self._resolve_forward_workload(namespace, name) if kind == "pods" else None
        if self._gate.switching() or epoch != self._gate.epoch():
            # The workload lookup awaited through a switch (this coroutine
            # registers below only after the lookup, so teardown missed it):
            # the old-cluster pod selection must not spawn kubectl against
            # the retargeted context.
            self._ui.notify(
                f"port-forward to {name} cancelled - the kube context changed",
                severity="warning",
            )
            return
        spec = ForwardSpec(
            kind=kind,
            namespace=namespace,
            name=name,
            local_port=local_port,
            remote_port=remote_port,
            workload=workload,
        )
        worker = get_current_worker()
        self._launching_forwards[worker] = spec
        try:
            # Off the event loop: reclaiming the local port may block briefly
            # on reaping a previously stopped child that ignored SIGTERM.
            record = await asyncio.to_thread(registry.start, spec)
        except (OSError, ValueError) as exc:
            # OSError: spawn failed (kubectl missing). ValueError: local
            # port collision detected up front by the registry, or a spawn
            # that lost the race against teardown (registry shut down).
            if not self._forwards_closing:
                self._ui.notify(f"Port-forward failed to start: {exc}", severity="error")
            self._audit_forward_shutdown_safe("port-forward-start", spec, outcome=f"error: {exc}")
            return
        except asyncio.CancelledError:
            # Shutdown cancelled the launch mid-spawn — the start must still
            # reach the log before any teardown stop entry (enqueue
            # directly: no new workers during shutdown).
            if self._audit_log() is not None:
                self._enqueue_forward_audit(
                    "port-forward-start", spec, outcome="stopped before ready"
                )
            raise
        finally:
            self._launching_forwards.pop(worker, None)
        if self._forwards_closing or registry.get(record.id) is None:
            # A quit or stop won the race between the registry publishing
            # the record and this coroutine resuming: no confirmation may
            # spawn (shutdown) or would ever resolve (record gone) — audit
            # the start here so its stop entry never reaches the log first.
            self._audit_forward_shutdown_safe(
                "port-forward-start", spec, outcome="stopped before ready"
            )
            return
        # Popen returning only proves the child exists — success is reported
        # after kubectl confirms the listener (or fails the bind/RBAC check).
        self._track_confirmation(record)

    async def _spawn_reattach(
        self, registry: ForwardRegistry, record: ForwardRecord, *, retarget: bool = False
    ) -> ForwardRecord | None:
        """Re-attach off-loop while teardown (and stops) can see and await it.

        With ``retarget`` the replacement follows the spec's recorded owning
        workload instead of the vanished pod (issue #38).

        Mirrors `_start_forward`'s tracking: between the registry adopting
        the replacement in its thread and this coroutine resuming, a quit or
        stop must defer behind this event so the replacement's start entry
        always reaches the audit log before any stop entry.
        """
        done = asyncio.Event()
        # The in-flight kubectl targets the retargeted spec — tracking and
        # the cancellation audit must name that GVR, not the vanished pod.
        attempted = (record.spec.retargeted() if retarget else None) or record.spec
        self._reattaching_forwards[done] = attempted
        try:
            revived = await asyncio.to_thread(
                functools.partial(registry.reattach, record.id, retarget=retarget)
            )
        except asyncio.CancelledError:
            # Shutdown cancelled the re-attach mid-spawn. The registry only
            # adopts a replacement while it is still open, so if one was (or
            # will be) adopted, its start must reach the log before the
            # teardown stop entries (enqueue directly: no new workers now).
            if self._audit_log() is not None:
                self._enqueue_forward_audit(
                    "port-forward-start", attempted, outcome="stopped before ready"
                )
            raise
        finally:
            self._reattaching_forwards.pop(done, None)
            done.set()
        if revived is None:
            # Never adopted (broken no more, stopped, or torn down mid-spawn)
            # — no replacement started, so there is nothing to report.
            return None
        if self._forwards_closing or registry.get(revived.id) is None:
            # A quit or stop won the race between the adoption and this
            # coroutine resuming: no confirmation may spawn (shutdown) or
            # would ever resolve (record gone) — audit the start here so its
            # stop entry never reaches the log first.
            self._audit_forward_shutdown_safe(
                "port-forward-start", revived.spec, outcome="stopped before ready"
            )
            return revived
        # Re-arm the broken toast right away: waiting for the next global
        # poll would silently swallow a breakage of the fresh process.
        self._broken_forwards.discard(revived.id)
        # Same readiness handshake as a fresh start (issue #38 review).
        self._track_confirmation(revived, reattached=True)
        return revived

    def _audit_forward_shutdown_safe(self, action: str, spec: ForwardSpec, *, outcome: str) -> None:
        """Audit a forward event without spawning workers once teardown began.

        The teardown flush drains directly-enqueued entries, so nothing is
        lost — outside of teardown this is a plain `_audit_forward` call.
        """
        if not self._forwards_closing:
            self._audit_forward(action, spec, outcome=outcome)
            return
        if self._audit_log() is not None:
            self._enqueue_forward_audit(action, spec, outcome=outcome)

    async def _confirm_forward(self, record: ForwardRecord, *, reattached: bool = False) -> None:
        """Toast and audit a forward start only once kubectl signals ready.

        An exit before the ready line is a failed start: the record is
        dropped (fresh starts only — a failed re-attach stays listed as
        broken for another try) and kubectl's last words become the error.
        """
        registry = self._forwards_registry()
        if registry is None:  # pragma: no cover - callers hold a registry
            return
        spec = record.spec
        worker = get_current_worker()
        try:
            # Snapshot before waiting: a re-attach during the wait bumps the
            # generation, and the abort below must never hit the replacement.
            generation = registry.generation(record.id)
            status = await asyncio.to_thread(
                registry.wait_ready, record.id, timeout=_FORWARD_READY_SECONDS
            )
            if status == "superseded" or self._current_confirmation(record.id) is not worker:
                # A re-attach superseded this confirmation: the record was
                # re-armed in place, so ``status`` (and everything on the
                # record) may describe the replacement process — nothing
                # observed here may be toasted, audited, or failed as this
                # generation's result. The registry reports the supersession
                # itself because the woken waiter can resume before the
                # re-attach publishes the replacement's confirmation token.
                self._audit_forward("port-forward-start", spec, outcome="superseded by re-attach")
                return
            if registry.get(record.id) is None:
                # The user stopped the still-starting forward from :pf. Its
                # deferred stop entry is queued behind this confirmation, so
                # the start still reaches the audit log first — and a
                # "failed to start" toast would be wrong for a deliberate stop.
                self._audit_forward("port-forward-start", spec, outcome="stopped before ready")
                return
            if status != "alive":
                self._report_failed_forward_start(
                    registry, record, status, reattached=reattached, generation=generation
                )
                return
            self._report_forward_ready(record, reattached=reattached)
        except asyncio.CancelledError:
            # Shutdown cancelled the confirmation mid-handshake — the start
            # must still reach the log before its teardown stop entry
            # (enqueue directly: no new workers during shutdown).
            if self._audit_log() is not None:
                self._enqueue_forward_audit(
                    "port-forward-start", spec, outcome="stopped before ready"
                )
            raise
        finally:
            # Drop only this generation's entry, and only once its start
            # audit is enqueued (above) — stops defer behind every
            # outstanding confirmation, so a superseded generation must stay
            # tracked until its entry cannot land after a stop anymore.
            self._untrack_confirmation(record.id, worker)

    def _untrack_confirmation(self, forward_id: int, worker: Worker[None]) -> None:
        """Remove one finished confirmation generation from the tracking maps."""
        entries = self._confirming_forwards.get(forward_id)
        if entries is not None:
            with contextlib.suppress(ValueError):
                entries.remove(worker)
            if not entries:
                del self._confirming_forwards[forward_id]
        # Only the current generation clears its own token — a superseded
        # worker leaving must not disturb the replacement's marker.
        if self._current_confirmations.get(forward_id) is worker:
            del self._current_confirmations[forward_id]

    def _track_confirmation(self, record: ForwardRecord, *, reattached: bool = False) -> None:
        """Spawn a readiness confirmation and register it as the current one."""
        worker = self._ui.run_worker(self._confirm_forward(record, reattached=reattached))
        self._confirming_forwards.setdefault(record.id, []).append(worker)
        self._current_confirmations[record.id] = worker

    def _current_confirmation(self, forward_id: int) -> Worker[None] | None:
        """The forward's current-generation confirmation worker, if any."""
        return self._current_confirmations.get(forward_id)

    def _report_forward_ready(self, record: ForwardRecord, *, reattached: bool) -> None:
        """Toast and audit a confirmed forward (fresh start or re-attach)."""
        spec = record.spec
        if reattached:
            self._audit_forward("port-forward-start", spec, outcome="reattached")
            self._ui.notify(f"Re-attached forward localhost:{spec.local_port}")
            return
        self._audit_forward("port-forward-start", spec)
        self._ui.notify(
            f"Forwarding localhost:{spec.local_port} → "
            f"{spec.namespace}/{spec.name}:{spec.remote_port}"
        )

    def _report_failed_forward_start(
        self,
        registry: ForwardRegistry,
        record: ForwardRecord,
        status: str,
        *,
        reattached: bool,
        generation: int | None = None,
    ) -> None:
        """Handle a readiness handshake that did not end in ``alive``.

        A ``starting`` result means kubectl stayed silent through the wait
        window: readiness was never confirmed and liveness polling could not
        correct a false success later (it only detects exits), so the forward
        is failed explicitly instead of reported ready on a guess. The caller
        already verified this confirmation is the record's current one.

        ``status`` is only a timeout snapshot: the abort itself is the
        registry's atomic compare-and-transition, whose returned outcome says
        exactly what happened — a readiness line that landed after the
        snapshot (``alive``) is reported as the success it is, a re-attach
        that raced the snapshot (``superseded``) keeps its replacement and
        only audits the supersession, and a stop that unlisted the record
        (``gone``) stands as the deliberate outcome it was.
        """
        spec = record.spec
        outcome = registry.fail_start(record.id, keep=reattached, generation=generation)
        if outcome == "alive":
            # The handshake resolved between the wait snapshot and the abort.
            self._report_forward_ready(record, reattached=reattached)
            return
        if outcome == "superseded":
            # A re-attach adopted a replacement while this confirmation timed
            # out — the replacement reports its own fate; this generation
            # only records that it was superseded.
            self._audit_forward("port-forward-start", spec, outcome="superseded by re-attach")
            return
        if outcome == "gone":  # stopped from :pf (or torn down) in the same window
            self._audit_forward("port-forward-start", spec, outcome="stopped before ready")
            return
        if status == "starting":
            detail = f"kubectl did not confirm the forward within {_FORWARD_READY_SECONDS:g}s"
        else:
            detail = record.last_output or "kubectl exited before the forward was ready"
        if reattached:
            # A failed re-attach stays listed as broken for another try. Mark
            # the breakage as already reported: the specific error toasted
            # below must not be followed by the poll's generic broken toast.
            self._broken_forwards.add(record.id)
        self._ui.notify(f"Port-forward failed to start: {detail}", severity="error")
        self._audit_forward("port-forward-start", spec, outcome=f"error: {detail}")

    async def _audit_stop_after_confirm(
        self, pending: list[Worker[None] | asyncio.Event], forward_id: int
    ) -> None:
        """Audit a stop only after every outstanding confirmation resolved.

        A superseded generation may still be waiting alongside the current
        one — each enqueues its own start entry, so the stop must defer
        behind all of them (in-flight launches and re-attaches included).
        The spec lives in `_deferred_stop_audits`
        (popped here on success) so that a shutdown cancelling this worker
        cannot lose the entry — teardown flushes whatever is left after the
        confirmations settle.
        """
        for confirm in pending:
            with contextlib.suppress(Exception):  # a cancelled confirm still frees the stop
                await confirm.wait()
        spec = self._deferred_stop_audits.pop(forward_id, None)
        if spec is not None:
            self._audit_forward("port-forward-stop", spec)

    def _audit_forward(self, action: str, spec: ForwardSpec, *, outcome: str = "success") -> None:
        """Queue a forward audit entry; a single worker drains in FIFO order."""
        if self._audit_log() is None:
            # Forwards are read-only risk profile (issue #38): they stay
            # usable without an audit sink, unlike cluster writes.
            return
        self._enqueue_forward_audit(action, spec, outcome=outcome)
        worker = self._forward_audit_worker
        if worker is None or worker.is_finished:
            self._forward_audit_worker = self._ui.run_worker(self._drain_forward_audits())

    def _enqueue_forward_audit(
        self, action: str, spec: ForwardSpec, *, outcome: str = "success", teardown: bool = False
    ) -> None:
        detail = f"localhost:{spec.local_port} -> {spec.name}:{spec.remote_port}"
        if teardown:
            detail += " (session teardown)"
        # Full GVR: a retargeted forward runs against an apps/batch workload,
        # and the audit schema disambiguates kinds by group (core/audit.py).
        group, version = forward_target_gvr(spec.kind)
        self._forward_audit_queue.append(
            {
                "action": action,
                "kind": spec.kind,
                "namespace": spec.namespace,
                "name": spec.name,
                "group": group,
                "version": version,
                "detail": detail,
                "outcome": outcome,
            }
        )

    async def _drain_forward_audits(self) -> None:
        """Write queued forward audit entries strictly in enqueue order.

        Entries are enqueued only on the event loop, and each write+dequeue
        runs atomically inside a single worker thread under
        `_forward_audit_io_lock`: even if the awaiting drain is cancelled
        mid-write, the thread finishes the pop, so a later flush (the
        unmount path) can neither duplicate the entry nor lose it.
        Append failures are best-effort by design (read-only risk profile):
        a full disk must not kill the app or block the forward.
        """
        audit = self._audit_log()
        if audit is None:
            return
        queue = self._forward_audit_queue

        def _write_head() -> None:
            with self._forward_audit_io_lock:
                if not queue:
                    return
                entry = queue[0]
                try:
                    audit.append(**entry)
                except OSError as exc:
                    logger.warning("forward audit (%s) failed: %s", entry["action"], exc)
                queue.popleft()

        while queue:
            await asyncio.to_thread(_write_head)

    def _open_forward_list(self) -> None:
        """`:pf` — the active-forwards screen with stop / re-attach keys."""
        registry = self._forwards_registry()
        if registry is None:
            self._ui.notify("Port-forward unavailable in this build", severity="warning")
            return

        def _on_stop(record: ForwardRecord) -> None:
            # A stopped broken forward will never poll alive again — drop its
            # id so the broken set does not grow for the session's lifetime.
            self._broken_forwards.discard(record.id)
            # In-flight launches and re-attaches on this record's local port
            # count as pending too: a stop landing in the window between the
            # registry publishing (or adopting) a record and its coroutine
            # resuming has no confirmation to defer behind yet. Other ports'
            # launches are unrelated and must not delay this stop's entry.
            port = record.spec.local_port
            pending: list[Worker[None] | asyncio.Event] = [
                *(w for w, spec in self._launching_forwards.items() if spec.local_port == port),
                *(e for e, spec in self._reattaching_forwards.items() if spec.local_port == port),
                *self._confirming_forwards.get(record.id, ()),
            ]
            if not pending:
                self._audit_forward("port-forward-stop", record.spec)
            else:
                # Start entries are only enqueued as the readiness
                # confirmations resolve — queue this stop behind all of them
                # so the log never shows a stop before any of its starts.
                self._deferred_stop_audits[record.id] = record.spec
                self._ui.run_worker(self._audit_stop_after_confirm(pending, record.id))
            self._ui.notify(f"Stopped forward localhost:{record.spec.local_port}")

        def _on_reattach_error(spec: ForwardSpec, exc: Exception) -> None:
            self._audit_forward("port-forward-start", spec, outcome=f"error: {exc}")

        async def _reattach(record: ForwardRecord, retarget: bool) -> ForwardRecord | None:
            return await self._spawn_reattach(registry, record, retarget=retarget)

        async def _target_exists(record: ForwardRecord) -> bool:
            # Only a confirmed 404 blocks the re-attach; when the target
            # cannot be verified (no fetcher, transport or transient errors)
            # it fails open and lets kubectl report the truth.
            get_manifest = self._get_manifest_fn()
            if get_manifest is None:
                return True
            spec = record.spec
            try:
                await get_manifest(spec.kind, spec.namespace, spec.name)
            except ApiStatusError as exc:
                return exc.status != 404
            except Exception as exc:  # verification is best-effort by design
                logger.debug("re-attach target check failed: %s", exc)
                return True
            return True

        self._ui.push_screen(
            ForwardListScreen(
                registry,
                on_stop=_on_stop,
                reattach=_reattach,
                on_reattach_error=_on_reattach_error,
                target_exists=_target_exists,
            )
        )

    def _poll_forwards(self) -> None:
        """Flag newly broken forwards with a toast (once per breakage)."""
        registry = self._forwards_registry()
        if registry is None:  # pragma: no cover - interval only set when present
            return
        registry.refresh()
        launching_ports = {
            spec.local_port
            for spec in (*self._launching_forwards.values(), *self._reattaching_forwards.values())
        }
        for record in registry.forwards():
            if record.status == "broken" and record.id not in self._broken_forwards:
                if (
                    record.id in self._current_confirmations
                    or record.spec.local_port in launching_ports
                ):
                    # A readiness confirmation — tracked, or still being
                    # installed by an in-flight launch or re-attach on this
                    # record's local port — is about to report this exact
                    # failure with its specific error; the generic breakage
                    # toast must not double it. Other ports' launches are
                    # unrelated and never defer this record's toast.
                    continue
                self._broken_forwards.add(record.id)
                self._ui.notify(
                    f"Port-forward localhost:{record.spec.local_port} ->"
                    f" {record.spec.namespace}/{record.spec.name} broken"
                    " (target gone?) — :pf to re-attach",
                    severity="warning",
                )
            elif record.status == "alive":
                # Re-attached: arm the toast again for the next breakage.
                self._broken_forwards.discard(record.id)
