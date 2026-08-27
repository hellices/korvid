"""Offline gate: one whole local-model turn that never leaves the loopback.

korvid must be usable air-gapped. This test runs the **real**
`OllamaProvider` against an `httpx.MockTransport` bound to the configured
loopback endpoint, through the **production** `DefaultAgentSession` graph,
and fails if anything about that run reaches for the outside world:

- every HTTP request must go to the configured loopback host and port;
- name resolution and socket creation are poisoned, so a request to
  anything the mock transport does not serve fails loudly rather than
  silently succeeding on a machine that happens to be online;
- no online catalog, prompt, or telemetry service is consulted — routing
  comes from the shipped exact-match catalog and the prompts from the
  shipped packs;
- the exact outbound snapshot the user could export equals the body that
  actually crossed the boundary.

Nothing here ever performs real network I/O.
"""

from __future__ import annotations

import json
import socket
from typing import Any

import httpx
import pytest

from korvid.agent.conversation import ConversationState
from korvid.agent.events import TextDelta, ToolCallFinished, ToolCallStarted, TurnComplete
from korvid.agent.evidence import EvidenceLedger
from korvid.agent.interaction import (
    AgentUiBridge,
    ClusterFacts,
    InteractionContext,
    PaneContext,
    ResourceIdentity,
    UiAction,
    UiActionResult,
)
from korvid.agent.model_catalog import MODEL_CATALOG, MODEL_CATALOG_VERSION
from korvid.agent.model_policy import (
    CapabilitySource,
    ModelRouter,
    ModelTier,
    PolicyEnvironment,
)
from korvid.agent.native_engine import NativeAgentEngine
from korvid.agent.outbound import request_char_budget
from korvid.agent.prompt_harness import PromptHarness
from korvid.agent.prompt_packs import SAFETY_CONTRACT
from korvid.agent.request_gateway import RequestGateway
from korvid.agent.session import DefaultAgentSession
from korvid.agent.tool_harness import ToolHarness
from korvid.providers.ollama import OllamaOptions, OllamaProvider
from korvid.tools.executor import RecordedExecution

#: The air-gapped deployment's own endpoint. Every request must go here.
LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = 11434
LOCAL_BASE_URL = f"http://{LOOPBACK_HOST}:{LOOPBACK_PORT}"
LOCAL_PATH = "/api/chat"
LOCAL_ENDPOINT = f"{LOOPBACK_HOST}:{LOOPBACK_PORT}{LOCAL_PATH}"
LOCAL_MODEL = "qwen3:8b"

_LOG_TAIL = "2026-07-27T07:59:00Z level=error msg='out of memory' exit=137 OOMKilled"


def _ndjson(*chunks: dict[str, Any]) -> str:
    return "".join(json.dumps(chunk) + "\n" for chunk in chunks)


def _tool_call_body() -> str:
    return _ndjson(
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "get_logs",
                            "arguments": {"pod": "worker-1", "namespace": "jobs"},
                        },
                    }
                ],
            },
            "done": False,
        },
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 812,
            "eval_count": 41,
        },
    )


def _answer_body() -> str:
    return _ndjson(
        {
            "message": {
                "role": "assistant",
                "content": "worker-1 was OOMKilled with exit 137 [E1].",
            },
            "done": False,
        },
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 1004,
            "eval_count": 22,
        },
    )


class _LocalOnlyTransport(httpx.MockTransport):
    """A transport that answers only the configured loopback endpoint.

    The endpoint check lives in the handler itself, not only in the
    assertions afterwards: a request to anything else must fail *at the
    boundary*, before it can consume a scripted body and let the rest of
    the turn complete as if nothing had left the machine.
    """

    def __init__(self, bodies: list[str]) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies = list(bodies)

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            endpoint = f"{request.url.host}:{request.url.port}{request.url.path}"
            if endpoint != LOCAL_ENDPOINT:
                raise AssertionError(f"off-endpoint request to {request.url}")
            if not self.bodies:
                raise AssertionError(f"unscripted request to {request.url}")
            return httpx.Response(
                200,
                text=self.bodies.pop(0),
                headers={"content-type": "application/x-ndjson"},
            )

        super().__init__(handler)


class _LocalExecutor(RecordedExecution):
    """A read-only executor: no cluster client, no network, no writes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, dict(arguments)))
        return _LOG_TAIL


class _StaticBridge(AgentUiBridge):
    """One fixed workspace; UI actions are recorded, never rendered."""

    def __init__(self) -> None:
        self.actions: list[UiAction] = []
        self._context = InteractionContext(
            kube_context="airgap",
            context_epoch=1,
            focused_pane=PaneContext(
                kind="pods",
                scope="jobs",
                filter_pattern=None,
                selected=ResourceIdentity(
                    kind="Pod", namespace="jobs", name="worker-1", uid="pod-1"
                ),
            ),
            secondary_pane=None,
            timeline_cursor=None,
        )

    def snapshot(self) -> InteractionContext:
        return self._context

    async def apply(self, action: UiAction) -> UiActionResult:
        self.actions.append(action)
        return UiActionResult(ok=True, message="applied", context=self._context)


@pytest.fixture
def no_external_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poison every escape hatch a real network call would need."""

    def refuse_lookup(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"offline session attempted a name lookup: {args!r}")

    def refuse_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"offline session attempted a socket connection: {args!r}")

    monkeypatch.setattr(socket, "getaddrinfo", refuse_lookup)
    monkeypatch.setattr(socket, "gethostbyname", refuse_lookup)
    monkeypatch.setattr(socket, "create_connection", refuse_connect)


class _OfflineSession:
    """The production graph, wired around one loopback provider."""

    def __init__(self) -> None:
        self.transport = _LocalOnlyTransport([_tool_call_body(), _answer_body()])
        self.provider = OllamaProvider(
            LOCAL_BASE_URL,
            LOCAL_MODEL,
            client=httpx.AsyncClient(transport=self.transport),
            options=OllamaOptions(num_ctx=16384, temperature=0.0, seed=7),
        )
        self.policy = ModelRouter(MODEL_CATALOG).resolve(
            descriptor=self.provider.descriptor,
            provider_capabilities=self.provider.capabilities,
            explicit_tier=None,
            environment=PolicyEnvironment(
                readonly=True, resize_supported=False, observability_backends=frozenset()
            ),
        )
        self.executor = _LocalExecutor()
        self.bridge = _StaticBridge()
        self.tools = ToolHarness(
            policy=self.policy,
            execution=self.executor,
            bridge=self.bridge,
            evidence=EvidenceLedger(),
        )
        self.conversation = ConversationState(
            max_history_chars=self.policy.max_history_chars,
            strict_history_budget=self.policy.strict_history_budget,
        )
        self.gateway = RequestGateway(self.provider, RequestGateway.prepare_policy(self.policy))
        self.session = DefaultAgentSession(
            engine=NativeAgentEngine(
                conversation=self.conversation, gateway=self.gateway, tools=self.tools
            ),
            bridge=self.bridge,
            prompt_harness=PromptHarness(),
            conversation=self.conversation,
            gateway=self.gateway,
            tools=self.tools,
            policy=self.policy,
            cluster=ClusterFacts(provider="unknown", distribution=None),
        )

    async def run(self, text: str) -> list[Any]:
        try:
            return [event async for event in self.session.run_turn(text)]
        finally:
            await self.session.aclose()
            await self.provider.aclose()


@pytest.fixture
async def offline_turn(no_external_lookup: None) -> Any:
    """One completed low-tier turn, plus everything it touched."""
    offline = _OfflineSession()
    events = await offline.run("Why does worker-1 keep dying?")
    return offline, events


async def test_the_catalogued_local_model_routes_low_without_asking_anyone(
    offline_turn: Any,
) -> None:
    """The routed policy of the one provider the turn ran on — and closed."""
    offline, _events = offline_turn
    policy = offline.policy
    assert policy.tier is ModelTier.LOW
    assert policy.route_source is CapabilitySource.CATALOG
    assert policy.catalog_version == MODEL_CATALOG_VERSION
    assert policy.prompt_pack_id == "low-korvid-operator"


def test_the_offline_fixture_refuses_name_lookup_and_socket_creation(
    no_external_lookup: None,
) -> None:
    """The poison is the gate; a silently working escape hatch is the risk."""
    with pytest.raises(AssertionError, match="name lookup"):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(AssertionError, match="name lookup"):
        socket.gethostbyname("example.invalid")
    with pytest.raises(AssertionError, match="socket connection"):
        socket.create_connection(("example.invalid", 443))


def test_the_transport_refuses_an_off_host_request_before_answering_it() -> None:
    """An off-endpoint request must fail at the boundary, not be served.

    Consuming a scripted body first would let a turn that reached the
    outside world complete exactly like an air-gapped one, and only the
    after-the-fact assertions would notice.
    """
    transport = _LocalOnlyTransport([_answer_body()])

    with pytest.raises(AssertionError, match="off-endpoint request"):
        transport.handle_request(httpx.Request("POST", "http://example.invalid/api/chat"))

    assert len(transport.bodies) == 1


@pytest.mark.parametrize(
    "url",
    [
        f"http://{LOOPBACK_HOST}:11435/api/chat",
        f"http://{LOOPBACK_HOST}:{LOOPBACK_PORT}/api/generate",
        f"http://10.0.0.1:{LOOPBACK_PORT}/api/chat",
    ],
)
def test_the_transport_pins_the_exact_configured_host_port_and_path(url: str) -> None:
    transport = _LocalOnlyTransport([_answer_body()])

    with pytest.raises(AssertionError, match="off-endpoint request"):
        transport.handle_request(httpx.Request("POST", url))

    assert transport.bodies == [_answer_body()]


def test_the_transport_serves_the_configured_loopback_endpoint() -> None:
    transport = _LocalOnlyTransport([_answer_body()])

    response = transport.handle_request(httpx.Request("POST", f"{LOCAL_BASE_URL}{LOCAL_PATH}"))

    assert response.status_code == 200
    assert transport.bodies == []


async def test_every_request_goes_to_the_configured_loopback_endpoint(
    offline_turn: Any,
) -> None:
    offline, _events = offline_turn
    assert offline.transport.requests
    for request in offline.transport.requests:
        assert request.url.host == LOOPBACK_HOST, request.url
        assert request.url.port == LOOPBACK_PORT, request.url
        assert request.url.path == LOCAL_PATH, request.url
        assert request.method == "POST"


async def test_only_the_local_chat_endpoint_is_ever_contacted(offline_turn: Any) -> None:
    """No catalog service, no prompt service, no telemetry sink."""
    offline, _events = offline_turn
    hosts = {f"{r.url.host}:{r.url.port}{r.url.path}" for r in offline.transport.requests}
    assert hosts == {LOCAL_ENDPOINT}


async def test_the_turn_completes_within_the_low_tier_budgets(offline_turn: Any) -> None:
    offline, events = offline_turn
    iterations = len(offline.transport.requests)
    assert iterations == 2
    assert iterations <= offline.policy.max_iterations
    started = [event for event in events if isinstance(event, ToolCallStarted)]
    assert len(started) == 1
    assert offline.policy.max_tool_calls_per_iteration == 1
    assert offline.policy.max_result_chars == 3_000


async def test_one_read_tool_call_reaches_the_executor_and_mints_evidence(
    offline_turn: Any,
) -> None:
    offline, events = offline_turn
    assert offline.executor.calls == [("get_logs", {"pod": "worker-1", "namespace": "jobs"})]
    finished = [event for event in events if isinstance(event, ToolCallFinished)]
    assert len(finished) == 1
    assert finished[0].ok
    assert offline.session.evidence.references() == ("E1",)


async def test_the_answer_citation_is_credited_to_a_real_read(offline_turn: Any) -> None:
    _offline, events = offline_turn
    answer = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert "[E1]" in answer
    complete = [event for event in events if isinstance(event, TurnComplete)]
    assert len(complete) == 1
    assert complete[0].cited == ("E1",)
    assert complete[0].uncited == ()


async def test_a_tool_result_never_exceeds_the_policy_result_budget(
    offline_turn: Any,
) -> None:
    offline, _events = offline_turn
    body = json.loads(offline.transport.requests[-1].content)
    tool_messages = [m for m in body["messages"] if m.get("role") == "tool"]
    assert tool_messages
    for message in tool_messages:
        assert len(message["content"]) <= offline.policy.max_result_chars


async def test_the_exported_snapshot_equals_the_body_that_crossed_the_boundary(
    offline_turn: Any,
) -> None:
    """What a user can inspect must be what actually went out."""
    offline, _events = offline_turn
    snapshot = offline.session.latest_outbound_payload
    assert snapshot is not None
    assert snapshot.model == LOCAL_MODEL

    sent = json.loads(offline.transport.requests[-1].content)
    recorded = json.loads(snapshot.payload_json)
    assert sent["messages"] == recorded["messages"]
    assert sent["tools"] == recorded["tools"]
    assert sent["model"] == LOCAL_MODEL


async def test_the_request_carries_the_shipped_offline_prompt_and_surface(
    offline_turn: Any,
) -> None:
    offline, _events = offline_turn
    sent = json.loads(offline.transport.requests[0].content)
    system = sent["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith(SAFETY_CONTRACT)
    armed = {tool["function"]["name"] for tool in sent["tools"]}
    assert armed == {tool["function"]["name"] for tool in offline.policy.tools}
    assert "get_logs" in armed
    # Read-only: an air-gapped eval/offline session never offers a write.
    assert not armed & {"scale_resource", "delete_resource", "rollout_restart"}


async def test_the_request_stays_inside_the_outbound_character_ceiling(
    offline_turn: Any,
) -> None:
    offline, _events = offline_turn
    snapshot = offline.session.latest_outbound_payload
    assert snapshot is not None
    ceiling = request_char_budget(
        max_history_chars=offline.policy.max_history_chars,
        tools_chars=len(json.dumps(json.loads(snapshot.payload_json)["tools"])),
    )
    assert len(snapshot.payload_json) <= ceiling


async def test_token_usage_comes_from_the_local_server(offline_turn: Any) -> None:
    offline, events = offline_turn
    complete = next(event for event in events if isinstance(event, TurnComplete))
    assert complete.estimated is False
    assert offline.session.total_tokens == (812 + 1004, 41 + 22)
