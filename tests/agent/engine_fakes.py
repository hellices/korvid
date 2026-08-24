"""Shared fakes and builders for the agent engine suites (issue #316, Task 10).

The engine is driven through its collaborators only — a real
`ConversationState`, a real `RequestGateway` over a scripted `LLMProvider`,
and a real `ToolHarness` over a recording executor and UI bridge — so every
assertion is made against public behaviour (emitted events, the payloads the
provider actually received, retained history) rather than engine internals.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from korvid.agent.conversation import ConversationState
from korvid.agent.engine import AgentEngine, AgentTurnRequest
from korvid.agent.events import AgentEvent
from korvid.agent.evidence import EvidenceLedger
from korvid.agent.interaction import (
    AgentUiBridge,
    InteractionContext,
    PaneContext,
    ResourceIdentity,
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
from korvid.agent.native_engine import NativeAgentEngine
from korvid.agent.outbound import OutboundPolicy, request_char_budget
from korvid.agent.prompt_harness import ComposedPrompt
from korvid.agent.provider import REQUEST_SENT, LLMProvider
from korvid.agent.request_gateway import RequestGateway
from korvid.agent.tool_harness import ToolHarness
from korvid.tools.executor import RecordedExecution, ToolOutcome
from korvid.tools.registry import TOOLS_BY_NAME, resolve_result_formats

#: The composed prompt strings a turn carries. The engine never composes
#: them — Task 6's `PromptHarness` does — so the fakes hand it fixed text.
TURN_SYSTEM_MESSAGE = "korvid safety contract: cite your evidence."
USER_TEXT = "why is the api pod failing?"

DONE: dict[str, Any] = {"type": "done"}


def text_delta(text: str) -> dict[str, Any]:
    """One streamed assistant text chunk."""
    return {"type": "text_delta", "text": text}


def tool_call(call_id: str, name: str, arguments: str = "{}") -> dict[str, Any]:
    """One streamed tool call."""
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": arguments}


def usage(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    """One provider-reported usage event."""
    return {"type": "usage", "input_tokens": input_tokens, "output_tokens": output_tokens}


def text_turn(answer: str = "the pod is healthy") -> list[Any]:
    """A provider round that answers with text and stops."""
    return [text_delta(answer), DONE]


def tool_turn(
    name: str = "get_logs",
    arguments: str = '{"pod":"api-0","namespace":"prod"}',
    call_id: str = "c1",
) -> list[Any]:
    """A provider round that asks for one tool call."""
    return [tool_call(call_id, name, arguments), DONE]


def _plain(value: Any) -> Any:
    """Deep-copy a (possibly frozen) schema into plain dict/list structures."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


class ScriptedProvider(LLMProvider):
    """Streams one scripted event list per `complete` call.

    A `BaseException` in a script is raised at that point in the stream (a
    provider that dies mid-flight); an `asyncio.Event` is awaited (a stalled
    stream). Every other item is yielded as a completion event. The
    generator's `finally` counts closures, so cancellation and orderly
    close are both observable.
    """

    def __init__(
        self,
        turns: Sequence[Sequence[Any]] = (),
        *,
        acknowledge: bool = True,
        model: str = "qwen3:8b",
    ) -> None:
        self._turns = [list(turn) for turn in turns]
        self._acknowledge = acknowledge
        self._model = model
        self.calls: list[list[dict[str, Any]]] = []
        self.tool_surfaces: list[list[dict[str, Any]]] = []
        self.closed = 0
        self.streaming = asyncio.Event()
        self.closed_event = asyncio.Event()
        self.stalled = asyncio.Event()

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("test", self._model)

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities.unknown()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(copy.deepcopy(messages))
        self.tool_surfaces.append(copy.deepcopy(tools))
        script = self._turns.pop(0) if self._turns else []
        self.streaming.set()
        try:
            if self._acknowledge:
                yield {"type": REQUEST_SENT}
            for item in script:
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, asyncio.Event):
                    # Reaching this point proves the consumer processed every
                    # event yielded before it and asked for the next one.
                    self.stalled.set()
                    await item.wait()
                    continue
                yield dict(item)
        finally:
            self.closed += 1
            self.closed_event.set()


class RecordingExecution(RecordedExecution):
    """Records every dispatch and answers from a per-tool script.

    `gate` stalls each dispatch until the test releases it, so a turn can be
    cancelled with a tool call in flight. `max_concurrent` proves the engine
    never runs two tool calls at once.
    """

    def __init__(self, results: Mapping[str, Any] | None = None, *, default: Any = "ok") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.active = 0
        self.max_concurrent = 0
        self._results = dict(results or {})
        self._default = default

    @property
    def names(self) -> list[str]:
        """The tool names dispatched, in order."""
        return [name for name, _arguments in self.calls]

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        return (await self.execute_recorded(name, arguments)).text

    async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, copy.deepcopy(arguments)))
        self.active += 1
        self.max_concurrent = max(self.max_concurrent, self.active)
        self.entered.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            answer = self._results.get(name, self._default)
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, ToolOutcome):
                return answer
            return ToolOutcome(text=str(answer))
        finally:
            self.active -= 1


class RecordingBridge(AgentUiBridge):
    """Records the typed UI actions applied to it."""

    def __init__(self, *, ok: bool = True, message: str = "screen updated") -> None:
        self.actions: list[UiAction] = []
        self._ok = ok
        self._message = message

    def snapshot(self) -> InteractionContext:
        return interaction()

    async def apply(self, action: UiAction) -> UiActionResult:
        self.actions.append(action)
        return UiActionResult(ok=self._ok, message=self._message, context=interaction())


class ExplodingBridge(RecordingBridge):
    """A UI bridge that fails the way a torn-down screen would.

    The failure it raises is supplied by the test, so a case can prove the
    engine names such a failure by type and never by what it said: a
    bridge error can carry a selector, a resource body or an endpoint.
    """

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error if error is not None else RuntimeError("bridge exploded")

    async def apply(self, action: UiAction) -> UiActionResult:
        raise self.error


def interaction(epoch: int = 1) -> InteractionContext:
    """A workspace snapshot the engine only reads `context_epoch` from."""
    return InteractionContext(
        kube_context="dev",
        context_epoch=epoch,
        focused_pane=PaneContext(
            kind="pods",
            scope="prod",
            filter_pattern=None,
            selected=ResourceIdentity("Pod", "prod", "api-0", "uid-0"),
        ),
        secondary_pane=None,
        timeline_cursor=None,
    )


def make_policy(
    *,
    tool_names: Sequence[str] = ("get_logs",),
    max_iterations: int = 6,
    max_history_chars: int = 24_000,
    max_result_chars: int | None = None,
    max_tool_calls: int | None = None,
    strict_history_budget: bool = False,
    allow_parallel_tool_calls: bool = True,
    tier: ModelTier = ModelTier.HIGH,
) -> ResolvedAgentPolicy:
    """A resolved policy armed with exactly `tool_names`."""
    return ResolvedAgentPolicy(
        model=ModelDescriptor("test", "qwen3:8b"),
        capabilities=ModelCapabilities.unknown(),
        tier=tier,
        route_source=CapabilitySource.FALLBACK,
        prompt_pack_id="test-pack",
        prompt_overlay_ids=(),
        tools=tuple(copy.deepcopy(TOOLS_BY_NAME[name].schema) for name in tool_names),
        max_iterations=max_iterations,
        max_history_chars=max_history_chars,
        max_result_chars=max_result_chars,
        max_tool_calls_per_iteration=max_tool_calls,
        allow_parallel_tool_calls=allow_parallel_tool_calls,
        strict_history_budget=strict_history_budget,
        catalog_version=None,
    )


@dataclass
class Harness:
    """One engine and the collaborators a test inspects."""

    engine: AgentEngine
    conversation: ConversationState
    gateway: RequestGateway
    tools: ToolHarness
    provider: ScriptedProvider
    execution: RecordedExecution
    bridge: RecordingBridge
    policy: ResolvedAgentPolicy
    interaction: InteractionContext

    def request(
        self,
        user_text: str = USER_TEXT,
        *,
        system_message: str = TURN_SYSTEM_MESSAGE,
    ) -> AgentTurnRequest:
        """One turn request carrying an already composed prompt."""
        return AgentTurnRequest(
            prompt=ComposedPrompt(system_message=system_message, user_message=user_text),
            policy=self.policy,
            interaction=self.interaction,
        )

    async def run(
        self,
        user_text: str = USER_TEXT,
        *,
        system_message: str = TURN_SYSTEM_MESSAGE,
    ) -> list[AgentEvent]:
        """Drive one whole turn and collect its events."""
        request = self.request(user_text, system_message=system_message)
        return [event async for event in self.engine.run(request)]


def build_harness(
    turns: Sequence[Sequence[Any]] = (),
    *,
    policy: ResolvedAgentPolicy | None = None,
    provider: ScriptedProvider | None = None,
    execution: RecordedExecution | None = None,
    bridge: RecordingBridge | None = None,
    max_request_chars: int | None = None,
    gateway_class: type[RequestGateway] = RequestGateway,
    epoch: int = 1,
) -> Harness:
    """Wire a native engine over real components and scripted edges."""
    resolved = policy if policy is not None else make_policy()
    scripted = provider if provider is not None else ScriptedProvider(turns)
    executor = execution if execution is not None else RecordingExecution()
    ui_bridge = bridge if bridge is not None else RecordingBridge()
    schemas = [_plain(schema) for schema in resolved.tools]
    ceiling = max_request_chars
    if ceiling is None:
        ceiling = request_char_budget(
            max_history_chars=resolved.max_history_chars,
            tools_chars=len(json.dumps(schemas)),
        )
    outbound = OutboundPolicy(ceiling, resolve_result_formats(schemas))
    conversation = ConversationState(
        max_history_chars=resolved.max_history_chars,
        strict_history_budget=resolved.strict_history_budget,
    )
    gateway = gateway_class(scripted, outbound)
    tools = ToolHarness(
        policy=resolved,
        execution=executor,
        bridge=ui_bridge,
        evidence=EvidenceLedger(),
    )
    engine = NativeAgentEngine(conversation=conversation, gateway=gateway, tools=tools)
    return Harness(
        engine=engine,
        conversation=conversation,
        gateway=gateway,
        tools=tools,
        provider=scripted,
        execution=executor,
        bridge=ui_bridge,
        policy=resolved,
        interaction=interaction(epoch),
    )


# -- payload readers ---------------------------------------------------------


def system_message(call: Sequence[Mapping[str, Any]]) -> str:
    """The ephemeral system message of one recorded request."""
    return str(call[0]["content"])


def roles(call: Sequence[Mapping[str, Any]]) -> list[str]:
    """The message roles of one recorded request, in order."""
    return [str(message.get("role")) for message in call]


def assistant_tool_calls(call: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every tool call stored on the assistant messages of one request."""
    calls: list[dict[str, Any]] = []
    for message in call:
        if message.get("role") == "assistant":
            calls.extend(message.get("tool_calls") or [])
    return calls


def tool_results(call: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every tool result message of one recorded request."""
    return [dict(message) for message in call if message.get("role") == "tool"]
