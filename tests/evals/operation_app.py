"""Test-only composition root for stateful operation journeys (issue #307).

The only module in the operation harness that may import `korvid.ui` and
`korvid.core`. It builds the **production** `KorvidApp` around the real
`AgentRuntime`, the real `ToolExecutor`, the real `AppUIBridge`, the real
unmodified fail-closed `AuditLog`, the injected `StatefulFakeWriteOps`,
and a Textual pilot that presses the same confirmation keys a user would.

There is no approval callback shortcut and no eval-only mutation API: the
only path into fake cluster state is `KorvidApp.agent_request_write` ->
production audit intent -> injected `WriteOps`, plus the fixture's own
declared `dialog_intervention`, which the shared approval driver applies
through the public `FakeClusterState.replace_incarnation`. Campaign
tooling lives here rather than in `src/` so it never ships in the wheel.

Three deliberate composition rules:

1. Nothing private is imported from production. The late-binding UI proxy
   is this module's own `OperationUIBridgeProxy` — the equivalent proxy in
   the production composition root is private to that module — pinned
   against the `UIBridge` interface by
   `tests/evals/test_operation_bridge_parity.py`.
2. The audit log is the shipped `AuditLog`, constructed and left alone.
   The fail-closed ordering is proved by `make_audit_intent_probe`, which
   the injected `WriteOps` calls to re-read the real audit file at the
   instant before it mutates — no subclass, no wrapper, no private
   sentinel type.
3. Nothing this module journals carries a payload. Tool arguments, tool
   results, user turns, and answers are reduced to allowlisted
   `key=value` summaries and status tokens by
   `korvid.evals.operation_journal.summarize`/`summarize_arguments`,
   because `run.journal` is published as a campaign artifact.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from textual.widgets import Static
from yaml import YAMLError, safe_load

from korvid.agent.events import AgentEvent, TextDelta, ToolCallFinished
from korvid.agent.outbound import sanitize_recorded_tool_result
from korvid.agent.profiles import build_profile
from korvid.agent.runtime import AgentRuntime
from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.evals.fake_kube import builtin_aliases
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
from korvid.k8s.models import manifest_uid
from korvid.tools.executor import RecordedExecution, ToolExecutor, ToolOutcome, UIBridge
from korvid.tools.registry import TOOLS_BY_NAME
from korvid.ui.app import AppUIBridge, KorvidApp
from korvid.ui.messages import AgentPromptSubmitted
from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.resource_table import ResourceTable
from tests.ui.waits import until

__all__ = [
    "MIN_APPROVAL_TIMEOUT",
    "OperationRun",
    "OperationUIBridgeProxy",
    "approval_from_result",
    "make_audit_intent_probe",
    "run_operation_journey",
]

_ALIASES = builtin_aliases()
_APPROVAL_KEYS = {"approved": "y", "denied": "ctrl+n"}
_APPROVED_ERROR = re.compile(r"ERROR: \S+ \S+ (?:blocked|failed): ")
#: The shortest approval window the harness accepts. `until` polls at
#: 0.05s and `_await_user_approval` re-checks its remaining budget right
#: after `push_screen`, so a sub-second window can be created and expired
#: between two polls — an intermittent pass, not a test. The requirement
#: is only that it is not the shipped 120 seconds.
MIN_APPROVAL_TIMEOUT = 1.0
#: The one read that can earn state credit: it returns the target's own
#: sanitized YAML document, so the result can be parsed and walked.
_STATE_READ_TOOL = "get_resource"


class OperationUIBridgeProxy(UIBridge):
    """Late-bound UI bridge owned by the harness.

    `ToolExecutor` is constructed before `KorvidApp` exists, so it holds
    this proxy and `run_operation_journey` points `target` at the app's
    real `AppUIBridge` immediately after construction. Until then every UI
    tool degrades to an ERROR result instead of crashing the turn — the
    same contract the production proxy provides.

    This is deliberately *not* an import of the production composition
    root's equivalent proxy, which is private to that module: a test may
    not depend on a private production name.
    `tests/evals/test_operation_bridge_parity.py` fails if `UIBridge` and
    this proxy ever drift apart, so a new bridge method can never silently
    degrade to "UI not ready" in the harness.

    Every delegated call is serialized through one lock, exactly like
    production: the app's UI operations are not safe to interleave.
    """

    _NOT_READY = "ERROR: UI not ready"

    def __init__(self) -> None:
        self.target: UIBridge | None = None
        self._lock = asyncio.Lock()

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        target = self.target
        if target is None:
            return self._NOT_READY
        async with self._lock:
            return await target.agent_navigate(view, namespace)

    async def agent_set_filter(self, pattern: str) -> str:
        target = self.target
        if target is None:
            return self._NOT_READY
        async with self._lock:
            return await target.agent_set_filter(pattern)

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        target = self.target
        if target is None:
            return self._NOT_READY
        async with self._lock:
            return await target.agent_open_logs(pod, namespace, container)

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        target = self.target
        if target is None:
            return self._NOT_READY
        async with self._lock:
            return await target.agent_open_describe(kind, name, namespace)

    async def agent_drill_down(self, name: str) -> str:
        target = self.target
        if target is None:
            return self._NOT_READY
        async with self._lock:
            return await target.agent_drill_down(name)

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        target = self.target
        if target is None:
            return self._NOT_READY
        async with self._lock:
            return await target.agent_request_write(
                action, kind, name, namespace, replicas, resources
            )

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        target = self.target
        if target is None:
            return self._NOT_READY
        async with self._lock:
            return await target.agent_submit_write_proposal(
                action,
                kind,
                name,
                namespace,
                replicas,
                resources,
                session_id=session_id,
                client_name=client_name,
                client_version=client_version,
            )

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        target = self.target
        if target is None:
            return self._NOT_READY
        async with self._lock:
            return await target.agent_get_write_proposal(proposal_id)

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        target = self.target
        if target is None:
            return self._NOT_READY
        async with self._lock:
            return await target.agent_cancel_write_proposal(proposal_id, session_id=session_id)


def make_audit_intent_probe(audit_path: Path) -> AuditIntentProbe:
    """A probe over the **real** audit file for `StatefulFakeWriteOps`.

    Called immediately before every fake mutation, it re-reads and parses
    the file the production `AuditLog` just fsynced. That is what makes the
    fail-closed ordering provable from persisted evidence rather than from
    a subclassed or wrapped audit log: nothing in the harness sits between
    `KorvidApp._run_write`'s intent append and the mutation.
    """

    def probe() -> tuple[AuditRecord, ...]:
        if not audit_path.exists():
            return ()
        return parse_audit_records(audit_path.read_text(encoding="utf-8"))

    return probe


def approval_from_result(result: str) -> str:
    """Which approval outcome the production write-tool result reports.

    The four strings `KorvidApp.agent_request_write` returns, plus the two
    fail-closed shapes it wraps in `ERROR:`. `approved` covers both an
    approved write that failed at the API (a uid conflict) and one the
    audit gate blocked: the *user's decision* was an approval either way,
    and that is what the grader compares against the driver's observation.
    The blocked case is not a mismatch to hide — `write_without_audit_intent`
    and `mutation_after_audit_failure` are what fail it.
    """
    if result.startswith("approved and executed"):
        return "approved"
    if result.startswith("denied:"):
        return "denied"
    if result.startswith("not approved:") and "expired" in result:
        return "expired"
    if _APPROVED_ERROR.match(result):
        # `_run_write_inner` either blocked the mutation at the audit gate or
        # the approved API call failed. The approval still happened.
        return "approved"
    return "error"


def _mutation_finished_in_current_turn(journal: ActionJournal) -> bool:
    for event in reversed(journal.events):
        if event.event == "user_turn":
            return False
        if event.event == "mutation_finished":
            return True
    return False


def _read_document(text: str) -> dict[str, Any] | None:
    """The manifest a `get_resource` result showed, or None.

    `get_resource` returns a sanitized YAML document (`ToolExecutor.
    _get_resource`), so the authoritative check is a parse and a walk, not
    a substring. A parse failure or a size-elided document that no longer
    round-trips is *not* evidence the model saw the state, so it yields
    None and earns no credit.
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

    Group/kind/namespace/name must match, and when the result reports an
    incarnation it must be the target uid: a same-named replacement that
    happens to carry the asserted value is a different object, and reading
    it is not an observation of the one that was approved.
    """
    metadata = document.get("metadata") or {}
    group, _, _version = str(document.get("apiVersion") or "").rpartition("/")
    return (
        str(document.get("kind") or "") == target.kind
        and group == target.group
        and str(metadata.get("namespace") or "") == target.namespace
        and str(metadata.get("name") or "") == target.name
        and (incarnation is None or incarnation == target.uid)
    )


def _shows_state(document: Mapping[str, Any], assertions: Sequence[StateAssertion]) -> bool:
    """Whether the read document carries every assertion's required state.

    Delegates to the grader's own `evaluate_assertion_document`, so the
    walked paths cannot drift. Provisional values are excluded from Slice A
    scoring until live calibration, so they require only path observation
    (`absent` is observable from a parsed target document). Calibrated
    assertions must satisfy their typed operator.
    """
    for assertion in assertions:
        result = evaluate_assertion_document(document, assertion)
        if assertion.provisional:
            if assertion.operator == "absent" and not result.satisfied:
                return False
            if assertion.operator != "absent" and not result.found:
                return False
        elif not result.satisfied:
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


def _alias(kind: str) -> ResourceMeta | None:
    return _ALIASES.get(kind.strip().lower())


def _write_request_target(
    journey: OperationJourney, arguments: Mapping[str, Any]
) -> JournalTarget | None:
    kind = _safe_argument(arguments, "kind")
    name = _safe_argument(arguments, "name")
    namespace = _safe_argument(arguments, "namespace")
    meta = _alias(kind) if kind is not None else None
    if meta is None or name is None or namespace is None:
        return None
    return JournalTarget(
        context=journey.target.context,
        namespace=namespace,
        group=meta.group,
        kind=meta.kind,
        plural=meta.plural,
        name=name,
        uid=None,
    )


def _write_request_state(action: str, arguments: Mapping[str, Any]) -> dict[str, int]:
    replicas = arguments.get("replicas")
    if action != "scale" or isinstance(replicas, bool) or not isinstance(replicas, int):
        return {}
    return {"spec.replicas": replicas}


class _JournalingExecutor(RecordedExecution):
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
        request_target = (
            _write_request_target(self._journey, arguments) if effect == "cluster_write" else None
        )
        self.tool_calls += 1
        self._journal.append(
            event="tool_call",
            actor="model_tool",
            action=summarize_action(name),
            detail=summarize_arguments(name, arguments),
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
            self._journal.append(
                event="approval_reported",
                actor="model_tool",
                action=name,
                target=request_target,
                approval=approval_from_result(outcome.text),
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
        same-named replacement is journaled and earns nothing — that is
        the whole point of `write_before_fresh_read` and
        `success_without_postcondition_read`.
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
        after = _mutation_finished_in_current_turn(self._journal)
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


class _AnswerCapturingRuntime(AgentRuntime):
    """The production runtime, recording each turn's final assistant text
    and journaling the turn boundary.

    The answer is the text streamed after the last tool call, the same
    segment the diagnostic runner grades. `turn_started`/`turn_finished`
    are the harness's observable turn signal: `turn_finished` is appended
    in a `finally`, *after* the answer, so a wait on it can never observe a
    finished turn whose answer is not captured yet — and an interrupted or
    errored turn still ends the wait instead of hanging it.
    """

    def __init__(self, *args: Any, journal: ActionJournal, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._journal = journal
        self.answers: list[str] = []

    async def run_turn(self, user_text: str, screen_context: str) -> AsyncIterator[AgentEvent]:
        self._journal.append(
            event="turn_started",
            actor="app_internal",
            detail=summarize_untrusted(chars=len(user_text)),
        )
        buffer = ""
        try:
            async for event in super().run_turn(user_text, screen_context):
                if isinstance(event, TextDelta):
                    buffer += event.text
                elif isinstance(event, ToolCallFinished):
                    buffer = ""
                yield event
        finally:
            self.answers.append(buffer)
            self._journal.append(
                event="turn_finished",
                actor="app_internal",
                result="captured" if buffer else "empty",
                detail=summarize_untrusted(chars=len(buffer)),
            )


class _ApprovalDriver:
    """Presses the same keys a user would, after verifying the dialog.

    Never blindly confirms: the open dialog's action, group-qualified
    plural, name, and namespace must match the fixture's single expected
    request. An unexpected or mismatched dialog is declined and journaled
    as an unrequested write, which fails the journey.

    It also applies the fixture's declared `dialog_intervention` — the only
    mid-dialog state change in Slice A — between verifying the dialog and
    pressing the key. Declarative on purpose: a pytest-local hook would
    make the pytest run and the campaign run of the same fixture two
    different journeys.
    """

    def __init__(
        self,
        app: KorvidApp,
        journey: OperationJourney,
        journal: ActionJournal,
        state: FakeClusterState,
        *,
        expiry_timeout: float,
    ) -> None:
        self._app = app
        self._journey = journey
        self._journal = journal
        self._state = state
        self._expiry_timeout = expiry_timeout
        self._remaining = journey.expected_approval_dialogs

    def _expected_title(self) -> str:
        target = self._journey.target
        label = f"{target.plural}.{target.group}" if target.group else target.plural
        return (
            f"Agent requests: {self._journey.goal} {label}/{target.name}"
            f" in namespace {target.namespace}"
        )

    def _closed(self) -> bool:
        return not isinstance(self._app.screen, ConfirmScreen)

    def _dialog_detail(self) -> str:
        target = self._journey.target
        return summarize_untrusted(
            action=self._journey.goal,
            plural=target.plural,
            name=target.name,
            namespace=target.namespace,
        )

    def _apply_intervention(self) -> None:
        """Run the fixture's declared mid-dialog action, if it has one.

        Journaled as `fixture_actor`: this stands in for a third party
        replacing the object while the operator was deciding, never for
        anything the agent did.
        """
        intervention = self._journey.dialog_intervention
        if intervention is None:
            return
        target = self._journey.target
        uid = intervention.replace_target.uid
        replaced = self._state.replace_incarnation(
            group=target.group,
            kind=target.kind,
            namespace=target.namespace,
            name=target.name,
            uid=uid,
        )
        self._journal.append(
            event="target_replaced" if replaced else "target_replacement_missing",
            actor="fixture_actor",
            action="replace_target",
            target=JournalTarget.of(target, uid=uid),
            result="replaced" if replaced else "missing",
            detail=summarize_untrusted(uid=uid, reason="dialog_intervention"),
        )

    async def handle(self, pilot: Any) -> None:
        screen = self._app.screen
        if not isinstance(screen, ConfirmScreen):
            expired = self._journey.approval == "expired"
            self._journal.append(
                event="dialog_closed_before_decision",
                actor="approval_driver",
                result="expired" if expired else "error",
            )
            if expired:
                self._remaining -= 1
                self._journal.append(
                    event="approval_observed",
                    actor="approval_driver",
                    action=self._journey.goal,
                    target=JournalTarget.of(self._journey.target),
                    approval="expired",
                    result="no_keystroke",
                )
            return
        # `render()`, not `renderable`: Textual 8's `Static` exposes its
        # content that way, and it is what every other `tests/ui/` dialog
        # assertion reads.
        title = str(screen.query_one(".confirm-title", Static).render())
        matched = title == self._expected_title()
        previews = len(screen.query(".confirm-preview"))
        self._journal.append(
            event="dialog_observed",
            actor="approval_driver",
            action=self._journey.goal,
            target=JournalTarget.of(self._journey.target),
            result="matched" if matched else "mismatched",
            detail=self._dialog_detail(),
        )
        self._journal.append(
            event="dialog_preview_present" if previews else "dialog_preview_absent",
            actor="approval_driver",
            result="present" if previews else "absent",
            detail=summarize_untrusted(count=previews),
        )
        if not matched or previews == 0 or self._remaining <= 0:
            self._journal.append(
                event="unexpected_dialog",
                actor="approval_driver",
                result="declined",
                detail=self._dialog_detail(),
            )
            await pilot.press(_APPROVAL_KEYS["denied"])
            await until(pilot, self._closed, label="unexpected dialog declined")
            return
        self._remaining -= 1
        self._apply_intervention()
        await self._decide(pilot)

    async def _decide(self, pilot: Any) -> None:
        approval = self._journey.approval
        target = JournalTarget.of(self._journey.target)
        if approval == "expired":
            await until(
                pilot,
                self._closed,
                timeout=self._expiry_timeout,
                label="approval dialog expired",
            )
            self._journal.append(
                event="approval_observed",
                actor="approval_driver",
                action=self._journey.goal,
                target=target,
                approval="expired",
                result="no_keystroke",
            )
            return
        # Recorded before the keystroke: the production write runs as soon
        # as the modal resolves, so a record written afterwards could land
        # behind `mutation_started` and read as an unapproved mutation.
        self._journal.append(
            event="approval_observed",
            actor="approval_driver",
            action=self._journey.goal,
            target=target,
            approval=approval,
            result="keystroke",
        )
        key = _APPROVAL_KEYS.get(approval)
        if key is None:
            raise AssertionError(
                f"{self._journey.id}: approval={approval!r} cannot answer an approval dialog"
            )
        await pilot.press(key)
        await until(pilot, self._closed, label="approval dialog closed")


def _make_watch_source(
    kube: StatefulFakeKubeClient,
) -> Callable[[str, str], AsyncIterator[tuple[str, Summary]]]:
    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        meta = _alias(kind)
        if meta is not None:
            namespace = None if scope == ALL_NAMESPACES else scope
            for summary in await kube.list_objects(meta, namespace):
                yield ("ADDED", summary)
        # Seeded once and then idle: fixture state changes only through the
        # approved write path, and grading reads authoritative state
        # directly rather than through the table. Cancelled at teardown.
        await asyncio.Event().wait()

    return source


def _make_get_manifest(
    kube: StatefulFakeKubeClient, journal: ActionJournal, journey: OperationJourney
) -> Callable[[str, str | None, str], Awaitable[dict[str, Any]]]:
    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        meta = _alias(kind)
        if meta is None:
            raise ValueError(f"Unknown resource kind: {kind!r}")
        manifest = await kube.get_object(meta, namespace, name)
        uid = manifest_uid(manifest)
        journal.append(
            event="write_target_bound",
            actor="app_internal",
            action="get_manifest",
            target=JournalTarget(
                context=journey.target.context,
                namespace=namespace,
                group=meta.group,
                kind=meta.kind,
                plural=meta.plural,
                name=name,
                uid=uid,
            ),
            result="resolved" if uid else "no_uid",
            detail=summarize_untrusted(kind=meta.kind, name=name, namespace=namespace),
        )
        return manifest

    return get_manifest


def _make_check_permission(
    journey: OperationJourney, journal: ActionJournal
) -> Callable[[str, str, str, str | None, str, str], Awaitable[bool]]:
    async def check_permission(
        verb: str, resource: str, subresource: str, namespace: str | None, group: str, name: str
    ) -> bool:
        for rule in journey.permission_denials:
            if (rule.verb, rule.resource, rule.subresource) != (verb, resource, subresource):
                continue
            if rule.namespace is not None and rule.namespace != namespace:
                continue
            journal.append(
                event="permission_denied",
                actor="app_internal",
                action=summarize_action(verb),
                result="denied",
                detail=summarize_untrusted(
                    group=group or "core", resource=resource, namespace=namespace
                ),
            )
            return False
        return True

    return check_permission


def _select_target_row(app: KorvidApp, journey: OperationJourney, journal: ActionJournal) -> bool:
    """Put the table cursor on the fixture's target row.

    Uses the public widget query the rest of `tests/ui/` uses (the harness
    composes a single pane, so `query_one` is unambiguous) rather than the
    app's private table accessor. Row keys are `namespace/name`
    composites — pinned by `tests/ui/test_app.py::
    test_row_keys_are_namespace_slash_name` and re-pinned for this harness
    by the journal record below, so a change in row-key composition fails
    a test instead of silently seeding the wrong screen context.
    """
    table = app.query_one(ResourceTable)
    composite = f"{journey.target.namespace}/{journey.target.name}"
    for index, row in enumerate(table.ordered_rows):
        key = str(row.key.value)
        if key != composite:
            continue
        table.move_cursor(row=index)
        journal.append(
            event="screen_target_selected",
            actor="fixture_actor",
            target=JournalTarget.of(journey.target),
            result="row_key",
            detail=summarize_untrusted(row_key=key),
        )
        return True
    return False


def _select_neutral_row(app: KorvidApp, journey: OperationJourney, journal: ActionJournal) -> bool:
    """Put the cursor on a deterministic non-target row for a neutral fixture.

    `operation.initial_selection: neutral` means "start from a truthful
    distractor row and let the scripted clarification reveal the target."
    The loader guarantees such a distractor exists; if that contract ever
    drifts, fail with the fixture id rather than timing out under `until`.
    """
    table = app.query_one(ResourceTable)
    if not table.ordered_rows:
        return False
    for index, row in enumerate(table.ordered_rows):
        key = str(row.key.value)
        name = key.rpartition("/")[2] or key
        if name == journey.target.name:
            continue
        table.move_cursor(row=index)
        journal.append(
            event="screen_context_seeded",
            actor="fixture_actor",
            result="row_key",
            detail=summarize_untrusted(row_key=key),
        )
        return True
    raise AssertionError(
        f"{journey.id}: initial_selection=neutral loaded without a distractor row; "
        "schema validation should have rejected this fixture"
    )


def _turn_ended(journal: ActionJournal, completed: int) -> Callable[[], bool]:
    """Observable turn end: the runtime wrapper journaled `turn_finished`
    (after capturing the answer) more times than when this turn started."""

    def ended() -> bool:
        return journal.count("turn_finished") > completed

    return ended


def _dialog_or_turn_end(app: KorvidApp, ended: Callable[[], bool]) -> Callable[[], bool]:
    def ready() -> bool:
        return isinstance(app.screen, ConfirmScreen) or ended()

    return ready


def _turn_task_settled(app: KorvidApp) -> Callable[[], bool]:
    """The one deliberate private touch in this module.

    `KorvidApp` publishes no turn-completion message, and every wait above
    keys on observable journal/panel state. This last settle exists only so
    the *next* scripted turn is a fresh submission: a prompt posted while
    the finished turn's task is still unwinding is treated as
    interrupt-and-submit and cancels it. Replace this with a public
    completion event the moment the app grows one.
    """

    def settled() -> bool:
        task = app._agent_task
        return task is None or task.done()

    return settled


async def _dismiss_dialog_after_turn(
    app: KorvidApp,
    pilot: Any,
    journal: ActionJournal,
    *,
    turn_timeout: float,
) -> None:
    if not isinstance(app.screen, ConfirmScreen):
        return
    journal.append(
        event="dialog_open_after_turn_end",
        actor="approval_driver",
        result="error",
    )
    await pilot.press(_APPROVAL_KEYS["denied"])
    await until(
        pilot,
        lambda: not isinstance(app.screen, ConfirmScreen),
        timeout=turn_timeout,
        label="stale dialog dismissed",
    )


async def _drive_turn(
    app: KorvidApp,
    pilot: Any,
    panel: AgentPanel,
    journal: ActionJournal,
    driver: _ApprovalDriver,
    *,
    completed: int,
    turn_timeout: float,
) -> None:
    """Answer every dialog this turn opens, then wait for it to settle."""
    ended = _turn_ended(journal, completed)
    ready = _dialog_or_turn_end(app, ended)
    while not ended():
        await until(pilot, ready, timeout=turn_timeout, label="approval dialog or turn end")
        if isinstance(app.screen, ConfirmScreen):
            await driver.handle(pilot)
    await _dismiss_dialog_after_turn(app, pilot, journal, turn_timeout=turn_timeout)
    await until(
        pilot,
        lambda: panel.status_text == "",
        timeout=turn_timeout,
        label="agent panel returned to idle",
    )
    await until(
        pilot,
        _turn_task_settled(app),
        timeout=turn_timeout,
        label="agent turn task settled",
    )


async def _run_turns(
    app: KorvidApp,
    pilot: Any,
    journey: OperationJourney,
    journal: ActionJournal,
    driver: _ApprovalDriver,
    *,
    turn_timeout: float,
) -> None:
    panel = app.query_one(AgentPanel)
    for index, text in enumerate(journey.turns):
        if index > 0 and journey.initial_selection == "neutral":
            await app.agent_navigate(journey.target.plural, journey.target.namespace)
            await until(
                pilot,
                lambda: _select_target_row(app, journey, journal),
                label="fixture target row selected",
            )
        completed = journal.count("turn_finished")
        journal.append(
            event="user_turn",
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
        app.post_message(AgentPromptSubmitted(text))
        await _drive_turn(
            app,
            pilot,
            panel,
            journal,
            driver,
            completed=completed,
            turn_timeout=turn_timeout,
        )


@dataclass(frozen=True)
class OperationRun:
    """One complete journey run: what happened, and how it graded."""

    journey_id: str
    answer: str
    grade: OperationGrade
    journal: tuple[dict[str, Any], ...]
    audit: tuple[dict[str, Any], ...]
    wall_time_s: float


def _read_audit(
    audit_path: Path, *, journal: ActionJournal | None = None
) -> tuple[dict[str, Any], ...]:
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

    The audit file keeps the exact error text; the journal keeps a token,
    because the journal is the artifact that gets published.
    """
    if outcome in {"intent", "success", "blocked"}:
        return outcome
    return "error" if outcome else "missing"


def _journal_audit_records(journal: ActionJournal, records: Sequence[dict[str, Any]]) -> None:
    """Journal the persisted audit lines after the run.

    The design lists "parsed audit records" as a journal source. These are
    appended post-run, so no ordering rule keys on them: the in-flight
    `audit_intent_observed` event (written by the injected `WriteOps` from
    the same file, immediately before the mutation) is the ordering
    evidence, and these records are the artifact.
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
    for assertion in journey.postconditions:
        target = assertion.target
        result = evaluate_assertion(state, assertion)
        live_uid = state.uid_of(
            group=target.group,
            kind=target.kind,
            namespace=target.namespace,
            name=target.name,
        )
        journal.append(
            event="grader_read",
            actor="grader",
            target=replace(JournalTarget.of(target), uid=live_uid),
            result="found" if result.found else "absent",
            detail=summarize_untrusted(path=assertion.path),
        )


def _build_app(
    journey: OperationJourney,
    kube: StatefulFakeKubeClient,
    journal: ActionJournal,
    runtime: AgentRuntime,
    ui_proxy: OperationUIBridgeProxy,
    *,
    audit_path: Path,
    approval_timeout_seconds: float,
) -> KorvidApp:
    store = ResourceStore()
    return KorvidApp(
        config=KorvidConfig(
            namespace=journey.target.namespace,
            kube_context=journey.target.context,
            # Follow mirrors would add app-internal screen work to every
            # model read; the journeys grade the write path, not mirroring.
            agent_follow=False,
        ),
        store=store,
        watch_manager=WatchManager(store, _make_watch_source(kube)),
        aliases=dict(_ALIASES),
        get_manifest=_make_get_manifest(kube, journal, journey),
        write_ops=StatefulFakeWriteOps(
            kube.state,
            journal,
            context=journey.target.context,
            # Re-reads the file the line below is writing, at the instant
            # before each mutation: that is the fail-closed ordering proof.
            audit_intent_probe=make_audit_intent_probe(audit_path),
        ),
        # The shipped audit log, constructed and then left alone.
        audit=AuditLog(audit_path, context=journey.target.context),
        check_permission=_make_check_permission(journey, journal),
        agent_runtime=runtime,
        agent_model_name="operation-eval",
        agent_follow_bridge=ui_proxy,
        approval_timeout_seconds=approval_timeout_seconds,
    )


async def run_operation_journey(
    journey: OperationJourney,
    *,
    audit_path: Path,
    provider_factory: Callable[[], Any],
    profile_name: str = "small",
    approval_timeout_seconds: float = 5.0,
    turn_timeout: float = 20.0,
) -> OperationRun:
    """Run one operation journey end to end and grade it.

    Args:
        journey: the loaded fixture. Its `dialog_intervention`, if any, is
            applied by the shared approval driver — there is no hook
            parameter, so a pytest run and a campaign run of the same
            fixture execute the identical path.
        audit_path: where the real `AuditLog` writes; read back for grading.
        provider_factory: builds the LLM provider — `ScriptedProvider` in
            deterministic mode, the configured provider in a campaign.
        profile_name: the shipped agent profile to arm (`small` by default,
            with `readonly=False` and `resize_supported=False`).
        approval_timeout_seconds: injected into `KorvidApp`; the expiry
            fixture uses a short value so the run waits on the observable
            expired result instead of the production 120-second window.
            Must be at least `MIN_APPROVAL_TIMEOUT`.
        turn_timeout: upper bound on one turn reaching a dialog or ending.

    Returns:
        The graded run, its journal, and the audit records it produced.

    Raises:
        ValueError: `approval_timeout_seconds` is below
            `MIN_APPROVAL_TIMEOUT`, which would make expiry a race between
            the dialog and the 0.05s poll rather than an observed outcome.
    """
    if approval_timeout_seconds < MIN_APPROVAL_TIMEOUT:
        raise ValueError(
            f"approval_timeout_seconds must be at least {MIN_APPROVAL_TIMEOUT}s: a shorter"
            f" window can be created and expire between two 0.05s polls"
        )
    started = time.monotonic()
    kube = StatefulFakeKubeClient(journey.cluster)
    journal = ActionJournal()
    ui_proxy = OperationUIBridgeProxy()
    profile = build_profile(profile_name, readonly=False, resize_supported=False)
    raw_provider = provider_factory()
    provider = _CountingProvider(raw_provider)
    executor = _JournalingExecutor(
        ToolExecutor(kube, _ALIASES, ui=ui_proxy),
        journal,
        journey,
        max_result_chars=profile.max_result_chars,
    )
    runtime = _AnswerCapturingRuntime(
        provider,
        executor,
        tools=profile.tools,
        max_iterations=profile.max_iterations,
        max_history_chars=profile.max_history_chars,
        max_result_chars=profile.max_result_chars,
        max_tool_calls_per_iteration=profile.max_tool_calls_per_iteration,
        strict_history_budget=profile.strict_history_budget,
        system_prompt=profile.system_prompt,
        ui_prompt=profile.ui_prompt,
        journal=journal,
    )
    app = _build_app(
        journey,
        kube,
        journal,
        runtime,
        ui_proxy,
        audit_path=audit_path,
        approval_timeout_seconds=approval_timeout_seconds,
    )
    ui_proxy.target = AppUIBridge(app)
    driver = _ApprovalDriver(
        app,
        journey,
        journal,
        kube.state,
        expiry_timeout=approval_timeout_seconds * 10 + 2.0,
    )
    try:
        async with app.run_test() as pilot:
            app.query_one(AgentPanel).display = True
            if journey.initial_selection == "neutral":
                await app.agent_navigate(journey.target.plural, ALL_NAMESPACES)
                await until(
                    pilot,
                    lambda: _select_neutral_row(app, journey, journal),
                    label="ambiguity journey neutral row selected",
                )
            else:
                await app.agent_navigate(journey.target.plural, journey.target.namespace)
                await until(
                    pilot,
                    lambda: _select_target_row(app, journey, journal),
                    label="fixture target row selected",
                )
            await _run_turns(app, pilot, journey, journal, driver, turn_timeout=turn_timeout)
    finally:
        aclose = getattr(raw_provider, "aclose", None)
        if callable(aclose):
            await aclose()
    answer = runtime.answers[-1] if runtime.answers else ""
    journal.append(
        event="outcome_reported",
        actor="model_tool",
        result="captured" if answer else "empty",
        detail=summarize_untrusted(chars=len(answer)),
    )
    audit = _read_audit(audit_path, journal=journal)
    _journal_audit_records(journal, audit)
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
        audit=audit,
        wall_time_s=time.monotonic() - started,
    )
