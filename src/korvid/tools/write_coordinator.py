"""The fail-closed write orchestration, decoupled from Textual (issue TBD).

`run_approved_write` is the shared audit -> mutate -> audit sequence every
cluster mutation passes through once it is approved: a fail-closed intent
audit (if the record cannot persist, the mutation is never attempted), the
mutation itself, and an outcome audit that never un-does a mutation that
already ran. It is a direct extraction of what was
`korvid.ui.write_coordinator.WriteCoordinator._run_write_inner` - the only
change is that `audit`/`notify` arrive as structural ports instead of
`self.audit_write`/`self._ui.notify`, so nothing here needs Textual,
`ViewState`, or a running `KorvidApp` to run.

The write-approval decision that must precede this call - and the
synchronous in-flight reservation, and the `ViewState`/context-epoch
revalidation - stay outside this function on purpose: approval and
revalidation genuinely need the running app's dialog and view model
(`korvid.ui.write_coordinator.WriteCoordinator`), while everything here is
generic to any approved write, from any caller.

`WRITE_VERBS`/`gvr_label`/`write_locus`/`perm_target` moved here alongside
it: they only format a `ResourceMeta`/action pair into a permission-message
or approval-dialog string, with no Textual or `ViewState` dependency at
all. `korvid.ui.write_coordinator` re-exports all four so every existing
import site keeps working unchanged.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError

logger = logging.getLogger(__name__)

#: action -> (verb, subresource) for the SubjectAccessReview pre-check.
WRITE_VERBS: dict[str, tuple[str, str]] = {
    "delete": ("delete", ""),
    "scale": ("patch", "scale"),
    "rollout_restart": ("patch", ""),
    "debug": ("patch", "ephemeralcontainers"),
    "edit": ("update", ""),
    "resize": ("patch", "resize"),
    "install": ("create", ""),
    "approve": ("update", ""),
    # Operator uninstall deletes the Subscription (then its CSV); the
    # pre-check and 403 messages therefore speak in delete terms.
    "uninstall": ("delete", ""),
    # Cordon/uncordon patch node.spec.unschedulable; the drain pre-check
    # covers its cordon step (evictions are per-namespace pod
    # subresource creations that surface individually during execution).
    "cordon": ("patch", ""),
    "uncordon": ("patch", ""),
    "drain": ("patch", ""),
    # Node shell creates a privileged debug pod in the shell namespace
    # (kubectl debug node/, issue #46); the pre-check runs against pods.
    "node-shell": ("create", ""),
}


def gvr_label(meta: ResourceMeta) -> str:
    """Group-qualified plural ('deployments.example.io') so rejection
    messages disambiguate same-plural resources across API groups."""
    return f"{meta.plural}.{meta.group}" if meta.group else meta.plural


def write_locus(ns: str | None) -> str:
    """Namespace qualifier shown in every approval dialog so identically
    named workloads in different namespaces are distinguishable."""
    return f" in namespace {ns}" if ns else " (cluster-scoped)"


def perm_target(action: str, meta: ResourceMeta) -> tuple[str, str]:
    """(verb, resource[/subresource]) as shown in permission messages."""
    verb, subresource = WRITE_VERBS[action]
    target = f"{meta.plural}/{subresource}" if subresource else meta.plural
    return verb, target


Severity = Literal["information", "warning", "error"]


class Notifier(Protocol):
    """A toast to the user - the only two arguments `run_approved_write` uses."""

    def __call__(self, message: str, *, severity: Severity) -> None: ...


class AuditRecorder(Protocol):
    """Append one audit record; raises if it cannot be persisted.

    Matches `korvid.ui.write_coordinator.WriteCoordinator.audit_write`'s
    bound-method signature exactly, so production passes it directly with
    no adapter.
    """

    async def __call__(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None: ...


async def run_approved_write(
    action: str,
    meta: ResourceMeta,
    namespace: str | None,
    name: str,
    op_factory: Callable[[], Awaitable[None]],
    detail: str,
    *,
    audit: AuditRecorder,
    notify: Notifier,
) -> str:
    """Fail-closed intent audit -> mutation -> outcome audit.

    Returns a short outcome string for callers that report back: 'done',
    'blocked: ...', or 'failed: ...'. Takes an operation *factory* so the
    mutation coroutine is never created until intent is audited - a
    declined or blocked write produces no unawaited coroutine to leak.
    """
    kind = meta.plural
    try:
        await audit(action, meta, namespace, name, detail, "intent")
    except Exception as exc:
        # Factory was never called - no coroutine to leak.
        logger.exception("audit intent record failed; write blocked: %s", exc)
        notify(f"{action} {kind}/{name} blocked: audit log unavailable", severity="error")
        return "blocked: audit log unavailable"
    try:
        await op_factory()
    except ApiStatusError as exc:
        with contextlib.suppress(Exception):
            await audit(action, meta, namespace, name, detail, f"error: {exc}")
        if exc.status == 403:
            # The SSAR pre-check fails open and permissions can change
            # mid-flight: keep the actionable RBAC message contract
            # instead of a bare "API 403: Forbidden".
            verb, target = perm_target(action, meta)
            message = f"missing permission: {verb} {target}"
        elif exc.status == 409:
            # The uid precondition tripped: the object was deleted and
            # recreated (or otherwise changed) after the approval was
            # given - nothing was modified.
            message = "conflict: the target changed since it was approved - refresh and retry"
        else:
            message = str(exc)
        notify(f"{action} {kind}/{name} failed: {message}", severity="error")
        return f"failed: {message}"
    except Exception as exc:
        with contextlib.suppress(Exception):
            await audit(action, meta, namespace, name, detail, f"error: {exc}")
        notify(f"{action} {kind}/{name} failed: {exc}", severity="error")
        return f"failed: {exc}"
    try:
        await audit(action, meta, namespace, name, detail, "success")
    except Exception:
        logger.exception("audit outcome record failed after successful write")
        notify("Audit log write failed (operation already executed)", severity="warning")
    notify(f"{action} {kind}/{name}: done", severity="information")
    return "done"
