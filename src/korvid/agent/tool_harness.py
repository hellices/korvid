"""Policy-aware tool execution harness for the agent engine (issue #316, Task 9).

One seam sits between the agent engine and the ports a tool call may reach:
the recorded tool executor and the typed UI bridge. Concentrating the routing
here keeps the engine (Task 10) free of tool classification and keeps the
security-relevant decisions — which tool is armed, which port it may reach,
whether its result is evidence, how many calls one iteration may make — in a
single place that reads them off the registry rather than re-deriving them.

Routing is by the registry's validated *effect*, never a second hard-coded
list:

- `cluster_read` / `external_read` / `cluster_write` reach only
  `RecordedExecution.execute_recorded`. Writes keep the executor's
  approval / audit / revalidation path; the harness holds no raw
  Kubernetes or write object and never retries or replays a write.
- `ui_only` tools become closed Task-1 `UiAction` values and are applied
  through `AgentUiBridge.apply`. Writes are never routed through that bridge.

Every result is sanitized exactly once through
`sanitize_recorded_tool_result`, using the registry's result format and the
policy's result budget, into a copy-owned `ToolOutcome`. Only a successful
cluster or external read mints evidence, from the *sanitized* text, so a
citation's excerpt matches what the model was shown.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from korvid.agent.evidence import EvidenceLedger
from korvid.agent.interaction import (
    AgentUiBridge,
    DrillDown,
    Navigate,
    OpenDescribe,
    OpenLogs,
    SetFilter,
    UiAction,
    UiActionResult,
)
from korvid.agent.model_policy import ResolvedAgentPolicy
from korvid.agent.outbound import ToolResultBlockedError, sanitize_recorded_tool_result
from korvid.agent.prompt_harness import interaction_context_note_with_redactions
from korvid.core.redaction import RedactionRecord
from korvid.tools.executor import (
    MAX_RESULT_CHARS,
    RecordedExecution,
    ToolOutcome,
    ToolResultBlocked,
    cap_result,
    compact_result,
)
from korvid.tools.registry import ToolDef, tool_def, tool_schema_names

#: Effects whose successful results a claim may cite. A screen action or a
#: write is not an observation someone can go and check, however successful
#: it reports itself, so it never mints evidence (issue #192).
_EVIDENCE_EFFECTS: frozenset[str] = frozenset({"cluster_read", "external_read"})


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """The immutable outcome of dispatching one tool call through the harness.

    Attributes:
        call_id: The provider's id for this tool call, echoed back so the
            engine can pair the result with the request that made it.
        name: The tool name exactly as the model called it.
        outcome: The copy-owned, sanitized result (text, merged redaction
            trail, error verdict, and read identity).
        evidence_ref: The reference minted for a successful read (`E<n>`),
            or None for a UI action, a write, an error, or a rejected call.
    """

    call_id: str
    name: str
    outcome: ToolOutcome
    evidence_ref: str | None


class ToolHarness:
    """Routes one tool call to its port under the resolved agent policy.

    The harness owns two pieces of per-run state: the context epoch the
    current turn's evidence belongs to, and the number of tool calls made in
    the current iteration. `reset_evidence` starts a turn (clearing the
    ledger); `begin_iteration` starts an iteration (clearing the call count).

    Args:
        policy: The resolved routing policy. Its `tools` schemas are the
            only armed surface, and its `max_tool_calls_per_iteration` and
            `max_result_chars` bound execution and result size.
        execution: The recorded tool executor. Cluster/external reads and
            every write dispatch here; writes keep its approval gate.
        bridge: The typed UI bridge. UI-drive tools apply here as
            `UiAction` values; nothing else reaches it.
        evidence: The turn-scoped ledger successful reads mint into.
    """

    def __init__(
        self,
        *,
        policy: ResolvedAgentPolicy,
        execution: RecordedExecution,
        bridge: AgentUiBridge,
        evidence: EvidenceLedger,
    ) -> None:
        self._execution = execution
        self._bridge = bridge
        self._evidence = evidence
        armed, max_calls, max_result_chars = _armed_surface(policy)
        #: Names come only from the policy's own schemas, so a tool the
        #: registry defines but this policy did not arm is unreachable.
        self._armed: frozenset[str] = armed
        self._max_calls = max_calls
        self._max_result_chars = max_result_chars
        self._context_epoch = 0
        self._calls_this_iteration = 0

    @staticmethod
    def validate_policy(policy: ResolvedAgentPolicy) -> None:
        """Check that `policy`'s armed surface is executable, without a harness.

        Every armed name must resolve to a registry definition under its
        *exact* name: routing, result format, and evidence eligibility all
        come from that definition, so an armed name the registry does not
        define could only ever fail at call time, mid-turn, in front of the
        user. `AgentSession` (issue #316 task 11) calls this before it
        swaps a live session's policy so the refusal happens while the old
        surface is still armed and intact.

        Args:
            policy: The resolved policy whose `tools` are the armed surface.

        Raises:
            ValueError: A schema is malformed, or an armed name has no
                registry definition.
        """
        _armed_surface(policy)

    def retarget(self, policy: ResolvedAgentPolicy) -> None:
        """Install a new armed surface and its budgets on this same harness.

        Identity is preserved deliberately: the engine holds this object
        for its whole life, so re-pointing it here is what lets a context
        switch change the armed tools without rebuilding the engine, and
        guarantees the very next call the engine routes sees the new
        surface. Validation happens entirely in locals, so a refusal
        leaves the previous surface armed and callable.

        The in-flight per-iteration call count is untouched: this is only
        ever called between turns, and clearing it here would hand a
        mid-iteration caller a fresh budget.

        Args:
            policy: The resolved policy to arm.

        Raises:
            ValueError: A schema is malformed, or an armed name has no
                registry definition. Nothing is changed in that case.
        """
        armed, max_calls, max_result_chars = _armed_surface(policy)
        self._armed = armed
        self._max_calls = max_calls
        self._max_result_chars = max_result_chars

    @property
    def evidence(self) -> EvidenceLedger:
        """The turn-scoped evidence ledger this harness mints into."""
        return self._evidence

    @property
    def context_epoch(self) -> int:
        """The context epoch the current turn's evidence belongs to."""
        return self._context_epoch

    def begin_iteration(self) -> None:
        """Start a new iteration, resetting the per-iteration call budget."""
        self._calls_this_iteration = 0

    def reset_evidence(self, context_epoch: int) -> None:
        """Start a new turn: drop the previous turn's evidence and re-epoch.

        A citation must resolve to a read fetched now, not to a stale read
        from an earlier turn whose resource may since have changed, so the
        ledger is cleared on every turn and epoch change (issue #192).
        """
        self._context_epoch = context_epoch
        self._evidence.start_turn()

    def clear_evidence(self) -> None:
        """Drop what the current surface minted, without claiming an epoch.

        The retarget counterpart of `reset_evidence`. A citation read
        against the cluster we just left must stop resolving *immediately*
        — including for a screen still rendering the answer that cited it
        — but the epoch that evidence belongs to is a property of the live
        workspace, which only a starting turn reads. So this clears and
        says nothing about the epoch; the next turn's `reset_evidence`
        supplies the real one.
        """
        self._evidence.start_turn()

    async def execute(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolExecution:
        """Route one tool call to its port and return the sanitized outcome.

        The per-iteration budget is checked before any port, so a low-tier
        iteration never dispatches more calls than the policy permits. An
        unarmed or unknown call returns a bounded deterministic error
        without touching the executor or the bridge, and — like a
        `reject` — spends none of that budget: the model still owes one
        result per stored call, and a correction it cannot act on is no
        correction. Arguments are copied before dispatch so neither the
        executor nor the evidence ledger can be handed the caller's
        mutable dict.

        Raises:
            OutboundPolicyError: the result could not be sanitized safely
                (fail-closed), stopping the turn before its next request.
        """
        arguments = copy.deepcopy(arguments)
        if self._over_budget():
            return self._error(
                call_id, name, "iteration tool-call budget exhausted; no further calls this step"
            )
        if name not in self._armed:
            return self._error(call_id, name, f"tool {name!r} is not armed for this policy")
        definition = tool_def(name)
        if definition is None:  # defensive: an armed built-in always resolves
            return self._error(call_id, name, f"unknown tool {name!r}")
        # Counted here, after the name resolves: a refusal touches no port
        # and does no work, so charging the iteration for it would let a
        # model that guesses two wrong names spend a low-tier step and be
        # refused for budget on the corrected call it was just told to
        # make. Same rule as `reject`.
        self._calls_this_iteration += 1
        if definition.effect == "ui_only":
            return await self._run_ui(call_id, definition, arguments)
        return await self._run_recorded(call_id, definition, arguments)

    def reject(self, call_id: str, name: str, reason: str) -> ToolExecution:
        """Answer a call the engine refuses to dispatch, touching no port.

        The engine filters calls the protocol cannot use — unusable
        arguments above all — and the model still needs one result per
        stored call to correct itself from. Routing those refusals through
        here keeps a single result-error format and a single bound: a
        rejection is *not* a dispatch, so it spends no per-iteration
        budget, mints no evidence, and never reaches the executor or the
        bridge.

        Args:
            call_id: The provider's id for the call being refused.
            name: The tool name exactly as the model called it.
            reason: Why the call cannot run, in the model's terms.

        Returns:
            A bounded error execution, shaped exactly like every other
            deterministic refusal this harness produces.
        """
        return self._error(call_id, name, reason)

    def _over_budget(self) -> bool:
        return self._max_calls is not None and self._calls_this_iteration >= self._max_calls

    async def _run_recorded(
        self, call_id: str, definition: ToolDef, arguments: dict[str, Any]
    ) -> ToolExecution:
        """Dispatch a read or write through the recorded executor only."""
        try:
            produced = await self._execution.execute_recorded(definition.name, arguments)
        except ToolResultBlocked as exc:
            # The executor could not vouch for its own result, so the turn
            # stops here rather than sending an unvetted document onward.
            raise ToolResultBlockedError(str(exc)) from exc
        outcome = self._sanitize(definition, produced)
        ref: str | None = None
        if definition.effect in _EVIDENCE_EFFECTS and not outcome.error:
            ref = self._evidence.record(
                definition.name,
                arguments,
                outcome.text,
                error=outcome.error,
                incarnation=outcome.incarnation,
                container=outcome.container,
            )
        return ToolExecution(
            call_id=call_id, name=definition.name, outcome=outcome, evidence_ref=ref
        )

    async def _run_ui(
        self, call_id: str, definition: ToolDef, arguments: dict[str, Any]
    ) -> ToolExecution:
        """Apply a UI-drive tool as a typed action; UI actions are not evidence."""
        try:
            action = _ui_action(definition, arguments)
        except ValueError as exc:
            return self._error(call_id, definition.name, f"invalid arguments: {exc}")
        result = await self._bridge.apply(action)
        outcome = self._sanitize_ui_result(definition, result)
        return ToolExecution(
            call_id=call_id, name=definition.name, outcome=outcome, evidence_ref=None
        )

    def _sanitize_ui_result(
        self,
        definition: ToolDef,
        result: UiActionResult,
    ) -> ToolOutcome:
        """Preserve post-action context as valid JSON inside the result cap."""
        limit = self._max_result_chars or MAX_RESULT_CHARS
        context_budget = max(256, limit * 2 // 3)
        context_text, context_redactions = interaction_context_note_with_redactions(
            result.context,
            max_chars=context_budget,
        )
        separator = "\n\n"
        message_budget = max(limit - len(context_text) - len(separator), 0)
        message_text = ""
        message_redactions: tuple[RedactionRecord, ...] = ()
        if message_budget:
            message_text, message_redactions = sanitize_recorded_tool_result(
                definition.name,
                result.message,
                (),
                max_chars=message_budget,
                error=not result.ok,
                result_format=definition.result_format,
            )
        text = f"{message_text}{separator if message_text else ''}{context_text}"
        return ToolOutcome(
            text=text,
            redactions=(*message_redactions, *context_redactions),
            error=not result.ok,
        )

    def _sanitize(self, definition: ToolDef, produced: ToolOutcome) -> ToolOutcome:
        """Sanitize once and return a copy-owned outcome.

        The result is bounded and redacted in its registry format, the
        producer's redaction trail is merged with this pass's, and the read
        identity (`incarnation`, `container`) is carried through unchanged.
        """
        text, redactions = sanitize_recorded_tool_result(
            definition.name,
            produced.text,
            produced.redactions,
            max_chars=self._max_result_chars,
            error=produced.error,
            result_format=definition.result_format,
        )
        return ToolOutcome(
            text=text,
            redactions=redactions,
            error=produced.error,
            incarnation=produced.incarnation,
            container=produced.container,
        )

    def _error(self, call_id: str, name: str, message: str) -> ToolExecution:
        """A bounded deterministic error outcome that touches no port."""
        return ToolExecution(
            call_id=call_id,
            name=name,
            outcome=ToolOutcome(text=self.cap_text(f"ERROR: {message}"), error=True),
            evidence_ref=None,
        )

    def cap_text(self, text: str, *, suffix: str = "") -> str:
        """Bound text to the policy result budget while retaining a suffix."""
        limit = self._max_result_chars or MAX_RESULT_CHARS
        if not suffix:
            return cap_result(text, limit=limit)
        if len(suffix) >= limit:
            return cap_result(suffix, limit=limit)
        return compact_result(text, limit=limit - len(suffix)) + suffix


def _armed_surface(
    policy: ResolvedAgentPolicy,
) -> tuple[frozenset[str], int | None, int | None]:
    """Derive the armed names and budgets a policy asks for, or refuse it.

    Kept module-level and pure so construction, `validate_policy`, and
    `retarget` cannot drift into three slightly different notions of what
    an executable surface is.

    Raises:
        ValueError: A schema is malformed, or an armed name has no
            registry definition under that exact name.
    """
    names = tool_schema_names(list(policy.tools))
    unknown = [name for name in names if tool_def(name) is None]
    if unknown:
        raise ValueError(
            "armed tools are not defined by the tool registry: " + ", ".join(sorted(unknown))
        )
    return (
        frozenset(names),
        policy.max_tool_calls_per_iteration,
        policy.max_result_chars,
    )


def _ui_action(definition: ToolDef, arguments: dict[str, Any]) -> UiAction:
    """Parse a UI-drive tool call into a closed Task-1 `UiAction`.

    Keyed on the registry's validated dispatch target, not the tool name, so
    a definition can never silently map to a different action. Invalid
    arguments raise `ValueError`, which the caller turns into one bounded
    deterministic error.
    """
    dispatch = definition.dispatch
    if dispatch == "agent_navigate":
        return Navigate(
            view=_require_str(arguments, "view"), namespace=_optional_str(arguments, "namespace")
        )
    if dispatch == "agent_set_filter":
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str):
            raise ValueError("set_filter requires a string 'pattern'")
        # An empty pattern clears the filter (grammar in the tool schema),
        # which the typed action spells as a None pattern.
        return SetFilter(filter_pattern=pattern or None)
    if dispatch == "agent_open_logs":
        return OpenLogs(
            pod=_require_str(arguments, "pod"),
            namespace=_require_str(arguments, "namespace"),
            container=_optional_str(arguments, "container"),
        )
    if dispatch == "agent_open_describe":
        return OpenDescribe(
            kind=_require_str(arguments, "kind"),
            name=_require_str(arguments, "name"),
            namespace=_optional_str(arguments, "namespace"),
        )
    if dispatch == "agent_drill_down":
        return DrillDown(name=_require_str(arguments, "name"))
    raise ValueError(f"tool {definition.name!r}: no UI action mapping for {dispatch!r}")


def _require_str(arguments: dict[str, Any], key: str) -> str:
    """A required non-blank string argument, or a `ValueError`."""
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key!r} must be a non-empty string")
    return value


def _optional_str(arguments: dict[str, Any], key: str) -> str | None:
    """An optional string argument: absent is None; wrong-typed is refused."""
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key!r} must be a string when provided")
    normalized = value.strip()
    return normalized or None
