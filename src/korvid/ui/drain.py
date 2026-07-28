"""Drain execution lifecycle, extracted from the app (issue #97 U3d).

`DrainController` owns the post-approval half of the node drain flow:
fail-closed intent auditing, cordoning, re-checking the approved plan
(pods may have landed while the dialog was open), pod-by-pod eviction
with live progress and PDB/throttle handling, the bounded wait for
accepted evictions to actually terminate, and the outcome audit.

Keybinding routing, the press-again-to-cancel semantics (`_drain_worker`),
the approval dialog, and the `@_tracks_cluster_write` wrapper stay on the
app — it hands this controller narrow callables instead of itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from time import monotonic

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.drain import DrainPlan, DrainTarget, is_pdb_denial
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.writes import WriteOps

logger = logging.getLogger(__name__)

#: `_audit_write` shape the app injects: (action, meta, namespace, name,
#: detail, outcome) -> awaitable; raises when the record cannot persist.
AuditWrite = Callable[[str, ResourceMeta, None, str, str, str], Awaitable[None]]


class DrainController:
    """Owns the approved drain's execution, auditing, and progress."""

    def __init__(
        self,
        *,
        notify: Callable[..., None],
        audit_write: AuditWrite,
        set_progress: Callable[[str], None],
    ) -> None:
        self._notify = notify
        self._audit_write = audit_write
        self._set_progress = set_progress
        # Bounded post-eviction termination wait (tests shrink these).
        self.wait_timeout: float = 120.0
        self.wait_poll: float = 2.0
        # Bounded retry for throttled (non-PDB) 429s during eviction.
        self.throttle_retries: int = 2
        self.throttle_backoff: float = 1.0
        # How long a cancelled drain waits for an in-flight eviction POST
        # to settle before giving up on counting it.
        self.settle_timeout: float = 5.0

    async def run(
        self,
        ops: WriteOps,
        meta: ResourceMeta,
        name: str,
        uid: str | None,
        plan: DrainPlan,
    ) -> None:
        """Execute an approved drain: cordon, re-check the plan (pods may
        have landed while the dialog was open), evict pod by pod, then wait
        (bounded) for the accepted evictions to actually terminate.
        Auditing is fail-closed (no intent record, no drain); a PDB-refused
        eviction (429) surfaces as a live warning and the drain moves on;
        cancellation anywhere after the intent record is audited, stops
        issuing evictions, and never uncordons the node."""
        total = len(plan.targets)
        counts = {"evicted": 0, "blocked": 0, "failed": 0, "still": 0}
        try:
            await self._audit_write(
                "drain", meta, None, name, f"planned evictions: {total}", "intent"
            )
        except Exception as exc:
            logger.exception("audit intent record failed; drain blocked: %s", exc)
            self._notify(f"drain nodes/{name} blocked: audit log unavailable", severity="error")
            return
        try:
            try:
                await ops.cordon_node(name, True, uid=uid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await self._audit_write(
                        "drain", meta, None, name, "cordon step failed", f"error: {exc}"
                    )
                self._notify(f"drain nodes/{name} failed: cordon: {exc}", severity="error")
                return
            targets = await self._recheck_plan(ops, meta, name, plan)
            if targets is None:
                return
            total = len(targets)
            self._notify(
                f"drain nodes/{name}: cordoned; evicting {total} pods"
                " (press the drain key again to cancel)",
                severity="information",
            )
            await self._evict_targets(ops, name, targets, counts)
        except asyncio.CancelledError:
            summary = f"cancelled: evicted {counts['evicted']} of {total}; node was not uncordoned"
            with contextlib.suppress(Exception):
                await self._audit_write("drain", meta, None, name, summary, "cancelled")
            self._notify(f"drain nodes/{name} {summary}", severity="warning")
            raise
        summary = (
            f"evicted {counts['evicted']}, pdb-blocked {counts['blocked']},"
            f" failed {counts['failed']} of {total}"
        )
        if counts["still"]:
            summary += f"; {counts['still']} evicted pods not yet terminated"
        clean = counts["blocked"] == 0 and counts["failed"] == 0 and counts["still"] == 0
        outcome = "success" if clean else f"partial: {summary}"
        try:
            await self._audit_write("drain", meta, None, name, summary, outcome)
        except Exception:
            logger.exception("audit outcome record failed after drain")
            self._notify("Audit log write failed (drain already executed)", severity="warning")
        if clean:
            self._notify(f"drain nodes/{name}: {summary}")
        else:
            self._notify(f"drain nodes/{name}: {summary}", severity="warning")

    async def _recheck_plan(
        self,
        ops: WriteOps,
        meta: ResourceMeta,
        name: str,
        plan: DrainPlan,
    ) -> tuple[DrainTarget, ...] | None:
        """Re-list the node's pods right after cordoning: the node was still
        schedulable while the user reviewed the plan, so pods may have landed
        since. Pods that disappeared are simply dropped; pods the user never
        approved abort the drain (the node stays cordoned, so a re-run shows
        a stable plan to re-approve). None means the drain must not proceed
        (already audited and notified)."""
        try:
            fresh = await ops.drain_plan(name)
        except Exception as exc:
            summary = f"plan re-check failed after cordon: {exc}"
            with contextlib.suppress(Exception):
                await self._audit_write("drain", meta, None, name, summary, "aborted")
            self._notify(
                f"drain nodes/{name} aborted: {summary}; node remains cordoned",
                severity="error",
            )
            return None
        approved = {t.uid or t.ref for t in plan.targets}
        unapproved = [t.ref for t in fresh.targets if (t.uid or t.ref) not in approved]
        if unapproved:
            summary = (
                f"plan changed after cordon: {len(unapproved)} unapproved pods"
                f" ({', '.join(unapproved[:3])})"
            )
            with contextlib.suppress(Exception):
                await self._audit_write("drain", meta, None, name, summary, "aborted")
            self._notify(
                f"drain nodes/{name} aborted: {summary};"
                " node remains cordoned - re-run drain to review the updated plan",
                severity="warning",
            )
            return None
        return fresh.targets

    async def _evict_targets(
        self,
        ops: WriteOps,
        name: str,
        targets: tuple[DrainTarget, ...],
        counts: dict[str, int],
    ) -> None:
        """Evict *targets* one by one with live progress, then wait for the
        accepted evictions' pods to leave the node. Mutates *counts* in
        place so a cancellation mid-loop still leaves accurate numbers for
        the caller's audit record."""
        total = len(targets)
        accepted: list[DrainTarget] = []
        try:
            for done, target in enumerate(targets, start=1):
                self._set_progress(f"drain {name}: {done - 1}/{total}")
                result = await self._evict_one(ops, target, counts)
                if result == "evicted":
                    accepted.append(target)
                counts[result] += 1
                self._set_progress(f"drain {name}: {done}/{total}")
            if accepted:
                keys = {t.uid or t.ref for t in accepted}
                counts["still"] = await self._await_evictions(ops, name, keys)
        finally:
            self._set_progress("")

    async def _evict_one(self, ops: WriteOps, target: DrainTarget, counts: dict[str, int]) -> str:
        """Issue one eviction; returns 'evicted', 'blocked' (PDB admission
        denial - warn live and move on instead of hanging on a budget that
        may never free) or 'failed'. A 429 *without* the PDB denial markers
        is apiserver throttling (API Priority and Fairness): retried with
        bounded backoff instead of being misreported as pdb-blocked.
        Cancellation lets the in-flight request settle first (the POST may
        already have reached the apiserver) so the cancellation audit
        reports what actually happened, then propagates."""
        for attempt in range(self.throttle_retries + 1):
            request = asyncio.ensure_future(
                ops.evict_pod(target.namespace, target.name, uid=target.uid)
            )
            try:
                await asyncio.shield(request)
            except asyncio.CancelledError:
                await self._settle_cancelled_eviction(request, counts)
                raise
            except ApiStatusError as exc:
                if is_pdb_denial(exc):
                    self._notify(
                        f"eviction refused by PodDisruptionBudget: {target.ref}",
                        severity="warning",
                    )
                    return "blocked"
                if exc.status == 429 and attempt < self.throttle_retries:
                    await asyncio.sleep(self.throttle_backoff * (attempt + 1))
                    continue
                self._notify(f"eviction failed: {target.ref}: {exc}", severity="error")
                return "failed"
            except Exception as exc:
                self._notify(f"eviction failed: {target.ref}: {exc}", severity="error")
                return "failed"
            return "evicted"
        return "failed"  # not reached: the last attempt returns above

    async def _settle_cancelled_eviction(
        self, request: asyncio.Future[None], counts: dict[str, int]
    ) -> None:
        """The drain was cancelled while an eviction POST was in flight -
        the request may already have reached the apiserver. Wait (bounded)
        for it to settle and count a landed eviction, so the cancellation
        audit records what actually happened instead of assuming the
        eviction never went out."""
        try:
            await asyncio.wait_for(asyncio.shield(request), timeout=self.settle_timeout)
        except TimeoutError:
            request.cancel()
            return
        except asyncio.CancelledError:
            request.cancel()
            raise
        except Exception:
            return  # refused or failed: nothing landed
        counts["evicted"] += 1

    async def _await_evictions(self, ops: WriteOps, name: str, keys: set[str]) -> int:
        """A 201 from the Eviction API only *starts* graceful deletion: the
        pod can linger for its grace period, a finalizer, or an unreachable
        kubelet. Poll the node's pod list until the accepted pods are gone
        (bounded) so progress and the audit outcome reflect reality; returns
        how many are still present at the deadline (audited as partial)."""
        deadline = monotonic() + self.wait_timeout
        while True:
            try:
                present = await ops.pods_on_node(name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("post-eviction termination poll failed", exc_info=True)
                return len(keys)
            keys &= set(present)
            if not keys:
                return 0
            if monotonic() >= deadline:
                return len(keys)
            self._set_progress(f"drain {name}: waiting for {len(keys)} pods to terminate")
            await asyncio.sleep(self.wait_poll)
