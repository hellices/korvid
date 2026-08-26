"""Policy-aware tool execution harness (issue #316, Task 9).

The harness is the single seam the agent engine uses to run one tool call.
It routes by the registry's validated effect — cluster/external reads and
every write go to `RecordedExecution.execute_recorded`; UI-drive tools become
typed Task-1 `UiAction` values applied through `AgentUiBridge.apply` — mints
evidence only for successful reads, sanitizes every result once, and enforces
the policy's per-iteration tool-call budget before touching any port.
"""

from __future__ import annotations

import copy
import json
from typing import Any, get_args

import pytest

from korvid.agent.evidence import EvidenceLedger
from korvid.agent.interaction import (
    AgentUiBridge,
    ClusterFacts,
    DrillDown,
    InteractionContext,
    Navigate,
    OpenDescribe,
    OpenLogs,
    PaneContext,
    ResourceIdentity,
    SetFilter,
    UiAction,
    UiActionResult,
)
from korvid.agent.model_policy import (
    CapabilitySource,
    ModelCapabilities,
    ModelDescriptor,
    ModelTier,
    ResolvedAgentPolicy,
)
from korvid.agent.outbound import OutboundPolicyError
from korvid.agent.tool_harness import (
    DIRECT_OPEN_ACKNOWLEDGEMENT,
    ToolExecution,
    ToolHarness,
    _ui_action,
)
from korvid.core.redaction import RedactionRecord
from korvid.tools.executor import (
    RecordedExecution,
    ToolExecutor,
    ToolOutcome,
    UIBridge,
)
from korvid.tools.registry import AGENT_SURFACES, TOOL_DEFS, TOOLS_BY_NAME, ToolDef

# --- fixtures ---------------------------------------------------------------


def _context() -> InteractionContext:
    return InteractionContext(
        kube_context="dev",
        context_epoch=1,
        focused_pane=PaneContext(
            kind="pods",
            scope="default",
            filter_pattern=None,
            selected=ResourceIdentity("Pod", "default", "api-1", "uid-1"),
        ),
        secondary_pane=None,
        timeline_cursor=None,
    )


def _policy(
    tool_names: list[str],
    *,
    max_tool_calls: int | None,
    max_result_chars: int | None = None,
    tier: ModelTier = ModelTier.HIGH,
) -> ResolvedAgentPolicy:
    tools = tuple(copy.deepcopy(TOOLS_BY_NAME[name].schema) for name in tool_names)
    return ResolvedAgentPolicy(
        model=ModelDescriptor("test", "model"),
        capabilities=ModelCapabilities.unknown(),
        tier=tier,
        route_source=CapabilitySource.FALLBACK,
        prompt_pack_id="test",
        prompt_overlay_ids=(),
        tools=tools,
        max_iterations=6,
        max_history_chars=24_000,
        max_result_chars=max_result_chars,
        max_tool_calls_per_iteration=max_tool_calls,
        allow_parallel_tool_calls=False,
        strict_history_budget=True,
        catalog_version=None,
    )


class _RecordingExecution(RecordedExecution):
    """A `RecordedExecution` that records calls and returns a fixed outcome."""

    def __init__(self, outcome: ToolOutcome | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._outcome = outcome if outcome is not None else ToolOutcome(text="ok")

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:  # pragma: no cover
        return (await self.execute_recorded(name, arguments)).text

    async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, arguments))
        return self._outcome


class _MutatingExecution(RecordedExecution):
    """Mutates the arguments it receives, to prove the harness copies inputs."""

    def __init__(self) -> None:
        self.seen: dict[str, Any] | None = None

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:  # pragma: no cover
        return (await self.execute_recorded(name, arguments)).text

    async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.seen = arguments
        arguments["injected"] = "mutated"
        return ToolOutcome(text="ok")


class _RecordingBridge(AgentUiBridge):
    """Records the typed UI actions applied to it."""

    def __init__(
        self,
        *,
        ok: bool = True,
        message: str = "done",
        context: InteractionContext | None = None,
    ) -> None:
        self.actions: list[UiAction] = []
        self._ok = ok
        self._message = message
        self._context = context or _context()

    def snapshot(self) -> InteractionContext:
        return self._context

    async def apply(self, action: UiAction) -> UiActionResult:
        self.actions.append(action)
        return UiActionResult(ok=self._ok, message=self._message, context=self._context)


class _ApprovalBridge(UIBridge):
    """The existing approval-gated write bridge; records approved writes."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return "ok"

    async def agent_set_filter(self, pattern: str) -> str:
        return "ok"

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        return "ok"

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        return "ok"

    async def agent_drill_down(self, name: str) -> str:
        return "ok"

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        self.writes.append({"action": action, "kind": kind, "name": name, "namespace": namespace})
        return f"approved and executed: {action} {kind}/{name}"

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
    ) -> str:  # pragma: no cover - not exercised here
        return "proposal pending"

    async def agent_get_write_proposal(self, proposal_id: str) -> str:  # pragma: no cover
        return "proposal pending"

    async def agent_cancel_write_proposal(
        self, proposal_id: str, *, session_id: str = ""
    ) -> str:  # pragma: no cover
        return "proposal cancelled"


def _harness(
    policy: ResolvedAgentPolicy,
    *,
    execution: RecordedExecution | None = None,
    bridge: AgentUiBridge | None = None,
    evidence: EvidenceLedger | None = None,
) -> ToolHarness:
    return ToolHarness(
        policy=policy,
        execution=execution if execution is not None else _RecordingExecution(),
        bridge=bridge if bridge is not None else _RecordingBridge(),
        evidence=evidence if evidence is not None else EvidenceLedger(),
    )


# --- port routing -----------------------------------------------------------


async def test_read_routes_only_to_recorded_execution() -> None:
    execution = _RecordingExecution(ToolOutcome(text="kind: Pod\nmetadata:\n  name: api-1\n"))
    bridge = _RecordingBridge()
    harness = _harness(
        _policy(["get_resource"], max_tool_calls=None), execution=execution, bridge=bridge
    )

    result = await harness.execute("c1", "get_resource", {"kind": "pods", "name": "api-1"})

    assert isinstance(result, ToolExecution)
    assert execution.calls == [("get_resource", {"kind": "pods", "name": "api-1"})]
    assert bridge.actions == []  # a read never touches the UI bridge


async def test_ui_tool_routes_only_to_bridge_as_typed_action() -> None:
    execution = _RecordingExecution()
    bridge = _RecordingBridge(message="switched to deployments")
    harness = _harness(
        _policy(["navigate"], max_tool_calls=None), execution=execution, bridge=bridge
    )

    result = await harness.execute(
        "c1", "navigate", {"view": "deployments", "namespace": "default"}
    )

    assert bridge.actions == [Navigate(view="deployments", namespace="default")]
    assert execution.calls == []  # a UI action never touches the executor
    assert result.outcome.text.startswith("switched to deployments")
    assert "Workspace context (JSON):" in result.outcome.text
    assert "api-1" in result.outcome.text
    assert result.evidence_ref is None


async def test_ui_tool_normalizes_a_blank_optional_namespace_to_none() -> None:
    bridge = _RecordingBridge()
    harness = _harness(
        _policy(["navigate"], max_tool_calls=None),
        bridge=bridge,
    )

    result = await harness.execute(
        "c1",
        "navigate",
        {"view": "deployments", "namespace": "   "},
    )

    assert result.outcome.error is False
    assert bridge.actions == [Navigate(view="deployments", namespace=None)]


async def test_ui_tool_keeps_large_post_action_context_as_valid_bounded_json() -> None:
    context = InteractionContext(
        kube_context="cluster-" + "x" * 5_000,
        context_epoch=1,
        focused_pane=PaneContext(
            kind="pods",
            scope="default",
            filter_pattern="token: workspace-secret " + "f" * 5_000,
            selected=ResourceIdentity(
                "Pod",
                "default",
                "pod-" + "n" * 5_000,
                "uid-" + "u" * 5_000,
            ),
        ),
        secondary_pane=None,
        timeline_cursor=None,
    )
    harness = _harness(
        _policy(["navigate"], max_tool_calls=None, max_result_chars=3_000),
        bridge=_RecordingBridge(context=context),
    )

    result = await harness.execute("c1", "navigate", {"view": "pods"})

    assert len(result.outcome.text) <= 3_000
    encoded = result.outcome.text.split("Workspace context (JSON): ", 1)[1]
    payload = json.loads(encoded)
    assert payload["context_epoch"] == 1
    assert payload["focused_pane"]["selected"]["name"].startswith("pod-")
    assert "workspace-secret" not in result.outcome.text


async def test_write_routes_through_executor_approval_not_ui_bridge() -> None:
    approval = _ApprovalBridge()
    executor = ToolExecutor(kube=None, aliases={}, ui=approval)  # type: ignore[arg-type]  # kube unused for write dispatch
    ui_bridge = _RecordingBridge()
    harness = _harness(
        _policy(["rollout_restart"], max_tool_calls=None), execution=executor, bridge=ui_bridge
    )

    await harness.execute(
        "c1",
        "rollout_restart",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert approval.writes == [
        {"action": "rollout_restart", "kind": "deployments", "name": "api", "namespace": "default"}
    ]
    # A write is never routed through the typed AgentUiBridge, and the harness
    # holds no direct Kubernetes/write object of its own.
    assert ui_bridge.actions == []
    assert not hasattr(harness, "_kube")


async def test_write_mints_no_evidence() -> None:
    approval = _ApprovalBridge()
    executor = ToolExecutor(kube=None, aliases={}, ui=approval)  # type: ignore[arg-type]
    evidence = EvidenceLedger()
    harness = _harness(
        _policy(["delete_resource"], max_tool_calls=None), execution=executor, evidence=evidence
    )

    result = await harness.execute(
        "c1", "delete_resource", {"kind": "pods", "name": "api-1", "namespace": "default"}
    )

    assert result.evidence_ref is None
    assert evidence.references() == ()


# --- every UI mapping -------------------------------------------------------


async def test_navigate_maps_to_navigate_action() -> None:
    bridge = _RecordingBridge()
    harness = _harness(_policy(["navigate"], max_tool_calls=None), bridge=bridge)
    await harness.execute("c1", "navigate", {"view": "pods"})
    assert bridge.actions == [Navigate(view="pods", namespace=None)]


async def test_set_filter_maps_pattern_to_set_filter_action() -> None:
    bridge = _RecordingBridge()
    harness = _harness(_policy(["set_filter"], max_tool_calls=None), bridge=bridge)
    await harness.execute("c1", "set_filter", {"pattern": "api"})
    assert bridge.actions == [SetFilter(filter_pattern="api")]


async def test_set_filter_empty_pattern_clears_the_filter() -> None:
    bridge = _RecordingBridge()
    harness = _harness(_policy(["set_filter"], max_tool_calls=None), bridge=bridge)
    await harness.execute("c1", "set_filter", {"pattern": ""})
    assert bridge.actions == [SetFilter(filter_pattern=None)]


async def test_open_logs_maps_to_open_logs_action() -> None:
    bridge = _RecordingBridge()
    harness = _harness(_policy(["open_logs"], max_tool_calls=None), bridge=bridge)
    await harness.execute(
        "c1", "open_logs", {"pod": "api-1", "namespace": "default", "container": "app"}
    )
    assert bridge.actions == [OpenLogs(pod="api-1", namespace="default", container="app")]


async def test_open_describe_maps_to_open_describe_action() -> None:
    bridge = _RecordingBridge()
    harness = _harness(_policy(["open_describe"], max_tool_calls=None), bridge=bridge)
    await harness.execute(
        "c1", "open_describe", {"kind": "pods", "name": "api-1", "namespace": "default"}
    )
    assert bridge.actions == [OpenDescribe(kind="pods", name="api-1", namespace="default")]


async def test_drill_down_maps_to_drill_down_action() -> None:
    bridge = _RecordingBridge()
    harness = _harness(_policy(["drill_down"], max_tool_calls=None), bridge=bridge)
    await harness.execute("c1", "drill_down", {"name": "api"})
    assert bridge.actions == [DrillDown(name="api")]


# --- terminal direct-open acknowledgement (issue #316, Task 2) --------------


async def test_open_logs_without_continue_analysis_returns_the_terminal_message() -> None:
    """The default (omitted `continue_analysis`) is the fast, terminal path."""
    harness = _harness(_policy(["open_logs"], max_tool_calls=None))

    result = await harness.execute("c1", "open_logs", {"pod": "api-1", "namespace": "default"})

    assert result.terminal_message == DIRECT_OPEN_ACKNOWLEDGEMENT


async def test_open_logs_with_continue_analysis_false_returns_the_terminal_message() -> None:
    harness = _harness(_policy(["open_logs"], max_tool_calls=None))

    result = await harness.execute(
        "c1",
        "open_logs",
        {"pod": "api-1", "namespace": "default", "continue_analysis": False},
    )

    assert result.terminal_message == DIRECT_OPEN_ACKNOWLEDGEMENT


async def test_open_logs_with_continue_analysis_true_returns_no_terminal_message() -> None:
    harness = _harness(_policy(["open_logs"], max_tool_calls=None))

    result = await harness.execute(
        "c1",
        "open_logs",
        {"pod": "api-1", "namespace": "default", "continue_analysis": True},
    )

    assert result.terminal_message is None


async def test_a_failed_open_logs_returns_no_terminal_message() -> None:
    bridge = _RecordingBridge(ok=False, message="could not open the log pane")
    harness = _harness(_policy(["open_logs"], max_tool_calls=None), bridge=bridge)

    result = await harness.execute("c1", "open_logs", {"pod": "api-1", "namespace": "default"})

    assert result.terminal_message is None


async def test_open_describe_without_continue_analysis_returns_the_terminal_message() -> None:
    harness = _harness(_policy(["open_describe"], max_tool_calls=None))

    result = await harness.execute(
        "c1", "open_describe", {"kind": "pods", "name": "api-1", "namespace": "default"}
    )

    assert result.terminal_message == DIRECT_OPEN_ACKNOWLEDGEMENT


async def test_open_describe_with_continue_analysis_true_returns_no_terminal_message() -> None:
    harness = _harness(_policy(["open_describe"], max_tool_calls=None))

    result = await harness.execute(
        "c1",
        "open_describe",
        {"kind": "pods", "name": "api-1", "namespace": "default", "continue_analysis": True},
    )

    assert result.terminal_message is None


async def test_a_failed_open_describe_returns_no_terminal_message() -> None:
    bridge = _RecordingBridge(ok=False, message="could not open describe")
    harness = _harness(_policy(["open_describe"], max_tool_calls=None), bridge=bridge)

    result = await harness.execute(
        "c1", "open_describe", {"kind": "pods", "name": "api-1", "namespace": "default"}
    )

    assert result.terminal_message is None


@pytest.mark.parametrize(
    "bad_value",
    ["true", "false", "1", "0", 1, 0],
    ids=["str-true", "str-false", "str-1", "str-0", "int-1", "int-0"],
)
async def test_open_logs_with_non_bool_continue_analysis_is_rejected_before_bridge(
    bad_value: object,
) -> None:
    """A string or integer `continue_analysis` must be rejected (invalid args)
    before the UI bridge is ever called."""
    bridge = _RecordingBridge()
    harness = _harness(_policy(["open_logs"], max_tool_calls=None), bridge=bridge)

    result = await harness.execute(
        "c1",
        "open_logs",
        {"pod": "api-1", "namespace": "default", "continue_analysis": bad_value},
    )

    assert result.outcome.error is True
    assert "continue_analysis" in result.outcome.text
    assert bridge.actions == [], "bridge must not be called when arguments are invalid"


@pytest.mark.parametrize(
    "bad_value",
    ["true", "false", "1", "0", 1, 0],
    ids=["str-true", "str-false", "str-1", "str-0", "int-1", "int-0"],
)
async def test_open_describe_with_non_bool_continue_analysis_is_rejected_before_bridge(
    bad_value: object,
) -> None:
    """A string or integer `continue_analysis` must be rejected (invalid args)
    before the UI bridge is ever called."""
    bridge = _RecordingBridge()
    harness = _harness(_policy(["open_describe"], max_tool_calls=None), bridge=bridge)

    result = await harness.execute(
        "c1",
        "open_describe",
        {
            "kind": "pods",
            "name": "api-1",
            "namespace": "default",
            "continue_analysis": bad_value,
        },
    )

    assert result.outcome.error is True
    assert "continue_analysis" in result.outcome.text
    assert bridge.actions == [], "bridge must not be called when arguments are invalid"


async def test_the_terminal_message_never_echoes_arguments_or_bridge_text() -> None:
    """Security boundary: the acknowledgement is a fixed constant. Neither
    the model's own arguments nor the bridge's (screen-controlled) message
    text may leak into it — either could carry secrets or injected content
    across the outbound boundary."""
    bridge = _RecordingBridge(message="UNTRUSTED-BRIDGE-MARKER-should-never-appear")
    harness = _harness(_policy(["open_logs"], max_tool_calls=None), bridge=bridge)

    result = await harness.execute(
        "c1", "open_logs", {"pod": "ARGUMENT-MARKER-should-never-appear", "namespace": "default"}
    )

    assert result.terminal_message == DIRECT_OPEN_ACKNOWLEDGEMENT
    assert "ARGUMENT-MARKER" not in result.terminal_message
    assert "UNTRUSTED-BRIDGE-MARKER" not in result.terminal_message


async def test_navigate_never_returns_a_terminal_message() -> None:
    """Only direct-open tools are terminal; other UI-drive tools are not."""
    harness = _harness(_policy(["navigate"], max_tool_calls=None))

    result = await harness.execute("c1", "navigate", {"view": "pods"})

    assert result.terminal_message is None


async def test_drill_down_never_returns_a_terminal_message() -> None:
    harness = _harness(_policy(["drill_down"], max_tool_calls=None))

    result = await harness.execute("c1", "drill_down", {"name": "api"})

    assert result.terminal_message is None


def _minimal_arguments(schema: dict[str, Any]) -> dict[str, Any]:
    """The smallest argument set a tool's own JSON schema calls valid.

    Only the declared `required` properties, each filled with a non-blank
    value of its declared type. Anything richer would test this helper
    instead of the conversion.
    """
    parameters = schema["function"].get("parameters", {})
    properties = parameters.get("properties", {})
    arguments: dict[str, Any] = {}
    for name in parameters.get("required", []):
        kind = properties.get(name, {}).get("type", "string")
        if kind == "integer":
            arguments[name] = 1
        elif kind == "number":
            arguments[name] = 1.0
        elif kind == "boolean":
            arguments[name] = True
        elif kind == "array":
            arguments[name] = ["x"]
        elif kind == "object":
            arguments[name] = {"k": "v"}
        else:
            arguments[name] = "x"
    return arguments


def _armed_ui_tools() -> list[ToolDef]:
    """Every registry tool that drives the screen on an agent surface."""
    return [
        definition
        for definition in TOOL_DEFS
        if definition.effect == "ui_only" and definition.surfaces & AGENT_SURFACES
    ]


def test_the_registry_does_arm_ui_tools_to_convert() -> None:
    """The completeness test below is worthless against an empty list."""
    names = {definition.name for definition in _armed_ui_tools()}

    assert names == {"navigate", "set_filter", "open_logs", "open_describe", "drill_down"}


@pytest.mark.parametrize(
    "definition", _armed_ui_tools(), ids=lambda definition: str(definition.name)
)
def test_every_armed_ui_tool_converts_to_a_typed_action(definition: ToolDef) -> None:
    """A UI tool a policy can arm must have a `UiAction` behind it.

    This is derived from the registry, not from a list kept next to it: a
    future screen tool added with `effect="ui_only"` and an agent surface
    is armed the moment a policy includes it, and without a conversion the
    model's first call comes back "no UI action mapping" — a tool korvid
    published and cannot run. The arguments come from each tool's own
    schema, so the check follows a schema change too.
    """
    action = _ui_action(definition, _minimal_arguments(definition.schema))

    assert isinstance(action, get_args(UiAction))


def test_the_completeness_check_fails_for_a_tool_with_no_conversion() -> None:
    """The teeth of the check above: a screen tool nothing converts.

    Stands in for the next `ui_only` tool someone adds to the registry and
    arms on an agent surface without teaching `_ui_action` about it — the
    parametrised test would collect it, call this same function, and fail
    here instead of shipping a tool whose every call answers "no UI action
    mapping".
    """
    future_tool = ToolDef(
        name="split_pane",
        effect="ui_only",
        dispatch="agent_split_pane",
        surfaces=AGENT_SURFACES,
        result_format="untrusted_text",
        schema={
            "type": "function",
            "function": {
                "name": "split_pane",
                "description": "Split the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"index": {"type": "integer"}},
                    "required": ["index"],
                },
            },
        },
    )

    with pytest.raises(ValueError, match="no UI action mapping"):
        _ui_action(future_tool, _minimal_arguments(future_tool.schema))


@pytest.mark.parametrize(
    "definition", _armed_ui_tools(), ids=lambda definition: str(definition.name)
)
async def test_every_armed_ui_tool_reaches_the_bridge_end_to_end(definition: ToolDef) -> None:
    """And the same call really does apply through the harness."""
    bridge = _RecordingBridge()
    harness = _harness(_policy([definition.name], max_tool_calls=None), bridge=bridge)

    result = await harness.execute("c1", definition.name, _minimal_arguments(definition.schema))

    assert result.outcome.error is False
    assert len(bridge.actions) == 1


async def test_ui_invalid_arguments_return_one_bounded_error_without_bridge() -> None:
    bridge = _RecordingBridge()
    harness = _harness(_policy(["navigate"], max_tool_calls=None), bridge=bridge)

    result = await harness.execute("c1", "navigate", {"view": "   "})

    assert result.outcome.error is True
    assert result.outcome.text.startswith("ERROR:")
    assert bridge.actions == []  # invalid arguments never reach the bridge


# --- unarmed / unknown ------------------------------------------------------


async def test_unarmed_tool_fails_before_any_port() -> None:
    execution = _RecordingExecution()
    bridge = _RecordingBridge()
    # get_logs is a real registry tool but is not in this policy's schemas.
    harness = _harness(
        _policy(["get_resource"], max_tool_calls=None), execution=execution, bridge=bridge
    )

    result = await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert result.outcome.error is True
    assert result.outcome.text.startswith("ERROR:")
    assert execution.calls == []
    assert bridge.actions == []


async def test_unknown_tool_fails_before_any_port() -> None:
    execution = _RecordingExecution()
    bridge = _RecordingBridge()
    harness = _harness(
        _policy(["get_resource"], max_tool_calls=None), execution=execution, bridge=bridge
    )

    result = await harness.execute("c1", "not_a_tool", {})

    assert result.outcome.error is True
    assert execution.calls == []
    assert bridge.actions == []


async def test_unknown_tool_error_obeys_the_policy_result_limit() -> None:
    harness = _harness(_policy(["get_resource"], max_tool_calls=None, max_result_chars=64))

    result = await harness.execute("c1", "not_a_tool_" + "x" * 1_000, {})

    assert result.outcome.error is True
    assert len(result.outcome.text) <= 64


# --- per-iteration budget ---------------------------------------------------


async def test_low_tier_rejects_second_call_in_one_iteration() -> None:
    execution = _RecordingExecution(ToolOutcome(text="ok"))
    harness = _harness(
        _policy(["list_resources"], max_tool_calls=1, tier=ModelTier.LOW), execution=execution
    )
    harness.begin_iteration()

    first = await harness.execute("c1", "list_resources", {"kind": "pods"})
    second = await harness.execute("c2", "list_resources", {"kind": "deployments"})

    assert first.outcome.error is False
    assert second.outcome.error is True
    assert second.outcome.text.startswith("ERROR:")
    # The rejected excess call never reaches the executor.
    assert execution.calls == [("list_resources", {"kind": "pods"})]


async def test_begin_iteration_resets_the_budget() -> None:
    execution = _RecordingExecution(ToolOutcome(text="ok"))
    harness = _harness(
        _policy(["list_resources"], max_tool_calls=1, tier=ModelTier.LOW), execution=execution
    )

    harness.begin_iteration()
    await harness.execute("c1", "list_resources", {"kind": "pods"})
    harness.begin_iteration()
    second = await harness.execute("c2", "list_resources", {"kind": "deployments"})

    assert second.outcome.error is False
    assert len(execution.calls) == 2


async def test_high_tier_unlimited_budget_accepts_many_calls() -> None:
    execution = _RecordingExecution(ToolOutcome(text="ok"))
    harness = _harness(
        _policy(["list_resources"], max_tool_calls=None, tier=ModelTier.HIGH), execution=execution
    )
    harness.begin_iteration()

    for i in range(5):
        result = await harness.execute("c", "list_resources", {"kind": f"kind-{i}"})
        assert result.outcome.error is False

    assert len(execution.calls) == 5


async def test_high_tier_bounded_budget_rejects_beyond_the_limit() -> None:
    execution = _RecordingExecution(ToolOutcome(text="ok"))
    harness = _harness(
        _policy(["list_resources"], max_tool_calls=2, tier=ModelTier.HIGH), execution=execution
    )
    harness.begin_iteration()

    await harness.execute("c1", "list_resources", {"kind": "pods"})
    await harness.execute("c2", "list_resources", {"kind": "deployments"})
    third = await harness.execute("c3", "list_resources", {"kind": "services"})

    assert third.outcome.error is True
    assert len(execution.calls) == 2


# --- result sanitisation ----------------------------------------------------


async def test_oversized_result_is_capped_before_history() -> None:
    big = "x" * 1000
    execution = _RecordingExecution(ToolOutcome(text=big))
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=None, max_result_chars=200), execution=execution
    )

    result = await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert len(result.outcome.text) <= 200
    assert len(result.outcome.text) < len(big)


async def test_oversized_result_keeps_both_ends_of_the_report() -> None:
    """Migrated from the retired profile-budget suite.

    korvid's diagnostic reports put identity first and the evidence —
    Warning events, log excerpts, exit reasons — last. A prefix-only cap
    would keep the pod's name and throw away the reason it died, so the
    model would answer from the least diagnostic half of its own read.
    """
    head = "IDENTITY: pod prod/api-0\n"
    tail = "\nLOG EXCERPT: OOMKilled (exit 137)"
    execution = _RecordingExecution(ToolOutcome(text=head + "x" * 5_000 + tail))
    harness = _harness(
        _policy(["diagnose_pod"], max_tool_calls=None, max_result_chars=1_000),
        execution=execution,
    )

    result = await harness.execute("c1", "diagnose_pod", {"name": "api-0", "namespace": "prod"})

    assert len(result.outcome.text) <= 1_000
    assert "IDENTITY: pod prod/api-0" in result.outcome.text
    assert "OOMKilled (exit 137)" in result.outcome.text


async def test_a_ui_drive_action_is_never_citable_as_evidence() -> None:
    """Migrated from the retired evidence-runtime suite.

    A screen action reads nothing from the cluster. Minting a reference
    for it would let the model cite "[E1]" for a claim whose only backing
    is that a pane changed.
    """
    evidence = EvidenceLedger()
    bridge = _RecordingBridge(message="switched to deployments")
    harness = _harness(_policy(["navigate"], max_tool_calls=None), bridge=bridge, evidence=evidence)

    result = await harness.execute("c1", "navigate", {"view": "deployments"})

    assert bridge.actions == [Navigate(view="deployments", namespace=None)]
    assert result.evidence_ref is None
    assert evidence.references() == ()


async def test_producer_redactions_are_preserved_and_merged() -> None:
    produced = (RedactionRecord(path="result.spec.password", reason="value-classified-secret"),)
    execution = _RecordingExecution(ToolOutcome(text="ok", redactions=produced))
    harness = _harness(_policy(["get_logs"], max_tool_calls=None), execution=execution)

    result = await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert any(
        record.reason == "value-classified-secret" and record.path.startswith("tool_result")
        for record in result.outcome.redactions
    )


async def test_unredactable_structured_result_blocks_the_turn() -> None:
    # A structured_yaml tool whose result is a bare scalar cannot be redacted
    # as a document, so the fail-closed sanitizer stops the turn.
    execution = _RecordingExecution(ToolOutcome(text="a plain scalar, not a mapping"))
    harness = _harness(_policy(["get_resource"], max_tool_calls=None), execution=execution)

    with pytest.raises(OutboundPolicyError, match="mapping or list"):
        await harness.execute("c1", "get_resource", {"kind": "pods", "name": "api-1"})


# --- evidence ---------------------------------------------------------------


async def test_successful_read_mints_evidence_from_sanitized_text() -> None:
    execution = _RecordingExecution(ToolOutcome(text="log excerpt"))
    evidence = EvidenceLedger()
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=None), execution=execution, evidence=evidence
    )

    result = await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert result.evidence_ref == "E1"
    minted = evidence.resolve("E1")
    assert minted is not None
    assert minted.tool == "get_logs"
    assert "log excerpt" in minted.excerpt


async def test_failed_read_mints_no_evidence() -> None:
    execution = _RecordingExecution(ToolOutcome(text="ERROR: boom", error=True))
    evidence = EvidenceLedger()
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=None), execution=execution, evidence=evidence
    )

    result = await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert result.evidence_ref is None
    assert evidence.references() == ()


async def test_evidence_carries_incarnation_and_container() -> None:
    execution = _RecordingExecution(
        ToolOutcome(text="log excerpt", incarnation="pod-uid-1", container="sidecar")
    )
    evidence = EvidenceLedger()
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=None), execution=execution, evidence=evidence
    )

    await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})

    minted = evidence.resolve("E1")
    assert minted is not None
    assert minted.incarnation == "pod-uid-1"
    assert minted.container == "sidecar"


async def test_external_read_mints_evidence() -> None:
    execution = _RecordingExecution(ToolOutcome(text="cpu: 0.5"))
    evidence = EvidenceLedger()
    harness = _harness(
        _policy(["query_metrics"], max_tool_calls=None), execution=execution, evidence=evidence
    )

    result = await harness.execute("c1", "query_metrics", {"signal": "cpu", "namespace": "default"})

    assert result.evidence_ref == "E1"


async def test_reset_evidence_clears_and_sets_context_epoch() -> None:
    execution = _RecordingExecution(ToolOutcome(text="log excerpt"))
    evidence = EvidenceLedger()
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=None), execution=execution, evidence=evidence
    )

    await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})
    assert evidence.references() == ("E1",)

    harness.reset_evidence(7)

    assert harness.context_epoch == 7
    assert evidence.references() == ()
    # Numbering restarts on the next turn.
    second = await harness.execute("c2", "get_logs", {"pod": "api-2", "namespace": "default"})
    assert second.evidence_ref == "E1"


async def test_clear_evidence_drops_the_ledger_and_keeps_the_context_epoch() -> None:
    """A retarget clears citations without claiming to know the live epoch.

    The epoch a turn's evidence belongs to is read from the workspace by
    the turn that starts, so an operation that only re-arms the surface
    must not invent one — it drops what the old surface minted and leaves
    the epoch to the next turn.
    """
    execution = _RecordingExecution(ToolOutcome(text="log excerpt"))
    evidence = EvidenceLedger()
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=None), execution=execution, evidence=evidence
    )
    harness.reset_evidence(7)
    await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})
    assert evidence.references() == ("E1",)

    harness.clear_evidence()

    assert harness.context_epoch == 7
    assert evidence.references() == ()
    assert evidence.resolve("E1") is None


async def test_harness_exposes_its_evidence_ledger() -> None:
    evidence = EvidenceLedger()
    harness = _harness(_policy(["get_logs"], max_tool_calls=None), evidence=evidence)
    assert harness.evidence is evidence


# --- input copying ----------------------------------------------------------


async def test_inputs_are_copied_before_dispatch() -> None:
    execution = _MutatingExecution()
    harness = _harness(_policy(["list_resources"], max_tool_calls=None), execution=execution)
    arguments = {"kind": "pods"}

    await harness.execute("c1", "list_resources", arguments)

    # The executor mutated its copy, not the caller's dict.
    assert arguments == {"kind": "pods"}


def test_cluster_facts_contract_available() -> None:
    # Guards the interaction import surface the harness tests rely on.
    facts = ClusterFacts(provider="azure", distribution="aks")
    assert facts.provider == "azure"


# --- refusing a call the engine will not dispatch (Task 10) ------------------


async def test_reject_answers_a_call_without_touching_a_port() -> None:
    """The engine's own refusals reuse this harness's one error format."""
    execution = _RecordingExecution()
    bridge = _RecordingBridge()
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=None), execution=execution, bridge=bridge
    )

    result = harness.reject("c1", "get_logs", "arguments must be a JSON object")

    assert result == ToolExecution(
        call_id="c1",
        name="get_logs",
        outcome=ToolOutcome(text="ERROR: arguments must be a JSON object", error=True),
        evidence_ref=None,
    )
    assert execution.calls == []
    assert bridge.actions == []


async def test_reject_is_bounded() -> None:
    harness = _harness(_policy(["get_logs"], max_tool_calls=None))

    result = harness.reject("c1", "get_logs", "x" * 10_000)

    assert len(result.outcome.text) < 10_000
    assert result.outcome.error is True


async def test_reject_mints_no_evidence() -> None:
    evidence = EvidenceLedger()
    harness = _harness(_policy(["get_logs"], max_tool_calls=None), evidence=evidence)

    harness.reject("c1", "get_logs", "arguments must be a JSON object")

    assert evidence.references() == ()
    assert evidence.prompt_note() == ""


async def test_an_unarmed_call_does_not_spend_the_iteration_budget() -> None:
    """A refusal is not a dispatch, however the model spelled the name.

    An unarmed name never reaches a port — the harness answers it from a
    fixed string. Charging the iteration budget for it lets a model that
    guesses two wrong names spend a low-tier iteration without korvid
    doing any work, and the correction it is being given becomes
    unusable: the very next (correct) call is refused for budget.
    """
    execution = _RecordingExecution()
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=1, tier=ModelTier.LOW), execution=execution
    )
    harness.begin_iteration()

    unarmed = await harness.execute("c1", "delete_resource", {"kind": "pods", "name": "api-1"})
    real = await harness.execute("c2", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert unarmed.outcome.error is True
    assert "not armed" in unarmed.outcome.text
    assert real.outcome.error is False
    assert execution.calls == [("get_logs", {"pod": "api-1", "namespace": "default"})]


async def test_an_unknown_tool_name_does_not_spend_the_iteration_budget() -> None:
    """The same rule for a name no registry entry defines at all."""
    execution = _RecordingExecution()
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=1, tier=ModelTier.LOW), execution=execution
    )
    harness.begin_iteration()

    unknown = await harness.execute("c1", "hallucinated_tool", {})
    real = await harness.execute("c2", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert unknown.outcome.error is True
    assert real.outcome.error is False
    assert execution.calls == [("get_logs", {"pod": "api-1", "namespace": "default"})]


async def test_the_budget_still_bounds_the_calls_that_do_dispatch() -> None:
    """Not charging refusals must not stop charging real calls."""
    execution = _RecordingExecution()
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=1, tier=ModelTier.LOW), execution=execution
    )
    harness.begin_iteration()

    await harness.execute("c1", "delete_resource", {"kind": "pods", "name": "api-1"})
    first = await harness.execute("c2", "get_logs", {"pod": "api-1", "namespace": "default"})
    second = await harness.execute("c3", "get_logs", {"pod": "api-2", "namespace": "default"})

    assert first.outcome.error is False
    assert second.outcome.error is True
    assert "budget exhausted" in second.outcome.text
    assert len(execution.calls) == 1


async def test_reject_does_not_spend_the_iteration_budget() -> None:
    """A call that never ran cannot consume the budget a real call needs."""
    execution = _RecordingExecution()
    harness = _harness(_policy(["get_logs"], max_tool_calls=1), execution=execution)
    harness.begin_iteration()

    harness.reject("c1", "get_logs", "arguments must be a JSON object")
    result = await harness.execute("c2", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert execution.calls == [("get_logs", {"pod": "api-1", "namespace": "default"})]
    assert result.outcome.error is False


# --- policy validation and retarget -----------------------------------------


def _unregistered_policy() -> ResolvedAgentPolicy:
    """A policy arming a name the registry does not define."""
    schema = copy.deepcopy(TOOLS_BY_NAME["get_logs"].schema)
    schema["function"]["name"] = "not_a_registered_tool"
    armed = _policy(["get_logs"], max_tool_calls=None)
    object.__setattr__(armed, "tools", (schema,))
    return armed


def test_validate_policy_accepts_a_registry_backed_surface() -> None:
    """What validation accepts, construction accepts — one derivation."""
    armed = _policy(["get_logs", "navigate"], max_tool_calls=1)

    ToolHarness.validate_policy(armed)

    assert isinstance(_harness(armed), ToolHarness)


def test_validate_policy_rejects_an_armed_name_the_registry_does_not_define() -> None:
    with pytest.raises(ValueError, match="not_a_registered_tool"):
        ToolHarness.validate_policy(_unregistered_policy())


def test_validate_policy_rejects_a_malformed_schema() -> None:
    armed = _policy(["get_logs"], max_tool_calls=None)
    object.__setattr__(armed, "tools", ({"type": "function", "function": {}},))

    with pytest.raises(ValueError, match="name a function"):
        ToolHarness.validate_policy(armed)


def test_constructing_a_harness_rejects_an_unreachable_armed_tool() -> None:
    """An armed name with no definition would only ever be a runtime error."""
    with pytest.raises(ValueError, match="not_a_registered_tool"):
        _harness(_unregistered_policy())


async def test_retarget_arms_the_new_surface_on_the_same_harness() -> None:
    execution = _RecordingExecution()
    harness = _harness(_policy(["get_logs"], max_tool_calls=None), execution=execution)

    harness.retarget(_policy(["get_events"], max_tool_calls=None))
    armed = await harness.execute("c1", "get_events", {"namespace": "default"})
    disarmed = await harness.execute("c2", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert armed.outcome.error is False
    assert disarmed.outcome.error is True
    assert "not armed" in disarmed.outcome.text
    assert execution.calls == [("get_events", {"namespace": "default"})]


async def test_retarget_installs_the_new_per_iteration_cap() -> None:
    execution = _RecordingExecution()
    harness = _harness(_policy(["get_logs"], max_tool_calls=None), execution=execution)
    harness.begin_iteration()

    harness.retarget(_policy(["get_logs"], max_tool_calls=1))
    harness.begin_iteration()
    first = await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})
    second = await harness.execute("c2", "get_logs", {"pod": "api-2", "namespace": "default"})

    assert first.outcome.error is False
    assert second.outcome.error is True
    assert "budget exhausted" in second.outcome.text


async def test_retarget_installs_the_new_result_bound() -> None:
    execution = _RecordingExecution(ToolOutcome(text="y" * 5_000))
    harness = _harness(
        _policy(["get_logs"], max_tool_calls=None, max_result_chars=None), execution=execution
    )

    harness.retarget(_policy(["get_logs"], max_tool_calls=None, max_result_chars=100))
    result = await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert len(result.outcome.text) <= 200


async def test_a_refused_retarget_leaves_the_previous_surface_armed() -> None:
    execution = _RecordingExecution()
    harness = _harness(_policy(["get_logs"], max_tool_calls=None), execution=execution)

    with pytest.raises(ValueError, match="not_a_registered_tool"):
        harness.retarget(_unregistered_policy())
    result = await harness.execute("c1", "get_logs", {"pod": "api-1", "namespace": "default"})

    assert result.outcome.error is False
    assert execution.calls == [("get_logs", {"pod": "api-1", "namespace": "default"})]
