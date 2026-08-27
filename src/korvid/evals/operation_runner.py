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
import json
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from yaml import YAMLError, safe_load

from korvid.agent.events import AgentError, AgentEvent, TextDelta, ToolCallFinished
from korvid.agent.interaction import InteractionContext, PaneContext, ResourceIdentity
from korvid.agent.model_policy import PolicyEnvironment
from korvid.agent.outbound import sanitize_recorded_tool_result
from korvid.agent.session import DefaultAgentSession
from korvid.evals.__main__ import prompt_fingerprint
from korvid.evals.fake_kube import builtin_aliases
from korvid.evals.harness import NO_GRIND, PromptGrind, build_eval_harness, resolve_eval_policy
from korvid.evals.interaction import EvalUiBridge
from korvid.evals.operation import OperationJourney, OperationTarget, StateAssertion
from korvid.evals.operation_grader import (
    OperationGrade,
    evaluate_assertion,
    evaluate_assertion_document,
    grade_operation,
)
from korvid.evals.operation_journal import (
    ActionJournal,
    JournalTarget,
    summarize_action,
    summarize_arguments,
    summarize_untrusted,
)
from korvid.evals.operation_state import (
    AuditIntentProbe,
    AuditRecord,
    FakeClusterState,
    StatefulFakeKubeClient,
    StatefulFakeWriteOps,
    parse_audit_records,
)
from korvid.evals.runner import _CountingProvider
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import manifest_uid
from korvid.tools.approval import (
    SCRIPTED_POLICY_SOURCE,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalPolicy,
    ApprovalRequest,
    ScriptedApprovalPolicy,
)
from korvid.tools.audit import AuditLog
from korvid.tools.executor import RecordedExecution, ToolExecutor, ToolOutcome, UIBridge
from korvid.tools.registry import TOOLS_BY_NAME
from korvid.tools.write_coordinator import (
    WRITE_VERBS,
    AuditRecorder,
    gvr_label,
    run_approved_write,
    write_locus,
)

#: The one read tool that can earn state credit: it returns the target's
#: own sanitized YAML document, so the result can be parsed and walked.
_STATE_READ_TOOL = "get_resource"

_ALIASES: dict[str, ResourceMeta] = builtin_aliases()

#: Unlike `korvid.evals.harness.EVAL_ENVIRONMENT`, writes are armed here:
#: an operation journey *is* the write path under test, gated only by the
#: injected `ApprovalPolicy` and the real fail-closed `AuditLog` — never
#: by a harness shortcut. Copied from `tests/evals/operation_app.py`'s
#: `_WRITE_ENVIRONMENT` (not imported: that module is test-only).
_WRITE_ENVIRONMENT = PolicyEnvironment(
    readonly=False,
    resize_supported=False,
    observability_backends=frozenset(),
)

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


# ---------------------------------------------------------------------------
# Journaling executor + answer-capturing session
#
# Ported near-verbatim from `tests/evals/operation_app.py`'s
# `_JournalingExecutor`/`_AnswerCapturingSession` and their private helpers:
# every piece below has zero Textual dependency in the source module
# already, so this is a straight port (import paths only), not a rewrite.
# Faithful, not simplified: fixture-grading parity depends on the exact
# event names, actors, and ordering these emit.
# ---------------------------------------------------------------------------


def _read_document(text: str) -> dict[str, Any] | None:
    """The manifest a `get_resource` result showed, or None.

    `get_resource` returns a sanitized YAML document, so the authoritative
    check is a parse and a walk, not a substring. A parse failure or a
    size-elided document that no longer round-trips is *not* evidence the
    model saw the state, so it yields None and earns no credit.
    """
    try:
        document = safe_load(text)
    except YAMLError:
        return None
    return document if isinstance(document, dict) else None


def _is_target_document(
    document: Mapping[str, Any], target: OperationTarget, incarnation: str | None
) -> bool:
    """Whether a parsed read is about the journey's own target object.

    Group/kind/namespace/name must match, and a UID reported by the result
    or parsed document must be the target uid: a same-named replacement
    that happens to carry the asserted value is a different object, and
    reading it is not an observation of the one that was approved.
    """
    metadata = document.get("metadata") or {}
    group, _, _version = str(document.get("apiVersion") or "").rpartition("/")
    document_uid = metadata.get("uid")
    reported_uid = (
        incarnation
        if incarnation is not None
        else (document_uid if isinstance(document_uid, str) and document_uid else None)
    )
    return (
        str(document.get("kind") or "") == target.kind
        and group == target.group
        and str(metadata.get("namespace") or "") == target.namespace
        and str(metadata.get("name") or "") == target.name
        and reported_uid == target.uid
    )


def _shows_state(document: Mapping[str, Any], assertions: Sequence[StateAssertion]) -> bool:
    """Whether the read document carries every assertion's required state.

    Delegates to the grader's own `evaluate_assertion_document`, so the
    walked paths and typed operators cannot drift.
    """
    if not assertions:
        return False
    for assertion in assertions:
        result = evaluate_assertion_document(document, assertion)
        if not result.satisfied:
            return False
    return True


def _safe_argument(arguments: Mapping[str, Any], key: str) -> str | None:
    """Project one untrusted argument through the journal's token policy."""
    value = arguments.get(key)
    if key in {"kind", "name", "namespace"} and isinstance(value, str):
        value = value.strip()
    detail = summarize_untrusted(**{key: value})
    prefix = f"{key}="
    suffix = " dropped=0"
    if not detail.startswith(prefix) or not detail.endswith(suffix):
        return None
    return detail[len(prefix) : -len(suffix)]


def _write_request_target(
    journey: OperationJourney, arguments: Mapping[str, Any]
) -> JournalTarget | None:
    kind = _safe_argument(arguments, "kind")
    name = _safe_argument(arguments, "name")
    namespace = _safe_argument(arguments, "namespace")
    meta = _ALIASES.get(kind.strip().lower()) if kind is not None else None
    if meta is None or name is None or namespace is None:
        return None
    if (
        meta.group != journey.target.group
        or meta.kind != journey.target.kind
        or meta.plural != journey.target.plural
    ):
        return None
    if name != journey.target.name or namespace != journey.target.namespace:
        return None
    return JournalTarget(
        context=journey.target.context,
        namespace=journey.target.namespace,
        group=meta.group,
        kind=meta.kind,
        plural=meta.plural,
        name=journey.target.name,
        uid=None,
    )


def _write_request_state(action: str, arguments: Mapping[str, Any]) -> dict[str, int]:
    replicas = arguments.get("replicas")
    if action != "scale" or isinstance(replicas, bool) or not isinstance(replicas, int):
        return {}
    return {"spec.replicas": replicas}


def _mutation_pending_verification(journal: ActionJournal) -> bool:
    return journal.count("mutation_finished") > journal.count("postcondition_read")


class _OperationJournalingExecutor(RecordedExecution):
    """The real `ToolExecutor`, with model-side journal attribution."""

    def __init__(
        self,
        executor: RecordedExecution,
        journal: ActionJournal,
        journey: OperationJourney,
        *,
        max_result_chars: int | None,
    ) -> None:
        self._executor = executor
        self._journal = journal
        self._journey = journey
        self._target = JournalTarget.of(journey.target)
        self._max_result_chars = max_result_chars
        self.tool_calls = 0

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        return (await self.execute_recorded(name, arguments)).text

    async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        definition = TOOLS_BY_NAME.get(name)
        effect = definition.effect if definition is not None else "unknown"
        journal_name = name if definition is not None else ""
        request_target = (
            _write_request_target(self._journey, arguments) if effect == "cluster_write" else None
        )
        dialogs_before = self._journal.count("approval_observed")
        self.tool_calls += 1
        self._journal.append(
            event="tool_call",
            actor="model_tool",
            action=summarize_action(journal_name),
            detail=summarize_arguments(journal_name, arguments),
        )
        if effect == "cluster_write":
            action = (definition.write_action if definition is not None else None) or name
            self._journal.append(
                event="write_requested",
                actor="model_tool",
                action=action,
                target=request_target,
                post_state=_write_request_state(action, arguments),
                result="requested",
                detail=summarize_arguments(name, arguments),
            )
        outcome = await self._executor.execute_recorded(name, arguments)
        if effect == "cluster_write":
            approval = approval_from_result(outcome.text)
            new_approvals = [
                event for event in self._journal.events if event.event == "approval_observed"
            ][dialogs_before:]
            if approval == "approved" and not any(
                event.approval == "approved" for event in new_approvals
            ):
                approval = "error"
            self._journal.append(
                event="approval_reported",
                actor="model_tool",
                action=name,
                target=request_target,
                approval=approval,
                result="reported",
                detail=summarize_untrusted(tool=name, chars=len(outcome.text)),
            )
        elif effect in {"cluster_read", "external_read"}:
            visible_text, _records = sanitize_recorded_tool_result(
                name,
                outcome.text,
                outcome.redactions,
                max_chars=self._max_result_chars,
                error=outcome.error,
                result_format=definition.result_format if definition is not None else None,
            )
            self._note_read(name, outcome, visible_text)
        return outcome

    def _note_read(self, name: str, outcome: ToolOutcome, visible_text: str) -> None:
        """Journal a model read and decide whether it earns state credit.

        Credit needs *parsed evidence about this object*: a `get_resource`
        whose sanitized YAML parses, whose identity matches the fixture
        target, and whose walked assertion paths are satisfied under the
        grader's own operator semantics. A listing, an events read, a
        failed call, an unparsable or elided document, or a read of a
        same-named replacement is journaled and earns nothing.
        """
        target = self._journey.target
        document = (
            _read_document(visible_text) if name == _STATE_READ_TOOL and not outcome.error else None
        )
        if document is None or not _is_target_document(document, target, outcome.incarnation):
            self._journal.append(
                event="off_target_read",
                actor="model_tool",
                action=name,
                target=self._target,
                result="no_credit",
                detail=summarize_untrusted(tool=name, reason="not_a_target_document"),
            )
            return
        after = _mutation_pending_verification(self._journal)
        assertions = self._journey.postconditions if after else self._journey.preconditions
        shows = _shows_state(document, assertions)
        checkpoint = "postcondition_read" if after else "precondition_read"
        if not after:
            self._journal.append(
                event="target_resolved",
                actor="model_tool",
                action=name,
                target=JournalTarget.of(target, uid=outcome.incarnation or target.uid),
                result="resolved",
                detail=summarize_untrusted(tool=name),
            )
        self._journal.append(
            event=checkpoint if shows else "read_without_state",
            actor="model_tool",
            action=name,
            target=JournalTarget.of(target, uid=outcome.incarnation or target.uid),
            result="credited" if shows else "no_credit",
            detail=summarize_untrusted(tool=name, checkpoint=checkpoint, count=len(assertions)),
            credit=shows,
        )


class _AnswerCapturingSession(DefaultAgentSession):
    """The production session, recording each turn's final assistant text
    and journaling the turn boundary.

    The answer is the text streamed after the last tool call. `turn_started`
    /`turn_finished` are the harness's observable turn signal:
    `turn_finished` is appended in a `finally`, *after* the answer, so a
    wait on it can never observe a finished turn whose answer is not
    captured yet. Interrupted or errored turns end the wait with an empty,
    ungradable answer instead of publishing a partial completion claim.

    `run_turn` stays synchronous, like `DefaultAgentSession.run_turn`: it
    delegates to the base implementation *before* wrapping, so a session
    that is closed or mid-turn still raises at the call, and only the
    journaling/capture below is deferred to the first `__anext__`.
    """

    def __init__(self, *args: Any, journal: ActionJournal, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._journal = journal
        self.answers: list[str] = []

    def run_turn(self, user_text: str) -> AsyncIterator[AgentEvent]:
        return self._capture(user_text, super().run_turn(user_text))

    async def _capture(
        self, user_text: str, events: AsyncIterator[AgentEvent]
    ) -> AsyncIterator[AgentEvent]:
        self._journal.append(
            event="turn_started",
            actor="app_internal",
            detail=summarize_untrusted(chars=len(user_text)),
        )
        buffer = ""
        completed = False
        failed = False
        try:
            async for event in events:
                if isinstance(event, TextDelta):
                    buffer += event.text
                elif isinstance(event, ToolCallFinished):
                    buffer = ""
                elif isinstance(event, AgentError):
                    failed = True
                yield event
            completed = True
        finally:
            answer = buffer if completed and not failed else ""
            self.answers.append(answer)
            self._journal.append(
                event="turn_finished",
                actor="app_internal",
                result="error" if not completed or failed else ("captured" if answer else "empty"),
                detail=summarize_untrusted(chars=len(answer)),
            )


def _read_audit(
    audit_path: Path, *, journal: ActionJournal | None = None
) -> tuple[dict[str, Any], ...]:
    """Read back the persisted audit lines this run's `AuditLog` wrote.

    Ported unchanged from `tests/evals/operation_app.py`'s `_read_audit`.
    """
    if not audit_path.exists():
        return ()
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            malformed += 1
    if malformed and journal is not None:
        journal.append(
            event="audit_unparsable",
            actor="audit",
            result="error",
            detail=summarize_untrusted(count=malformed),
        )
    return tuple(records)


def _audit_result(outcome: str) -> str:
    """Map one persisted audit outcome onto the journal's status vocabulary.

    Ported unchanged from `tests/evals/operation_app.py`'s `_audit_result`.
    """
    if outcome in {"intent", "success", "blocked"}:
        return outcome
    return "error" if outcome else "missing"


def _journal_audit_records(journal: ActionJournal, records: Sequence[dict[str, Any]]) -> None:
    """Journal the persisted audit lines after the run.

    Ported unchanged from `tests/evals/operation_app.py`'s
    `_journal_audit_records`.
    """
    for record in records:
        journal.append(
            event="audit_record",
            actor="audit",
            action=summarize_action(str(record.get("action") or "")),
            result=_audit_result(str(record.get("outcome") or "")),
            detail=summarize_untrusted(
                kind=record.get("kind"),
                name=record.get("name"),
                context=record.get("context"),
            ),
        )


def _journal_grader_reads(
    journal: ActionJournal, state: FakeClusterState, journey: OperationJourney
) -> None:
    """Journal each postcondition read the grader itself performs, so the
    published journal explains where a `grader_read` checkpoint credit
    came from. Ported unchanged from `tests/evals/operation_app.py`'s
    `_journal_grader_reads`."""
    for assertion in journey.postconditions:
        target = assertion.target
        result = evaluate_assertion(state, assertion)
        live_uid = state.uid_of(
            group=target.group, kind=target.kind, namespace=target.namespace, name=target.name
        )
        journal.append(
            event="grader_read",
            actor="grader",
            target=replace(JournalTarget.of(target), uid=live_uid),
            result="found" if result.found else "absent",
            detail=summarize_untrusted(path=assertion.path),
        )


def _default_script(
    journey: OperationJourney, kube: StatefulFakeKubeClient
) -> tuple[list[ApprovalOutcome], list[Callable[[], None] | None]]:
    """Map one fixture's authored `approval`/`dialog_intervention` onto a
    scripted policy's script.

    Every bundled fixture's `expected_approval_dialogs` is 0 or 1 (never
    more) and its `approval` field is a single scalar outcome, so a
    one-element (or empty, for a fixture that never reaches a dialog)
    script always suffices — no bundled fixture needs
    `approval_rerequest_turns` to replay more than one dialog.
    """
    if journey.expected_approval_dialogs == 0 or journey.approval == "none":
        return [], []
    outcome = {
        "approved": ApprovalOutcome.APPROVE,
        "denied": ApprovalOutcome.DECLINE,
        "expired": ApprovalOutcome.EXPIRE,
    }[journey.approval]
    if journey.dialog_intervention is None:
        return [outcome], [None]
    replacement_uid = journey.dialog_intervention.replace_target.uid
    target = journey.target

    def intervene() -> None:
        kube.state.replace_incarnation(
            group=target.group,
            kind=target.kind,
            namespace=target.namespace,
            name=target.name,
            uid=replacement_uid,
        )

    return [outcome], [intervene]


def _operation_interaction(journey: OperationJourney) -> InteractionContext:
    """The static workspace snapshot a TUI-free run presents.

    `initial_selection: neutral` vs `target` models a Textual table
    cursor, which does not exist here — every bundled fixture's scripted
    transcript drives cluster tools directly, never a screen-navigation
    tool, so this always presents the fixture's own target as already
    selected rather than modelling cursor movement.
    """
    target = journey.target
    return InteractionContext(
        kube_context=target.context,
        context_epoch=0,
        focused_pane=PaneContext(
            kind=target.plural,
            scope=target.namespace,
            filter_pattern=None,
            selected=ResourceIdentity(
                kind=target.kind, namespace=target.namespace, name=target.name, uid=target.uid
            ),
        ),
        secondary_pane=None,
        timeline_cursor=None,
    )


@dataclass(frozen=True, slots=True)
class OperationRun:
    """The complete, publishable result of one TUI-free operation-journey run."""

    journey_id: str
    answer: str
    grade: OperationGrade
    journal: tuple[dict[str, Any], ...]
    audit: tuple[dict[str, Any], ...]
    #: One entry per approval decision the scripted policy actually made,
    #: in order — empty for a fixture that never reaches a dialog.
    decisions: tuple[dict[str, str], ...]
    wall_time_s: float
    prompt: dict[str, Any]


async def run_operation_case(
    journey: OperationJourney,
    *,
    audit_path: Path,
    provider_factory: Callable[[], Any],
    approval_script: Sequence[ApprovalOutcome] | None = None,
    model_tier: str | None = None,
    grind: PromptGrind = NO_GRIND,
) -> OperationRun:
    """Run one operation journey end to end, entirely TUI-free, and grade it.

    Builds the exact same production graph
    `korvid.evals.harness.build_eval_harness` composes for the read-only
    scenario/journey runners, over the exact same shared write path a
    Textual run uses (`run_approved_write`, a real `AuditLog`,
    `StatefulFakeWriteOps`) — the only substitution is `ApprovalPolicy`,
    an explicit `ScriptedApprovalPolicy` in place of a `ConfirmScreen`.

    Args:
        journey: the loaded fixture.
        audit_path: where the real `AuditLog` writes; read back for grading.
        provider_factory: builds the LLM provider — `ScriptedProvider` in
            deterministic mode, a configured live provider otherwise.
        approval_script: overrides the fixture's own authored
            `approval`/`dialog_intervention` derivation. `None` (the
            default) derives one scripted outcome from the fixture itself.
        model_tier: `"low"`, `"high"`, or `None` for automatic routing.
        grind: The eval-only prompt levers, composed after the immutable
            safety contract; published in the returned `OperationRun.prompt`.

    Returns:
        The graded run: its answer, journal, persisted audit records,
        approval decisions, wall-clock time, and prompt identity.
    """
    started = time.monotonic()
    kube = StatefulFakeKubeClient(journey.cluster)
    journal = ActionJournal()
    audit = AuditLog(audit_path, context=journey.target.context)
    if approval_script is not None:
        script: list[ApprovalOutcome] = list(approval_script)
        interventions: list[Callable[[], None] | None] = [None] * len(script)
    else:
        script, interventions = _default_script(journey, kube)
    policy = ScriptedApprovalPolicy(script, interventions=interventions)
    bridge = ScriptedOperationBridge(
        kube=kube,
        journal=journal,
        journey=journey,
        audit=audit,
        audit_path=audit_path,
        policy=policy,
    )
    raw_provider = provider_factory()
    provider = _CountingProvider(raw_provider)
    resolved_policy = resolve_eval_policy(
        provider, model_tier=model_tier, environment=_WRITE_ENVIRONMENT
    )
    executor = _OperationJournalingExecutor(
        ToolExecutor(kube, _ALIASES, ui=bridge),
        journal,
        journey,
        max_result_chars=resolved_policy.max_result_chars,
    )
    ui_bridge = EvalUiBridge(_operation_interaction(journey))
    harness = build_eval_harness(
        provider=provider,
        execution=executor,
        bridge=ui_bridge,
        policy=resolved_policy,
        grind=grind,
    )
    session = _AnswerCapturingSession(
        engine=harness.engine,
        bridge=harness.bridge,
        prompt_harness=harness.prompts,
        conversation=harness.conversation,
        gateway=harness.gateway,
        tools=harness.tools,
        policy=harness.policy,
        cluster=harness.cluster,
        user_rules=harness.user_rules,
        journal=journal,
    )
    try:
        for index, text in enumerate(journey.turns):
            journal.append(
                event="user_turn",
                actor="fixture_actor",
                detail=summarize_untrusted(count=index + 1),
            )
            if index + 1 in journey.approval_rerequest_turns:
                journal.append(
                    event="approval_rerequested",
                    actor="fixture_actor",
                    detail=summarize_untrusted(count=index + 1),
                )
            if index == 0:
                journal.append(
                    event="goal_received",
                    actor="fixture_actor",
                    action=journey.goal,
                    detail=summarize_untrusted(chars=len(text)),
                )
            async for _event in session.run_turn(text):
                pass
    finally:
        aclose = getattr(raw_provider, "aclose", None)
        if callable(aclose):
            await aclose()
        await session.aclose()
    answer = session.answers[-1] if session.answers else ""
    journal.append(
        event="outcome_reported",
        actor="model_tool",
        result="captured" if answer else "empty",
        detail=summarize_untrusted(chars=len(answer)),
    )
    audit_records = _read_audit(audit_path, journal=journal)
    _journal_audit_records(journal, audit_records)
    _journal_grader_reads(journal, kube.state, journey)
    grade = grade_operation(
        journey,
        journal,
        kube.state,
        answer,
        tool_calls=executor.tool_calls,
        iterations=provider.completions,
    )
    return OperationRun(
        journey_id=journey.id,
        answer=answer,
        grade=grade,
        journal=tuple(journal.payload()),
        audit=audit_records,
        decisions=tuple(
            {"outcome": outcome.value, "decision_source": SCRIPTED_POLICY_SOURCE}
            for outcome in script
        ),
        wall_time_s=time.monotonic() - started,
        prompt=prompt_fingerprint(resolved_policy, grind=grind),
    )
