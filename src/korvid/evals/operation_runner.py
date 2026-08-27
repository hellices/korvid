"""A TUI-free operation-journey write bridge.

See `docs/superpowers/specs/2026-08-28-operation-journey-runner-design.md`.
This module reuses the exact same production write path a Textual run
uses — `run_approved_write`, a real `AuditLog`, `StatefulFakeWriteOps` —
through an injected `ApprovalPolicy` instead of a `ConfirmScreen`. No
imports from `tests/`, `korvid.ui`, or `korvid.core`: the runner composes
entirely from `korvid.agent`, `korvid.k8s`, `korvid.evals`, and
`korvid.tools`, matching this package's `tach.toml` boundary unchanged.

`ScriptedOperationBridge` is the only piece of `UIBridge` this runner
implements for real: every `cluster_write`-effect tool the model can call
routes exclusively through `agent_request_write`
(`ToolExecutor._dispatch_write`), and no bundled operation fixture's turns
ever need screen navigation, so every other `UIBridge` method is a lean
"not supported" stub.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from korvid.evals.fake_kube import builtin_aliases
from korvid.evals.operation import OperationJourney
from korvid.evals.operation_journal import ActionJournal, JournalTarget, summarize_untrusted
from korvid.evals.operation_state import (
    AuditIntentProbe,
    AuditRecord,
    StatefulFakeKubeClient,
    StatefulFakeWriteOps,
    parse_audit_records,
)
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import manifest_uid
from korvid.tools.approval import ApprovalDecision, ApprovalOutcome, ApprovalPolicy, ApprovalRequest
from korvid.tools.audit import AuditLog
from korvid.tools.executor import UIBridge
from korvid.tools.write_coordinator import (
    WRITE_VERBS,
    AuditRecorder,
    gvr_label,
    run_approved_write,
    write_locus,
)

_ALIASES: dict[str, ResourceMeta] = builtin_aliases()

#: Every `run_approved_write` failure is surfaced as `f"ERROR: {action}
#: {gvr} {outcome}"`; this pattern recognizes the subset of those that
#: still count as an *approved* decision (the approval was granted, the
#: failure happened afterwards, at the audit gate or the API) rather than
#: a denial — ported unchanged from `tests/evals/operation_app.py`'s
#: `_APPROVED_ERROR` / `approval_from_result`.
_APPROVED_ERROR = re.compile(r"ERROR: \S+ \S+ (?:blocked|failed): ")

#: `ApprovalOutcome` -> the operation-schema's own past-tense approval
#: vocabulary (`korvid.evals.operation.APPROVAL_OUTCOMES`). `DISMISS` has
#: no distinct schema value — no bundled fixture distinguishes a dismissed
#: dialog from a declined one — so it maps to the same "denied" outcome a
#: decline does.
_JOURNAL_APPROVAL = {
    ApprovalOutcome.APPROVE: "approved",
    ApprovalOutcome.DECLINE: "denied",
    ApprovalOutcome.DISMISS: "denied",
    ApprovalOutcome.EXPIRE: "expired",
}

#: `(verb, resource, subresource)` a `PermissionDenial` rule matches on, per
#: write action this runner supports.
_PERMISSION_VERBS = {
    action: (verb, subresource) for action, (verb, subresource) in WRITE_VERBS.items()
}


def approval_from_result(result: str) -> str:
    """Classify one `agent_request_write` return string.

    Ported unchanged from `tests/evals/operation_app.py::approval_from_result`:
    `"approved"`, `"denied"`, `"expired"`, or `"error"` for anything else.
    """
    if result.startswith("approved and executed"):
        return "approved"
    if result.startswith("denied:"):
        return "denied"
    if result.startswith("not approved:") and "expired" in result:
        return "expired"
    if _APPROVED_ERROR.match(result):
        return "approved"
    return "error"


def make_audit_intent_probe(audit_path: Path) -> AuditIntentProbe:
    """A probe over the **real** audit file for `StatefulFakeWriteOps`.

    Ported unchanged from `tests/evals/operation_app.py::make_audit_intent_probe`:
    called immediately before every fake mutation, it re-reads and parses
    the file the production `AuditLog` just wrote, so the fail-closed
    ordering is provable from persisted evidence rather than from a
    subclassed or wrapped audit log.
    """

    def probe() -> tuple[AuditRecord, ...]:
        if not audit_path.exists():
            return ()
        return parse_audit_records(audit_path.read_text(encoding="utf-8"))

    return probe


def _audit_recorder(audit: AuditLog) -> AuditRecorder:
    """Adapt a real `AuditLog` to the `run_approved_write` `AuditRecorder`
    protocol, matching production's own `WriteCoordinator.audit_write`
    field mapping and its synchronous-call-off-the-event-loop pattern."""

    async def record(
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None:
        await asyncio.to_thread(
            lambda: audit.append(
                action=action,
                kind=meta.plural,
                group=meta.group,
                version=meta.version,
                namespace=namespace,
                name=name,
                detail=detail,
                outcome=outcome,
            )
        )

    return record


def _notify(message: str, *, severity: str) -> None:
    """No-op: nothing renders a toast in a TUI-free run."""


def _permission_locus(action: str, meta: ResourceMeta) -> str:
    """`verb resource[/subresource]` as shown in a missing-permission error."""
    verb, subresource = _PERMISSION_VERBS[action]
    target = f"{meta.plural}/{subresource}" if subresource else meta.plural
    return f"{verb} {target}"


class ScriptedOperationBridge(UIBridge):
    """The TUI-free `UIBridge`: only `agent_request_write` does real work.

    Composes the same production write primitives a Textual run uses
    (`run_approved_write`, a real `AuditLog`, `StatefulFakeWriteOps`)
    behind an injected `ApprovalPolicy` — never a bare string or boolean:
    the only thing this bridge ever inspects for a decision is the typed
    `ApprovalDecision` the policy returns.
    """

    def __init__(
        self,
        *,
        kube: StatefulFakeKubeClient,
        journal: ActionJournal,
        journey: OperationJourney,
        audit: AuditLog,
        audit_path: Path,
        policy: ApprovalPolicy,
    ) -> None:
        self._kube = kube
        self._journal = journal
        self._journey = journey
        self._audit = audit
        self._policy = policy
        self._audit_recorder = _audit_recorder(audit)
        self._write_ops = StatefulFakeWriteOps(
            kube.state,
            journal,
            context=journey.target.context,
            audit_intent_probe=make_audit_intent_probe(audit_path),
        )

    # -- unsupported screen actions -----------------------------------
    #
    # No bundled operation fixture drives a turn that needs screen
    # navigation: grading reads authoritative fake-cluster state
    # directly, never through a UI. Each stub below matches the
    # production proxy's own "not wired yet" contract (an ERROR string,
    # never a raised exception, so an unexpected model call still ends
    # the turn instead of crashing the run).

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return "ERROR: navigation is not supported in the TUI-free operation runner"

    async def agent_set_filter(self, pattern: str) -> str:
        return "ERROR: filtering is not supported in the TUI-free operation runner"

    async def agent_open_logs(
        self, pod: str, namespace: str | None, container: str | None = None
    ) -> str:
        return "ERROR: log viewing is not supported in the TUI-free operation runner"

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        return "ERROR: describe is not supported in the TUI-free operation runner"

    async def agent_drill_down(self, name: str) -> str:
        return "ERROR: drill-down is not supported in the TUI-free operation runner"

    async def agent_submit_write_proposal(self, *args: Any, **kwargs: Any) -> str:
        return "ERROR: write proposals are not supported in the TUI-free operation runner"

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        return "ERROR: write proposals are not supported in the TUI-free operation runner"

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        return "ERROR: write proposals are not supported in the TUI-free operation runner"

    # -- the real seam --------------------------------------------------

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        if action not in ("scale", "rollout_restart"):
            return f"ERROR: unsupported write action {action!r} in the TUI-free operation runner"
        meta = _ALIASES.get(kind.strip().lower())
        if meta is None:
            return f"ERROR: unknown kind {kind!r} - not a resource kind in this cluster"
        ns = namespace.strip() or None if namespace is not None else None
        name = name.strip()
        if not self._permitted(action, meta, ns):
            return f"ERROR: missing permission: {_permission_locus(action, meta)}"
        try:
            manifest = await self._kube.get_object(meta, ns, name)
        except ApiStatusError:
            return f"ERROR: {gvr_label(meta)}/{name} not found{write_locus(ns)}"
        uid = manifest_uid(manifest)
        if uid is None:
            return (
                f"ERROR: target identity has no UID for {gvr_label(meta)}/{name}"
                f"{write_locus(ns)}; write blocked"
            )
        journal_target = self._bind_target(meta, ns, name, uid)
        decision = await self._decide(action, meta, ns, name, journal_target)
        if decision.outcome is ApprovalOutcome.EXPIRE:
            return (
                f"not approved: the request expired before the user responded"
                f" ({action} {gvr_label(meta)}/{name})"
            )
        if decision.outcome is not ApprovalOutcome.APPROVE:
            return f"denied: the user declined the {action} request for {gvr_label(meta)}/{name}"
        return await self._execute(action, meta, ns, name, replicas, uid)

    def _bind_target(
        self, meta: ResourceMeta, ns: str | None, name: str, uid: str
    ) -> JournalTarget:
        target = JournalTarget(
            context=self._journey.target.context,
            namespace=ns,
            group=meta.group,
            kind=meta.kind,
            plural=meta.plural,
            name=name,
            uid=uid,
        )
        self._journal.append(
            event="write_target_bound",
            actor="app_internal",
            action="get_manifest",
            target=target,
            result="resolved",
            detail=summarize_untrusted(kind=meta.kind, name=name, namespace=ns or "cluster"),
        )
        return target

    async def _decide(
        self,
        action: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        journal_target: JournalTarget,
    ) -> ApprovalDecision:
        request = ApprovalRequest(
            title=f"Agent requests: {action} {gvr_label(meta)}/{name}{write_locus(ns)}",
            operation=f"{action} {gvr_label(meta)}/{name}",
        )
        decision: ApprovalDecision = await self._policy.decide(request)
        self._journal.append(
            event="approval_observed",
            actor="approval_driver",
            action=action,
            target=journal_target,
            approval=_JOURNAL_APPROVAL[decision.outcome],
            result="no_keystroke" if decision.outcome is ApprovalOutcome.EXPIRE else "keystroke",
        )
        return decision

    async def _execute(
        self,
        action: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        replicas: int | None,
        uid: str,
    ) -> str:
        async def op_factory() -> None:
            if action == "scale":
                await self._write_ops.scale_object(meta, ns, name, replicas or 0, uid=uid)
            else:
                await self._write_ops.rollout_restart(meta, ns, name, uid=uid)

        outcome = await run_approved_write(
            action,
            meta,
            ns,
            name,
            op_factory,
            "requested by agent",
            audit=self._audit_recorder,
            notify=_notify,
        )
        if outcome != "done":
            return f"ERROR: {action} {gvr_label(meta)}/{name} {outcome}"
        return f"approved and executed: {action} {gvr_label(meta)}/{name}"

    def _permitted(self, action: str, meta: ResourceMeta, namespace: str | None) -> bool:
        verb, subresource = _PERMISSION_VERBS[action]
        for rule in self._journey.permission_denials:
            if (rule.verb, rule.resource, rule.subresource) != (verb, meta.plural, subresource):
                continue
            if rule.namespace is not None and rule.namespace != namespace:
                continue
            self._journal.append(
                event="permission_denied",
                actor="app_internal",
                action=action,
                result="denied",
                detail=summarize_untrusted(
                    group=meta.group or "core",
                    resource=meta.plural,
                    namespace=rule.namespace if rule.namespace is not None else "all",
                ),
            )
            return False
        return True
