"""The safety boundary, driven end to end through the LiteLLM transport.

Task 16 adds no feature. Every invariant asserted here lives *above* the
transport by design — the outbound policy, the approval gate, fail-closed
auditing, masking, conversation repair, cancellation and the
sent-versus-intended distinction are all owned by `agent/` and `tools/`,
not by whichever adapter carries the bytes. That is exactly why a
transport swap is when they break: an adapter that reshapes messages
inside `complete`, retries a request the boundary prepared once, or
swallows a cancellation would move the wire out from under all of them
without a single one of their own suites noticing.

So the stack under test is the real one: a real `NativeAgentEngine` over a
real `ConversationState`, a real `RequestGateway` over a real
`OutboundPolicy`, a real `ToolHarness` over a real `ToolExecutor`, and a
real `LiteLLMProvider` — on an `httpx.MockTransport`, which is the only
double. Assertions are made against the *bytes the transport received*,
never against what korvid believed it sent.

What the write tests pin, precisely: that the *agent's* only route to a
mutation is `agent_request_write` — the approval entrypoint — and that a
refusal from the real `AuditLog` on the other side of it blocks the
mutation. `_ApprovalGatedBridge` is this module's own stand-in for the
perimeter behind that entrypoint; it is not `ui/write_coordinator.py`, so
nothing here proves the production coordinator's approval → intent-audit →
mutation ordering. That ordering is covered where it lives, in
`tests/ui/test_write_coordinator.py::test_the_perimeter_runs_its_steps_in_the_required_order`.
What is real here is the audit: the
sink is broken the way a disk breaks it and `AuditLog.append` raises for
real, so "the audit failed" is a filesystem refusal rather than a fake
agreeing with itself. The gate really opens —
`test_an_approved_write_reaches_the_cluster_exactly_once` proves it — so
every `write_ops.calls == []` below is a gate that held, not a stub that
could never fire.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("litellm")

import httpx

from korvid.agent.conversation import ConversationState
from korvid.agent.engine import AgentTurnRequest
from korvid.agent.events import AgentError, AgentEvent, TurnComplete
from korvid.agent.evidence import EvidenceLedger
from korvid.agent.model_policy import ModelDescriptor, ResolvedAgentPolicy
from korvid.agent.native_engine import NativeAgentEngine
from korvid.agent.outbound import OutboundPolicy, PreparedOutbound, request_char_budget
from korvid.agent.prompt_harness import ComposedPrompt
from korvid.agent.request_gateway import RequestGateway
from korvid.agent.tool_harness import ToolHarness
from korvid.core.audit import AuditLog
from korvid.core.redaction import RedactionRecord
from korvid.core.secrets import MASK_PLACEHOLDER
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.providers.litellm_provider import LiteLLMProvider
from korvid.providers.litellm_request import build_plan
from korvid.tools.executor import RecordedExecution, ToolExecutor
from korvid.tools.registry import resolve_result_formats
from tests.agent.engine_fakes import RecordingBridge, RecordingExecution, interaction, make_policy
from tests.tools.executor_fakes import FakeBridge

#: The credential the tests plant. Never a real key shape by accident: the
#: assertions search the wire for exactly this text.
LEAKED = "sk-leaked-0123456789"
#: The key the provider is configured with — it rides in the Authorization
#: header, which is the transport's business, and must never reach a panel.
API_KEY = "sk-configured-key"
BASE_URL = "https://mock.invalid/v1"

DEPLOYMENTS_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
SECRETS_META = ResourceMeta("Secret", "secrets", "", "v1", True)

Handler = Callable[[httpx.Request], httpx.Response]

#: Every client `build` opened during the running test. Closed — with the
#: `httpx.AsyncClient` underneath it — by `_close_clients` at teardown.
_OPEN_CLIENTS: list[Any] = []


@pytest.fixture(autouse=True)
async def _close_clients() -> AsyncIterator[None]:
    """Close the transports a test opened, before the next test runs."""
    _OPEN_CLIENTS.clear()
    yield
    while _OPEN_CLIENTS:
        await _OPEN_CLIENTS.pop().close()


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


def _chunk(**delta: Any) -> dict[str, Any]:
    """One streaming frame carrying a single choice's delta."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4o",
        "choices": [{"index": 0, "delta": dict(delta)}],
    }


def _fragment(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str = "",
) -> dict[str, Any]:
    """One `delta.tool_calls[*]` fragment, as the wire sends it."""
    function: dict[str, Any] = {"arguments": arguments}
    if name is not None:
        function["name"] = name
    fragment: dict[str, Any] = {"index": index, "type": "function", "function": function}
    if call_id is not None:
        fragment["id"] = call_id
    return fragment


def _sse(chunks: Sequence[dict[str, Any]]) -> bytes:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _finish(reason: str = "stop") -> dict[str, Any]:
    """The frame on which a provider says the choice is finished.

    korvid refuses a stream that never carries one (issue #336), so every
    well-formed fixture here ends with it — otherwise these boundary tests
    would all be testing truncation instead of what they are named for.
    """
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4o",
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
    }


def _streaming(*chunks: dict[str, Any], finish: str | None = "stop") -> Handler:
    """A well-formed SSE answer."""
    frames = [*chunks, _finish(finish)] if finish is not None else list(chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(frames),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    return handler


def _answers(text: str) -> Handler:
    """A round that answers with text and asks for nothing."""
    return _streaming(_chunk(content=text))


def _usage_frame(prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    """A choices-free frame carrying the provider's own counts.

    The shape `stream_options.include_usage` buys: counts arrive on their
    own frame at the end of the stream, and they are the *provider's*, not
    a tokenizer estimate LiteLLM synthesized for a provider that sent none.
    """
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4o",
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _answers_with_usage(text: str, *, prompt_tokens: int, completion_tokens: int) -> Handler:
    """A round that answers with text and reports what it cost."""
    return _streaming(_chunk(content=text), _usage_frame(prompt_tokens, completion_tokens))


def _asks(call_id: str, name: str, arguments: str) -> Handler:
    """A round that asks for one tool call, whole."""
    return _streaming(
        _chunk(tool_calls=[_fragment(0, call_id=call_id, name=name, arguments=arguments)])
    )


def _refusing(status: int, body: dict[str, Any]) -> Handler:
    """A provider that answered — with a failure. The payload arrived."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, request=request)

    return handler


def _unreachable() -> Handler:
    """A connection that never opened. Nothing was ever handed over."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return handler


def _stalling(started: asyncio.Event, responses: list[httpx.Response]) -> Handler:
    """A stream that delivers one frame and then never ends.

    The response object is handed back to the test rather than watched
    through the body generator: cancelling the turn unwinds that generator
    all by itself — it is suspended inside the cancelled task — so its
    `finally` would report "closed" even for a response nobody closed.
    `httpx.Response.is_closed` only turns true when something actually
    called `aclose()`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            yield f"data: {json.dumps(_chunk(content='thinking'))}\n\n".encode()
            started.set()
            await asyncio.Event().wait()

        response = httpx.Response(
            200,
            content=body(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )
        responses.append(response)
        return response

    return handler


def _truncated(call_id: str, name: str, arguments: str) -> Handler:
    """One whole tool-call fragment, then the connection dies mid-stream.

    The fragment carries arguments that parse: the only thing wrong with
    this call is that the stream it arrived on never finished, which is
    exactly the case a provider is tempted to let through.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        first = _chunk(tool_calls=[_fragment(0, call_id=call_id, name=name, arguments=arguments)])

        async def body() -> AsyncIterator[bytes]:
            yield f"data: {json.dumps(first)}\n\n".encode()
            raise httpx.ReadError("connection reset mid-stream", request=request)

        return httpx.Response(
            200,
            content=body(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    return handler


class Wire:
    """The mock transport: records every request body, answers from a script.

    The **last** handler is sticky. Measured on litellm 1.98.0, one
    `acompletion` whose connection fails — or which is answered with a 500
    — is retried inside the SDK, so a failure has to keep failing for as
    many attempts as the transport makes; a script that advanced per
    request would answer the second attempt with the next round's reply
    and quietly test something else entirely.
    """

    def __init__(self, handlers: Sequence[Handler]) -> None:
        self._handlers = list(handlers) or [_answers("nothing further")]
        self.bodies: list[bytes] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        handler = self._handlers.pop(0) if len(self._handlers) > 1 else self._handlers[0]
        return handler(request)

    @property
    def requests(self) -> int:
        """How many requests actually reached the transport."""
        return len(self.bodies)

    def sent(self, index: int = -1) -> str:
        """One request body, exactly as the transport received it."""
        return self.bodies[index].decode()

    def payload(self, index: int = -1) -> dict[str, Any]:
        """One request body, parsed."""
        parsed: dict[str, Any] = json.loads(self.bodies[index])
        return parsed

    def messages(self, index: int = -1) -> list[dict[str, Any]]:
        """The messages of one request, as the transport received them."""
        messages: list[dict[str, Any]] = self.payload(index)["messages"]
        return messages


# ---------------------------------------------------------------------------
# The boundary under test
# ---------------------------------------------------------------------------


class CountingPolicy(OutboundPolicy):
    """The real outbound policy, counting how often it prepared a request.

    The count is the ordering proof: one prepared request per request on
    the wire means nothing was built, reshaped or retried anywhere else.
    """

    def __init__(
        self,
        max_request_chars: int,
        result_formats: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(max_request_chars, result_formats)
        self.prepare_calls = 0

    def prepare(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        iteration: int,
        ingress: Mapping[int, Sequence[RedactionRecord]] | None = None,
        tool_errors: Collection[int] | None = None,
    ) -> PreparedOutbound:
        self.prepare_calls += 1
        return super().prepare(
            model,
            messages,
            tools,
            iteration=iteration,
            ingress=ingress,
            tool_errors=tool_errors,
        )


class WriteOps:
    """The port a mutation would reach. The model must never get here."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, str]] = []

    async def apply(self, action: str, kind: str, namespace: str | None, name: str) -> None:
        self.calls.append((action, kind, namespace, name))


class _ApprovalGatedBridge(FakeBridge):
    """This module's stand-in for the perimeter behind `agent_request_write`.

    It mirrors the shape `ui/write_coordinator.py` implements — approval,
    then a fail-closed intent audit, then the mutation — but it is *not*
    that coordinator, and nothing here is evidence about the production
    ordering; `tests/ui/test_write_coordinator.py` owns that. What this
    class exists to observe is the half the transport swap could break:
    that the agent's only route to a mutation is `agent_request_write`, and
    that a refusal from the **real** `AuditLog` on the other side of it
    stops the mutation being constructed at all.

    `approved` stands for the keystroke and is set by the *test*, never by
    anything the model can reach: the only entrypoint the agent has to this
    object is `agent_request_write`, and it never writes that field.
    """

    def __init__(self, audit: AuditLog, *, approved: bool = False) -> None:
        super().__init__()
        self.audit = audit
        self.approved = approved
        self.confirm_calls = 0
        self.write_ops = WriteOps()

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        self.confirm_calls += 1
        if not self.approved:
            return f"denied: the user declined the {action} request for {kind}/{name}"
        try:
            await asyncio.to_thread(
                self.audit.append,
                action=action,
                kind=kind,
                namespace=namespace,
                name=name,
                detail="requested by the agent",
                outcome="intent",
            )
        except OSError:
            # Fail closed (AGENTS.md): no record, no mutation. The message
            # names the shape that failed, never the audit path.
            return f"ERROR: {action} {kind}/{name} blocked: the intent audit could not be written"
        await self.write_ops.apply(action, kind, namespace, name)
        return f"approved and executed: {action} {kind}/{name}"


class ManifestKube:
    """A read port answering one manifest, whatever is asked of it."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self.manifest = manifest

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        copy: dict[str, Any] = json.loads(json.dumps(self.manifest))
        return copy


@dataclass
class Boundary:
    """One engine on a mock transport, plus everything a test inspects."""

    engine: NativeAgentEngine
    conversation: ConversationState
    gateway: RequestGateway
    policy: CountingPolicy
    resolved: ResolvedAgentPolicy
    wire: Wire
    execution: RecordedExecution
    events: list[AgentEvent] = field(default_factory=list)

    def request(self, user_text: str) -> AgentTurnRequest:
        return AgentTurnRequest(
            prompt=ComposedPrompt(
                system_message="korvid safety contract: cite your evidence.",
                user_message=user_text,
            ),
            policy=self.resolved,
            interaction=interaction(1),
        )

    async def run(self, user_text: str = "why is api-0 failing?") -> list[AgentEvent]:
        """Drive one whole turn and collect its events."""
        self.events = [event async for event in self.engine.run(self.request(user_text))]
        return self.events

    @property
    def snapshot(self) -> dict[str, Any]:
        """The payload the session would show as 'what was sent'."""
        latest = self.gateway.latest_outbound_payload
        assert latest is not None
        parsed: dict[str, Any] = json.loads(latest.payload_json)
        return parsed


def build(
    handlers: Sequence[Handler],
    *,
    tool_names: Sequence[str] = ("get_logs",),
    execution: RecordedExecution | None = None,
) -> Boundary:
    """Wire the real runtime onto a mock transport.

    Every client built here is registered for close at test teardown by
    `_close_clients`: a test that leaves one open leaks a connection pool
    into whatever runs next in the same session.
    """
    from openai import AsyncOpenAI

    wire = Wire(handlers)
    client = AsyncOpenAI(
        base_url=BASE_URL,
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
        # The SDK's own retry would replay a request the boundary prepared
        # once, and every count in this module would stop meaning anything.
        max_retries=0,
    )
    _OPEN_CLIENTS.append(client)
    provider = LiteLLMProvider(
        plan=build_plan(
            model="openai/gpt-4o",
            api_key=API_KEY,
            base_url=BASE_URL,
            options={},
            supported=[],
        ),
        descriptor=ModelDescriptor("openai", "gpt-4o"),
        client=client,
    )
    resolved = make_policy(tool_names=tool_names)
    schemas = [json.loads(json.dumps(schema)) for schema in resolved.tools]
    policy = CountingPolicy(
        request_char_budget(
            max_history_chars=resolved.max_history_chars,
            tools_chars=len(json.dumps(schemas)),
        ),
        resolve_result_formats(schemas),
    )
    conversation = ConversationState(max_history_chars=resolved.max_history_chars)
    gateway = RequestGateway(provider, policy)
    executor = execution if execution is not None else RecordingExecution()
    tools = ToolHarness(
        policy=resolved,
        execution=executor,
        bridge=RecordingBridge(),
        evidence=EvidenceLedger(),
    )
    return Boundary(
        engine=NativeAgentEngine(conversation=conversation, gateway=gateway, tools=tools),
        conversation=conversation,
        gateway=gateway,
        policy=policy,
        resolved=resolved,
        wire=wire,
        execution=executor,
    )


def cluster_executor(kube: Any, ui: Any = None) -> ToolExecutor:
    """A real `ToolExecutor` over the ports a boundary test supplies."""
    return ToolExecutor(
        kube,
        build_alias_map([PODS_META, DEPLOYMENTS_META, SECRETS_META]),
        ui=ui,
    )


def tool_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every tool-result message of one recorded request."""
    return [dict(message) for message in messages if message.get("role") == "tool"]


def assistant_calls(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every tool call stored on the assistant messages of one request."""
    calls: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "assistant":
            calls.extend(message.get("tool_calls") or [])
    return calls


# ---------------------------------------------------------------------------
# The outbound policy, and the payload snapshot
# ---------------------------------------------------------------------------


async def test_the_outbound_policy_still_runs_before_the_wire() -> None:
    """Order is: prepare_messages -> OutboundPolicy -> transport. A
    provider that reshaped messages inside complete() would bypass
    sanitization, size checks and the payload snapshot."""
    boundary = build([_answers("nothing to worry about")])

    await boundary.run(f"the operator pasted api_key: {LEAKED} into the chat — is that bad?")

    sent_body = boundary.wire.sent()
    assert LEAKED not in sent_body
    assert MASK_PLACEHOLDER in sent_body
    assert boundary.policy.prepare_calls == 1
    assert boundary.wire.requests == 1


async def test_the_payload_snapshot_matches_the_bytes_actually_sent() -> None:
    """The user-visible 'what was sent' panel must not be a
    reconstruction."""
    boundary = build(
        [
            _asks("c1", "get_logs", '{"pod": "api-0", "namespace": "prod"}'),
            _answers("crashlooping on a bad config [E1]"),
        ],
        execution=RecordingExecution({"get_logs": "back-off restarting failed container"}),
    )

    await boundary.run()

    snapshot = boundary.snapshot
    assert json.loads(boundary.wire.sent())["messages"] == snapshot["messages"]
    assert boundary.wire.payload()["tools"] == snapshot["tools"]
    assert boundary.wire.requests == 2


async def test_the_snapshot_records_the_model_the_request_was_addressed_to() -> None:
    """`OutboundSnapshot.model` names the model, never the endpoint —
    a payload export has to say where the data went.

    Naming *a* model is not enough: it has to be the one the bytes were
    addressed to. A boundary that snapshotted the resolved model and then
    let the transport send a fallback would export a payload attributed to
    a model that never saw it, so the wire's own `model` field is compared
    against the snapshot rather than only against a literal.
    """
    boundary = build([_answers("fine")])

    await boundary.run()

    latest = boundary.gateway.latest_outbound_payload
    assert latest is not None
    assert latest.model == "gpt-4o"
    assert latest.iteration == 1
    assert boundary.wire.payload()["model"] == latest.model
    exported = json.loads(latest.export_json())
    assert exported["model"] == boundary.wire.payload()["model"]
    assert exported["payload"]["messages"] == boundary.wire.messages()


# ---------------------------------------------------------------------------
# Approval, and the audit
# ---------------------------------------------------------------------------


async def test_a_write_tool_still_requires_approval_through_the_transport(
    tmp_path: Path,
) -> None:
    """The model asking is not the model doing."""
    bridge = _ApprovalGatedBridge(AuditLog(tmp_path / "audit.jsonl"), approved=False)
    boundary = build(
        [
            _asks("c1", "delete_resource", '{"kind": "deployments", "name": "web"}'),
            _answers("I cannot delete it without your approval"),
        ],
        tool_names=("delete_resource",),
        execution=cluster_executor(ManifestKube({"kind": "Deployment"}), ui=bridge),
    )

    await boundary.run("delete the web deployment")

    assert bridge.confirm_calls == 1
    assert bridge.write_ops.calls == []


async def test_a_denied_approval_is_reported_back_to_the_model_as_denied(
    tmp_path: Path,
) -> None:
    """The model has to learn the write did not happen — from the same
    result channel every other tool answer arrives on."""
    bridge = _ApprovalGatedBridge(AuditLog(tmp_path / "audit.jsonl"), approved=False)
    boundary = build(
        [
            _asks("c1", "delete_resource", '{"kind": "deployments", "name": "web"}'),
            _answers("understood, nothing was deleted"),
        ],
        tool_names=("delete_resource",),
        execution=cluster_executor(ManifestKube({"kind": "Deployment"}), ui=bridge),
    )

    await boundary.run("delete the web deployment")

    results = tool_messages(boundary.wire.messages())
    assert len(results) == 1
    assert "denied" in str(results[0]["content"])
    assert bridge.write_ops.calls == []


async def test_an_approved_write_reaches_the_cluster_exactly_once(tmp_path: Path) -> None:
    """The gate really opens. Without this every `write_ops.calls == []`
    above would be a stub that could never have fired, and the audit
    record would prove nothing about ordering."""
    audit_path = tmp_path / "audit.jsonl"
    bridge = _ApprovalGatedBridge(AuditLog(audit_path), approved=True)
    boundary = build(
        [
            _asks("c1", "delete_resource", '{"kind": "deployments", "name": "web"}'),
            _answers("the deployment is gone"),
        ],
        tool_names=("delete_resource",),
        execution=cluster_executor(ManifestKube({"kind": "Deployment"}), ui=bridge),
    )

    await boundary.run("delete the web deployment")

    assert bridge.confirm_calls == 1
    assert bridge.write_ops.calls == [("delete", "deployments", None, "web")]
    recorded = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert [entry["action"] for entry in recorded] == ["delete"]


async def test_an_audit_write_failure_still_blocks_the_action(tmp_path: Path) -> None:
    """Fail-closed. Unchanged by the transport, and worth a test that
    says so at this seam."""
    not_a_directory = tmp_path / "audit-dir"
    not_a_directory.write_text("this is a file, so no record can be written under it\n")
    bridge = _ApprovalGatedBridge(AuditLog(not_a_directory / "audit.jsonl"), approved=True)
    boundary = build(
        [
            _asks("c1", "delete_resource", '{"kind": "deployments", "name": "web"}'),
            _answers("the deletion was blocked"),
        ],
        tool_names=("delete_resource",),
        execution=cluster_executor(ManifestKube({"kind": "Deployment"}), ui=bridge),
    )

    await boundary.run("delete the web deployment")

    assert bridge.confirm_calls == 1
    assert bridge.write_ops.calls == []
    results = tool_messages(boundary.wire.messages())
    assert "blocked" in str(results[0]["content"])
    assert str(tmp_path) not in boundary.wire.sent()


# ---------------------------------------------------------------------------
# Conversation repair
# ---------------------------------------------------------------------------


async def test_conversation_repair_still_pairs_tool_calls_with_results() -> None:
    """A fragmented tool call reassembled by the new provider must
    produce the same repaired history the old adapters did."""
    boundary = build(
        [
            _streaming(
                _chunk(tool_calls=[_fragment(0, call_id="c1", name="get_logs", arguments="")]),
                _chunk(tool_calls=[_fragment(0, arguments='{"pod": "api-0",')]),
                _chunk(tool_calls=[_fragment(0, arguments=' "namespace": "prod"}')]),
            ),
            _answers("back-off restarting [E1]"),
        ],
        execution=RecordingExecution({"get_logs": "back-off restarting failed container"}),
    )

    await boundary.run()

    messages = boundary.wire.messages()
    calls = assistant_calls(messages)
    assert [call["id"] for call in calls] == ["c1"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"pod": "api-0", "namespace": "prod"}
    assert [result["tool_call_id"] for result in tool_messages(messages)] == ["c1"]
    assert boundary.execution.calls == [  # type: ignore[attr-defined]  # RecordingExecution
        ("get_logs", {"pod": "api-0", "namespace": "prod"})
    ]


async def test_a_truncated_tool_call_leaves_a_history_the_next_turn_can_send() -> None:
    """A call whose stream never finished is not a call, even when the
    fragments received so far happen to parse. Storing one would leave an
    assistant message no result can ever pair with, and every later
    request would be blocked by the outbound policy."""
    boundary = build(
        [
            _truncated("c1", "get_logs", '{"pod": "api-0", "namespace": "prod"}'),
            _answers("I could not read the logs"),
        ],
        execution=RecordingExecution({"get_logs": "back-off restarting failed container"}),
    )

    first = await boundary.run()
    second = await boundary.run("try again")

    assert isinstance(first[-1], AgentError)
    assert boundary.execution.calls == []  # type: ignore[attr-defined]  # RecordingExecution
    assert not boundary.conversation.has_unmatched_tool_calls
    assert assistant_calls(boundary.wire.messages()) == []
    assert isinstance(second[-1], TurnComplete)


async def test_a_complete_but_invalid_tool_call_never_reaches_the_tools() -> None:
    """A whole call whose arguments are not JSON is refused above the port.

    The truncated case above is the stream failing. This is the *model*
    failing: the stream completed, the call carries an id and a name, and
    only the arguments are unusable. The engine's contract for that is
    explicit — arguments cross the provider verbatim (`litellm_provider`
    repairs nothing), `_parse_arguments` refuses anything that is not a
    JSON object, and `ToolHarness.reject` answers it without touching the
    executor. A provider that "helpfully" parsed loosely, or an engine
    that fell back to `{}`, would dispatch a call the model never made —
    and for a write tool that is a mutation nobody asked for.

    So: nothing reaches the tools, the stored call keeps the raw text the
    model actually emitted, the model is told why on the ordinary result
    channel, and the turn finishes with a history that pairs.
    """
    raw_arguments = "{'pod': 'api-0', 'namespace': 'prod'}"
    boundary = build(
        [
            _asks("c1", "get_logs", raw_arguments),
            _answers("I will retry with valid arguments"),
        ],
        execution=RecordingExecution({"get_logs": "back-off restarting failed container"}),
    )

    events = await boundary.run()

    assert boundary.execution.calls == []  # type: ignore[attr-defined]  # RecordingExecution
    messages = boundary.wire.messages()
    calls = assistant_calls(messages)
    assert [call["function"]["arguments"] for call in calls] == [raw_arguments]
    results = tool_messages(messages)
    assert [result["tool_call_id"] for result in results] == ["c1"]
    assert str(results[0]["content"]) == "ERROR: tool arguments must be a JSON object"
    assert not boundary.conversation.has_unmatched_tool_calls
    assert isinstance(events[-1], TurnComplete)


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings(
    # litellm 1.98.0 inspects a usage-only delta attribute by attribute to
    # decide whether it is empty, reading `model_fields` off the instance on
    # the way past — deprecated in pydantic 2.11. korvid cannot fix that
    # upstream and must not stop exercising the one chunk shape that carries
    # a provider's own counts; `tests/providers/test_litellm_provider.py`
    # ignores it at the same seam for the same reason.
    "ignore:Accessing the 'model_"
)
async def test_measured_usage_is_committed_once_and_never_estimated() -> None:
    """The counts a turn reports are the provider's own, committed once.

    `ConversationState.commit_usage` *accumulates* and marks the iteration
    exactly counted, so this one assertion separates three failures a
    provider-level usage test cannot see: a boundary that handed the same
    frame over twice (doubled cost), one that dropped it (a heuristic
    estimate charged as if measured), and one that let LiteLLM's
    synthesized tokenizer tail through as a measurement.

    The counts are deliberately unroundable — no estimate of this payload
    lands on 137/29 — so `estimated is False` is corroborated by the
    numbers rather than trusted on its own.
    """
    boundary = build([_answers_with_usage("all quiet", prompt_tokens=137, completion_tokens=29)])

    events = await boundary.run()

    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert (complete.input_tokens, complete.output_tokens) == (137, 29)
    assert not complete.estimated
    # The counts exist because the plan asked for them: LiteLLM only sends
    # a usage frame when `stream_options.include_usage` rides on the wire.
    assert boundary.wire.payload()["stream_options"] == {"include_usage": True}


async def test_a_provider_that_reports_nothing_is_estimated_rather_than_charged_zero() -> None:
    """The other half of the rule: unknown tokens are not zero tokens.

    Without this, `..._committed_once_and_never_estimated` could be
    satisfied by a boundary that never estimates anything, and a round
    LiteLLM answered with its own tokenizer guess would be indistinguishable
    from one the provider measured.
    """
    boundary = build([_answers("all quiet")])

    events = await boundary.run()

    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert complete.estimated
    assert complete.input_tokens > 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_a_cancelled_turn_leaves_no_orphaned_stream() -> None:
    """The HTTP response is closed where the turn stops, not whenever the
    collector gets to it: an abandoned stream holds a connection, and on a
    real provider it keeps generating."""
    started = asyncio.Event()
    responses: list[httpx.Response] = []
    boundary = build([_stalling(started, responses)])

    async def drive() -> list[AgentEvent]:
        return [event async for event in boundary.engine.run(boundary.request("why?"))]

    task = asyncio.create_task(drive())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert [response.is_closed for response in responses] == [True]
    interrupted = boundary.conversation.finalize_interrupt()
    assert interrupted.input_tokens > 0
    assert not boundary.conversation.has_unmatched_tool_calls


# ---------------------------------------------------------------------------
# Sent, versus intended
# ---------------------------------------------------------------------------


async def test_request_sent_still_distinguishes_sent_from_intended() -> None:
    """A request that never reached the provider must leave the previous
    real handoff on display: the panel says what was *sent*, not what was
    built."""
    boundary = build([_answers("all quiet"), _unreachable()])

    answered = await boundary.run("first question")
    handed_over = boundary.snapshot

    unreachable = await boundary.run("second question")

    assert isinstance(answered[-1], TurnComplete)
    assert isinstance(unreachable[-1], AgentError)
    assert boundary.snapshot == handed_over
    assert handed_over["messages"] == boundary.wire.messages(0)


async def test_a_truncated_answer_ends_the_turn_in_operator_language() -> None:
    """The whole point of issue #336, end to end.

    A stream that stops without the provider ever saying it finished must
    not become a stored assistant answer, and the failure the operator
    reads must be korvid's written sentence rather than a class name — the
    engine withholds the text of any exception the contract did not
    declare safe.
    """
    boundary = build([_streaming(_chunk(content="half an ans"), finish=None)])

    events = await boundary.run()

    error = events[-1]
    assert isinstance(error, AgentError)
    assert "ended before" in error.message
    assert "ProviderStreamTruncatedError" not in error.message
    assert [message["role"] for message in boundary.conversation.messages] == ["user"]


async def test_a_truncated_tool_round_dispatches_nothing() -> None:
    """A call the model never finished writing is not a call. The harness
    would have no way to tell it from one the model meant to send."""
    recorder = RecordingExecution()
    boundary = build(
        [
            _streaming(
                _chunk(tool_calls=[_fragment(0, call_id="c1", name="get_logs", arguments="{}")]),
                finish=None,
            )
        ],
        execution=recorder,
    )

    events = await boundary.run()

    assert isinstance(events[-1], AgentError)
    assert recorder.names == []


async def test_an_answered_failure_still_counts_as_sent() -> None:
    """An HTTP 500 means the provider has the payload. The panel must show
    that payload rather than a stale one."""
    boundary = build([_refusing(500, {"error": {"message": "upstream exploded"}})])

    events = await boundary.run()

    assert isinstance(events[-1], AgentError)
    assert boundary.snapshot["messages"] == boundary.wire.messages(0)


async def test_a_transport_retry_never_re_enters_the_boundary() -> None:
    """Measured on litellm 1.98.0, the SDK retries a failed request itself.

    Whatever it retries, it must re-send the bytes the boundary already
    prepared: a retry that rebuilt the payload — a fallback model, a
    reshaped body — would put content on the wire that no snapshot
    records and no policy checked.

    The identical-bodies assertion only means something once a retry has
    actually happened, so the retry itself is asserted first: one prepared
    request that reached the socket once would satisfy `len(set(...)) == 1`
    while proving nothing at all.
    """
    boundary = build([_refusing(500, {"error": {"message": "upstream exploded"}})])

    await boundary.run()

    assert boundary.wire.requests > 1
    assert boundary.policy.prepare_calls == 1
    assert len(set(boundary.wire.bodies)) == 1


async def test_a_refused_credential_is_reported_without_the_credential() -> None:
    """A 401 body quotes the key it refused. The panel gets the written
    translation instead — the only part an operator can act on."""
    boundary = build(
        [_refusing(401, {"error": {"message": f"Incorrect API key provided: {API_KEY}"}})]
    )

    events = await boundary.run()

    error = events[-1]
    assert isinstance(error, AgentError)
    assert API_KEY not in error.message
    assert "credential" in error.message


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


async def test_masking_still_applies_to_a_sensitive_read_result() -> None:
    """A Secret read by the model is masked where the document is
    produced — in the stored history, before any outbound pass gets a
    second chance at it — and never appears on the wire in any round."""
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "db", "namespace": "prod"},
        "type": "Opaque",
        "data": {"password": LEAKED},
    }
    boundary = build(
        [
            _asks("c1", "get_resource", '{"kind": "secrets", "name": "db", "namespace": "prod"}'),
            _answers("the secret exists and its value is masked [E1]"),
        ],
        tool_names=("get_resource",),
        execution=cluster_executor(ManifestKube(secret)),
    )

    await boundary.run("does the db secret exist?")

    stored = tool_messages(boundary.conversation.messages)[0]
    assert LEAKED not in str(stored["content"])
    assert MASK_PLACEHOLDER in str(stored["content"])
    result = tool_messages(boundary.wire.messages())[0]
    assert LEAKED not in boundary.wire.sent()
    assert MASK_PLACEHOLDER in str(result["content"])
    latest = boundary.gateway.latest_outbound_payload
    assert latest is not None
    assert any(record.reason == "secret-value" for record in latest.redactions)
