import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import yaml

from korvid.agent.events import (
    AgentError,
    TextDelta,
    ToolCallFinished,
)
from korvid.agent.provider import REQUEST_SENT
from korvid.agent.runtime import AgentRuntime
from korvid.k8s.discovery import PODS_META
from korvid.k8s.errors import ApiStatusError
from korvid.tools.executor import (
    READ_TOOLS,
    RecordedExecution,
    ToolExecutor,
    ToolOutcome,
    as_recorded,
)
from korvid.tools.registry import CustomToolResult
from tests.agent.runtime_fakes import (
    _ERROR_SHAPED_SECRET,
    EchoExecutor,
    ScriptedProvider,
    _CustomExecutor,
    _deep_manifest_executor,
    _get_resource_turn,
    collect,
)


async def test_tool_call_with_non_mapping_arguments_is_rejected() -> None:
    """Valid JSON that is not an argument mapping never reaches the executor."""
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "[]"},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    events = await collect(AgentRuntime(p, EchoExecutor()), "logs?")
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert not finished.ok
    assert finished.summary == "ERROR: bad arguments"


async def test_retarget_swaps_prompt_but_preserves_history() -> None:
    """`:ctx` re-arms the runtime in place (issue #36): the system prompt
    describes the new cluster while earlier conversation turns survive."""
    p = ScriptedProvider(
        [
            [{"type": "text_delta", "text": "hi"}, {"type": "done"}],
            [{"type": "text_delta", "text": "again"}, {"type": "done"}],
        ]
    )
    rt = AgentRuntime(p, EchoExecutor(), cluster_context="The cluster runs on Azure (AKS).")
    await collect(rt, "hello")

    rt.retarget(tools=[], cluster_context="The cluster runs on AWS (EKS).")
    await collect(rt, "still there?")

    system = p.calls[1][0]
    assert system["role"] == "system"
    assert "The cluster runs on AWS (EKS)." in system["content"]
    assert "Azure" not in system["content"]
    history = [str(m.get("content") or "") for m in p.calls[1]]
    assert any("hello" in content for content in history)


async def test_latest_snapshot_is_set_before_each_call_and_tracks_last_iteration() -> None:
    class ObservingProvider:
        def __init__(self) -> None:
            self.runtime: AgentRuntime | None = None
            self.calls = 0
            self.observed: list[Any] = []

        @property
        def name(self) -> str:
            return "observing"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            assert self.runtime is not None
            self.calls += 1
            # A built-in acknowledges the transport before anything else;
            # only then is the payload a handoff (PR #197 review).
            yield {"type": REQUEST_SENT}
            self.observed.append(getattr(self.runtime, "latest_outbound_payload", None))
            if self.calls == 1:
                yield {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"}
            else:
                yield {"type": "text_delta", "text": "done"}
            yield {"type": "done"}

    provider = ObservingProvider()
    runtime = AgentRuntime(provider, EchoExecutor())
    provider.runtime = runtime

    await collect(runtime, "inspect")

    assert [snapshot.iteration for snapshot in provider.observed] == [1, 2]
    assert runtime.latest_outbound_payload is provider.observed[-1]


async def test_latest_snapshot_survives_a_blocked_next_turn() -> None:
    """The inspector shows the latest request that was actually handed over.

    A blocked turn sends nothing, so it has no payload of its own to show.
    Clearing on its way out would delete the evidence of the last real
    request — exactly when a user runs `:ai payload` to find out what
    left the machine (issue #189).
    """
    provider = ScriptedProvider([[{"type": "text_delta", "text": "ok"}, {"type": "done"}]])
    runtime = AgentRuntime(
        provider,
        EchoExecutor(),
        max_history_chars=20_000,
        max_request_chars=20_000,
    )
    await collect(runtime, "first")
    sent = runtime.latest_outbound_payload
    assert sent is not None

    events = await collect(runtime, "x" * 20_000)

    assert len(provider.calls) == 1
    assert any(
        isinstance(event, AgentError) and "outbound policy blocked" in event.message
        for event in events
    )
    assert runtime.latest_outbound_payload is sent
    assert "first" in json.loads(sent.payload_json)["messages"][1]["content"]


async def test_latest_snapshot_survives_a_turn_rolled_back_mid_flight() -> None:
    """A rollback restores history; it must not also erase what was sent.

    Turn two's first iteration really did reach the provider, so that
    iteration's snapshot is the latest handoff even though the turn it
    belonged to was dropped.
    """

    class MalformedSecretExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return json.dumps({"kind": "Secret", "data": "raw-secret"})

    provider = ScriptedProvider(
        [
            [{"type": "text_delta", "text": "ok"}, {"type": "done"}],
            [
                {"type": "tool_call", "id": "c1", "name": "get_resource", "arguments": "{}"},
                {"type": "done"},
            ],
        ]
    )
    runtime = AgentRuntime(provider, MalformedSecretExecutor())
    await collect(runtime, "first")
    first = runtime.latest_outbound_payload
    assert first is not None

    events = await collect(runtime, "second")

    assert len(provider.calls) == 2
    assert any(
        isinstance(event, AgentError) and "outbound policy blocked" in event.message
        for event in events
    )
    latest = runtime.latest_outbound_payload
    assert latest is not None
    assert latest is not first
    assert latest.iteration == 1
    assert "second" in json.loads(latest.payload_json)["messages"][-1]["content"]
    assert "raw-secret" not in latest.payload_json


async def test_provider_and_executor_mutation_cannot_change_history_or_snapshot() -> None:
    marker = "mutation-must-not-stick"
    custom_tools = [
        {
            "type": "function",
            "function": {
                "name": "get_logs",
                "description": "Fetch logs",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    class MutatingExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            arguments["nested"]["value"] = marker
            return "status=ok"

    class MutatingProvider:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def name(self) -> str:
            return "mutating"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            self.calls += 1
            messages[0]["content"] = marker
            tools[0]["function"]["description"] = marker
            if self.calls == 1:
                yield {
                    "type": "tool_call",
                    "id": "c1",
                    "name": "get_logs",
                    "arguments": '{"nested":{"value":"original"}}',
                }
            else:
                yield {"type": "text_delta", "text": "done"}
            yield {"type": "done"}

    provider = MutatingProvider()
    runtime = AgentRuntime(provider, MutatingExecutor(), tools=custom_tools)

    await collect(runtime, "inspect")

    assert marker not in json.dumps(runtime._messages)
    assert marker not in json.dumps(runtime._tools)
    snapshot = getattr(runtime, "latest_outbound_payload", None)
    assert snapshot is not None
    assert marker not in snapshot.payload_json
    payload = json.loads(snapshot.payload_json)
    assistant = next(message for message in payload["messages"] if message["role"] == "assistant")
    arguments = json.loads(assistant["tool_calls"][0]["function"]["arguments"])
    assert arguments["nested"]["value"] == "original"


def test_the_runtime_holds_its_executor_through_the_recorded_contract() -> None:
    """No private Protocol declared in the consuming layer, no isinstance
    check at each call: whatever the caller composed, the runtime holds it
    as the tools layer's ABC."""

    class Duck:
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ok"

    runtime = AgentRuntime(ScriptedProvider([]), as_recorded(Duck()))

    assert isinstance(runtime._executor, RecordedExecution)


async def test_a_string_only_executor_still_drives_a_turn() -> None:
    class Duck:
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "kind: Pod\nstatus:\n  restarts: 7\n"

    runtime = AgentRuntime(ScriptedProvider(_get_resource_turn()), as_recorded(Duck()))

    await collect(runtime, "why?")

    assert any("restarts: 7" in str(m.get("content")) for m in runtime._messages)


async def test_the_first_request_of_a_turn_is_iteration_one() -> None:
    """The exported number is what a reader counts requests with, and a
    reader counts from one — as `OutboundPolicy.prepare` now documents."""
    runtime = AgentRuntime(
        ScriptedProvider([[{"type": "text_delta", "text": "ok"}, {"type": "done"}]]), EchoExecutor()
    )

    await collect(runtime, "why?")

    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert snapshot.iteration == 1
    assert json.loads(snapshot.export_json())["iteration"] == 1


async def test_an_error_shaped_document_from_a_custom_executor_never_reaches_the_wire() -> None:
    """A string-only executor is not trusted to classify its own output:
    the text is a valid document and is redacted as one (PR #197 review)."""

    class Duck:
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return _ERROR_SHAPED_SECRET

    provider = ScriptedProvider(_get_resource_turn())
    runtime = AgentRuntime(provider, as_recorded(Duck()))

    await collect(runtime, "why?")

    wire = json.dumps(provider.calls)
    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert "cmF3LXNlY3JldA==" not in wire
    assert "cmF3LXNlY3JldA==" not in snapshot.payload_json
    assert not any("cmF3LXNlY3JldA==" in str(m.get("content")) for m in runtime._messages)


async def test_a_real_executor_error_still_reaches_the_model() -> None:
    """The executor says which branch produced the text, so an ordinary
    cluster failure is still reported rather than parsed as a document."""

    class _AngryKube:
        async def get_object(self, meta: Any, namespace: str, name: str) -> dict[str, Any]:
            raise RuntimeError("pods 'web' not found")

    runtime = AgentRuntime(
        ScriptedProvider(_get_resource_turn()),
        ToolExecutor(_AngryKube(), {"pods": PODS_META}),  # type: ignore[arg-type]  # test double for ReadOps
    )

    events = await collect(runtime, "why?")

    assert any(
        isinstance(e, ToolCallFinished) and not e.ok and "not found" in e.summary for e in events
    )
    assert any("not found" in str(m.get("content")) for m in runtime._messages)


async def test_a_tool_blocked_at_ingress_is_never_left_running() -> None:
    """The producer's block path closes its UI event before unwinding; the
    ingress pass raises from the same place and must do the same, or the
    tool row stays spinning for the rest of the session (PR #197 review)."""

    class Duck:
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ERROR: cluster said: this: is: not: yaml\n\t- [unclosed"

    provider = ScriptedProvider(_get_resource_turn())
    runtime = AgentRuntime(provider, as_recorded(Duck()))

    events = await collect(runtime, "why?")

    order = [type(e).__name__ for e in events if not isinstance(e, TextDelta)]
    assert order == ["ToolCallStarted", "ToolCallFinished", "AgentError", "TurnComplete"]
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.ok is False
    assert finished.summary == "blocked"
    assert finished.call_id == "c1"


async def test_a_turn_blocked_at_ingress_leaves_no_history_behind() -> None:
    class Duck:
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ERROR: cluster said: this: is: not: yaml\n\t- [unclosed"

    runtime = AgentRuntime(ScriptedProvider(_get_resource_turn()), as_recorded(Duck()))

    await collect(runtime, "why?")

    assert not [m for m in runtime._messages if m.get("role") in {"tool", "assistant"}]
    assert not runtime._provenance
    assert len(ScriptedProvider(_get_resource_turn()).turns) == 2


_CUSTOM_TOOL = {
    "type": "function",
    "function": {"name": "fetch_manifest", "description": "d", "parameters": {}},
}


def _custom_turn() -> list[list[dict[str, Any]]]:
    return [
        [
            {"type": "tool_call", "id": "c1", "name": "fetch_manifest", "arguments": "{}"},
            {"type": "done"},
        ],
        [{"type": "text_delta", "text": "ok"}, {"type": "done"}],
    ]


def test_a_custom_tool_without_a_declared_result_format_is_refused() -> None:
    """Offering a tool the boundary knows nothing about used to be silent,
    and its results were assumed to be text (PR #197 review)."""
    with pytest.raises(ValueError, match="result format must be declared"):
        AgentRuntime(ScriptedProvider([]), _CustomExecutor(), tools=[_CUSTOM_TOOL])


async def test_a_custom_tool_declared_structured_is_redacted_as_a_document() -> None:
    runtime = AgentRuntime(
        ScriptedProvider(_custom_turn()),
        _CustomExecutor(),
        tools=[_CUSTOM_TOOL],
        custom_tool_results=[CustomToolResult("fetch_manifest", "structured_yaml")],
    )

    events = await collect(runtime, "fetch it")

    assert not [event for event in events if isinstance(event, AgentError)]
    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert "Y2EtY2VydGlmaWNhdGUtYm9keQ==" not in snapshot.payload_json
    tool_message = next(
        message
        for message in reversed(json.loads(snapshot.payload_json)["messages"])
        if message["role"] == "tool"
    )
    assert yaml.safe_load(tool_message["content"])["kind"] == "Secret"


async def test_a_custom_tool_declared_text_keeps_the_text_pass() -> None:
    class _NotesExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "restart the deployment at 02:00"

    provider = ScriptedProvider(_custom_turn())
    runtime = AgentRuntime(
        provider,
        _NotesExecutor(),
        tools=[_CUSTOM_TOOL],
        custom_tool_results=[CustomToolResult("fetch_manifest", "untrusted_text")],
    )

    await collect(runtime, "fetch it")

    assert provider.calls[1][-1]["content"] == "restart the deployment at 02:00"


async def test_a_result_for_a_tool_that_was_never_offered_is_blocked() -> None:
    """A model-invented name has no declaration and no registry entry, so
    there is nothing to treat it as."""
    provider = ScriptedProvider(_custom_turn())
    runtime = AgentRuntime(provider, _CustomExecutor())

    events = await collect(runtime, "fetch it")

    assert len(provider.calls) == 1
    errors = [event for event in events if isinstance(event, AgentError)]
    assert errors
    assert "result format" in errors[0].message
    assert "Y2EtY2VydGlmaWNhdGUtYm9keQ==" not in json.dumps(provider.calls)


async def test_an_undeclared_tool_that_fails_still_tells_the_model_why() -> None:
    """A producer-declared failure is text either way — this must not
    become a way to lose ordinary "unknown tool" errors."""

    class _RaisingExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            raise RuntimeError("unknown tool 'fetch_manifest'")

    provider = ScriptedProvider(_custom_turn())
    runtime = AgentRuntime(provider, _RaisingExecutor())

    events = await collect(runtime, "fetch it")

    assert len(provider.calls) == 2
    assert not [event for event in events if isinstance(event, AgentError)]
    assert "unknown tool" in provider.calls[1][-1]["content"]


def test_retarget_keeps_the_declarations_the_new_surface_still_offers() -> None:
    runtime = AgentRuntime(
        ScriptedProvider([]),
        _CustomExecutor(),
        tools=[_CUSTOM_TOOL],
        custom_tool_results=[CustomToolResult("fetch_manifest", "structured_yaml")],
    )

    runtime.retarget(tools=[_CUSTOM_TOOL], cluster_context=None)
    assert runtime._result_formats == {"fetch_manifest": "structured_yaml"}

    runtime.retarget(tools=list(READ_TOOLS), cluster_context=None)
    assert "fetch_manifest" not in runtime._result_formats
    assert runtime._result_formats["get_resource"] == "structured_yaml"


def _logs_turn() -> list[list[dict[str, Any]]]:
    return [
        [
            {
                "type": "tool_call",
                "id": "c1",
                "name": "get_logs",
                "arguments": '{"pod": "api-1", "namespace": "default"}',
            },
            {"type": "done"},
        ],
        [{"type": "text_delta", "text": "ok"}, {"type": "done"}],
    ]


async def test_a_result_that_only_quotes_an_error_is_not_shown_as_a_failure() -> None:
    """A pod that logs `ERROR: db connection refused` produced a result,
    and the tool row must not tell the user the call failed."""

    class _QuotingExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ERROR: db connection refused\nERROR: retrying"

    provider = ScriptedProvider(_logs_turn())
    runtime = AgentRuntime(provider, _QuotingExecutor())

    events = await collect(runtime, "why?")

    finished = next(event for event in events if isinstance(event, ToolCallFinished))
    assert finished.ok
    assert finished.summary.startswith("ERROR: db connection refused")
    # The model still sees exactly what the tool produced.
    assert provider.calls[1][-1]["content"].startswith("ERROR: db connection refused")


async def test_a_producer_declared_failure_is_shown_as_a_failure_without_the_prefix() -> None:
    """The other direction: a failure the producer declares is a failure
    even when its text never says `ERROR:`."""

    class _DeclaringExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "the cluster said no"

        async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> Any:
            from korvid.tools.executor import ToolOutcome

            return ToolOutcome(text="the cluster said no", error=True)

    runtime = AgentRuntime(ScriptedProvider(_logs_turn()), _DeclaringExecutor())

    events = await collect(runtime, "why?")

    finished = next(event for event in events if isinstance(event, ToolCallFinished))
    assert not finished.ok
    assert finished.summary == "the cluster said no"


async def test_a_real_cluster_error_is_still_shown_as_a_failure() -> None:
    class _AngryKube:
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            raise ApiStatusError(403, "Forbidden")

    runtime = AgentRuntime(
        ScriptedProvider(_get_resource_turn()),
        ToolExecutor(_AngryKube(), {"pods": PODS_META}),  # type: ignore[arg-type]  # test double for ReadOps
    )

    events = await collect(runtime, "show me the pod")

    finished = next(event for event in events if isinstance(event, ToolCallFinished))
    assert not finished.ok
    assert "Forbidden" in finished.summary


async def test_a_blocked_result_is_still_reported_as_blocked() -> None:
    runtime = AgentRuntime(ScriptedProvider(_get_resource_turn()), _deep_manifest_executor())

    events = await collect(runtime, "show me the app")

    finished = next(event for event in events if isinstance(event, ToolCallFinished))
    assert not finished.ok
    assert finished.summary == "blocked"


def test_retarget_restores_declarations_when_the_surface_comes_back() -> None:
    """`:ctx` swaps the tool surface; swapping back must not have cost the
    caller the declarations it made at construction."""
    runtime = AgentRuntime(
        ScriptedProvider([]),
        _CustomExecutor(),
        tools=[_CUSTOM_TOOL],
        custom_tool_results=[CustomToolResult("fetch_manifest", "structured_yaml")],
    )

    runtime.retarget(tools=list(READ_TOOLS), cluster_context=None)
    assert "fetch_manifest" not in runtime._result_formats

    runtime.retarget(tools=[_CUSTOM_TOOL], cluster_context=None)

    assert runtime._result_formats == {"fetch_manifest": "structured_yaml"}


def test_retarget_to_a_disjoint_custom_surface_still_needs_its_own_declaration() -> None:
    other = {
        "type": "function",
        "function": {"name": "peek", "description": "d", "parameters": {}},
    }
    runtime = AgentRuntime(
        ScriptedProvider([]),
        _CustomExecutor(),
        tools=[_CUSTOM_TOOL],
        custom_tool_results=[CustomToolResult("fetch_manifest", "structured_yaml")],
    )

    with pytest.raises(ValueError, match="result format must be declared"):
        runtime.retarget(tools=[other], cluster_context=None)


def test_a_retarget_that_fails_leaves_the_declarations_intact() -> None:
    broken = {"type": "function", "function": {"name": "peek", "parameters": {}}}
    runtime = AgentRuntime(
        ScriptedProvider([]),
        _CustomExecutor(),
        tools=[_CUSTOM_TOOL],
        custom_tool_results=[CustomToolResult("fetch_manifest", "structured_yaml")],
    )

    with pytest.raises(ValueError, match="result format must be declared"):
        runtime.retarget(tools=[broken], cluster_context=None)

    assert runtime._tools == [_CUSTOM_TOOL]
    runtime.retarget(tools=[_CUSTOM_TOOL], cluster_context=None)
    assert runtime._result_formats == {"fetch_manifest": "structured_yaml"}


def _mock_ollama(handler: Any) -> Any:
    """A real OllamaProvider whose transport the test controls."""
    from korvid.providers.ollama import OllamaOptions, OllamaProvider

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider(
        base_url="http://x:11434",
        model="m1",
        credentials=None,
        client=client,
        options=OllamaOptions(),
    )


def _ollama_reply(text: str = "hi") -> str:
    return json.dumps({"done": True, "message": {"role": "assistant", "content": text}}) + "\n"


async def test_a_request_that_never_left_the_process_is_not_a_handoff() -> None:
    """`complete()` is an async generator: obtaining it runs nothing. A
    DNS or connect failure meant no bytes were sent, yet the inspector
    showed the payload as the latest thing this session sent."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nodename nor servname provided")

    runtime = AgentRuntime(_mock_ollama(refuse), EchoExecutor())

    events = await collect(runtime, "hello")

    assert [type(e).__name__ for e in events] == ["AgentError"]
    assert runtime.latest_outbound_payload is None


async def test_a_failed_request_leaves_the_previous_handoff_on_display() -> None:
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) > 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(
            200, text=_ollama_reply("first"), headers={"content-type": "application/x-ndjson"}
        )

    runtime = AgentRuntime(_mock_ollama(flaky), EchoExecutor())
    await collect(runtime, "one")
    first = runtime.latest_outbound_payload
    assert first is not None

    await collect(runtime, "two")

    assert runtime.latest_outbound_payload is first
    assert "two" not in first.payload_json


async def test_an_upstream_http_error_still_counts_as_a_handoff() -> None:
    """The request headers and body were on the wire before the status
    came back — the provider has the payload whatever it answered."""

    def five_hundred(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    runtime = AgentRuntime(_mock_ollama(five_hundred), EchoExecutor())

    events = await collect(runtime, "hello")

    assert any(isinstance(e, AgentError) for e in events)
    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert "hello" in snapshot.payload_json


async def test_a_successful_request_is_recorded_before_its_first_event() -> None:
    seen: list[bool] = []

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=_ollama_reply("done"), headers={"content-type": "application/x-ndjson"}
        )

    runtime = AgentRuntime(_mock_ollama(ok), EchoExecutor())
    async for event in runtime.run_turn("hello", "view=pods ns=default"):
        if isinstance(event, TextDelta):
            seen.append(runtime.latest_outbound_payload is not None)

    assert seen == [True]


async def test_the_handoff_acknowledgement_is_never_shown_to_the_user() -> None:
    """It is internal bookkeeping, not model output."""

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=_ollama_reply("done"), headers={"content-type": "application/x-ndjson"}
        )

    runtime = AgentRuntime(_mock_ollama(ok), EchoExecutor())

    events = await collect(runtime, "hello")
    text = "".join(e.text for e in events if isinstance(e, TextDelta))

    assert text == "done"
    assert "request_sent" not in text


async def test_a_provider_that_yields_nothing_is_not_a_handoff() -> None:
    """A plugin cannot acknowledge (API v1 knows four event types), so the
    conservative rule is its first completion event."""
    runtime = AgentRuntime(ScriptedProvider([[]]), EchoExecutor())

    await collect(runtime, "hello")

    assert runtime.latest_outbound_payload is None


async def test_a_plugin_style_provider_is_recorded_on_its_first_event() -> None:
    runtime = AgentRuntime(
        ScriptedProvider([[{"type": "text_delta", "text": "ok"}, {"type": "done"}]]),
        EchoExecutor(),
    )

    await collect(runtime, "hello")

    assert runtime.latest_outbound_payload is not None


async def test_a_stream_that_dies_midway_keeps_the_payload_it_sent() -> None:
    class _Dying:
        name = "dying"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "text_delta", "text": "par"}
            raise RuntimeError("stream died")

    runtime = AgentRuntime(_Dying(), EchoExecutor())

    await collect(runtime, "hello")

    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert "hello" in snapshot.payload_json


async def test_an_acknowledged_request_that_fails_is_still_charged() -> None:
    """The prompt reached the provider and was processed before it answered
    HTTP 500. Charging nothing reports a session as free that was not."""

    def five_hundred(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    runtime = AgentRuntime(_mock_ollama(five_hundred), EchoExecutor())

    await collect(runtime, "hello")

    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    charged_in, charged_out = runtime.total_tokens
    assert charged_in == len(snapshot.payload_json) // 4
    assert charged_out == 0
    assert runtime.usage_estimated is True


async def test_a_request_that_never_left_is_charged_nothing() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    runtime = AgentRuntime(_mock_ollama(refuse), EchoExecutor())

    await collect(runtime, "hello")

    assert runtime.total_tokens == (0, 0)
    assert runtime.usage_estimated is False


async def test_a_provider_that_yields_nothing_is_charged_nothing() -> None:
    """No acknowledgement and no event: there is no evidence a request ran,
    and the inspector already refuses to call it a handoff."""
    runtime = AgentRuntime(ScriptedProvider([[]]), EchoExecutor())

    await collect(runtime, "hello")

    assert runtime.total_tokens == (0, 0)
    assert runtime.latest_outbound_payload is None


async def test_a_plugin_style_first_event_is_enough_to_charge_the_prompt() -> None:
    """A plugin cannot acknowledge, so its first event is the evidence —
    the same rule the snapshot uses."""

    class _OneEventThenDies:
        @property
        def name(self) -> str:
            return "plugin"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "text_delta", "text": "par"}
            raise RuntimeError("stream died")

    runtime = AgentRuntime(_OneEventThenDies(), EchoExecutor())

    await collect(runtime, "hello")

    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert runtime.total_tokens[0] == len(snapshot.payload_json) // 4


async def test_an_acknowledged_openai_request_that_fails_is_still_charged() -> None:
    from korvid.providers.openai_compat import OpenAICompatProvider
    from korvid.providers.static_creds import StaticHeaderSource

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="nope"))
    )
    provider = OpenAICompatProvider(
        base_url="http://x/v1",
        model="m1",
        credentials=StaticHeaderSource("sk-test"),
        client=client,
    )
    runtime = AgentRuntime(provider, EchoExecutor())

    await collect(runtime, "hello")

    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert runtime.total_tokens[0] == len(snapshot.payload_json) // 4


async def test_the_runtime_takes_an_executor_that_implements_the_contract() -> None:
    class Real(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "kind: Pod\nstatus:\n  restarts: 7\n"

    runtime = AgentRuntime(ScriptedProvider(_get_resource_turn()), Real())

    await collect(runtime, "why?")

    assert runtime._executor.__class__ is Real
    assert any("restarts: 7" in str(m.get("content")) for m in runtime._messages)


def test_the_runtime_refuses_an_executor_that_only_looks_like_one() -> None:
    """Adapting a duck at the boundary made the boundary structural. The
    ABC is the contract; composing an adapter is the caller's decision."""

    class Duck:
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ok"

    with pytest.raises(TypeError, match="RecordedExecution"):
        AgentRuntime(ScriptedProvider([]), Duck())  # type: ignore[arg-type]  # the point of the test


async def test_a_caller_that_wants_a_duck_composes_the_adapter_itself() -> None:
    class Duck:
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "kind: Pod\nstatus:\n  restarts: 7\n"

    runtime = AgentRuntime(ScriptedProvider(_get_resource_turn()), as_recorded(Duck()))

    await collect(runtime, "why?")

    assert any("restarts: 7" in str(m.get("content")) for m in runtime._messages)


_REPEATED_KEY_SECRET = """kind: Secret
apiVersion: v1
metadata:
  name: tls
data:
  ca.crt: Y2EtY2VydGlmaWNhdGUtYm9keQ==
kind: ConfigMap
"""


class _RepeatedKeyExecutor(RecordedExecution):
    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        return _REPEATED_KEY_SECRET


async def test_a_custom_structured_tool_cannot_repeat_a_key_to_erase_the_classifier() -> None:
    """`kind: Secret … kind: ConfigMap` loaded as a ConfigMap that still
    carried the credentials, so the whole `data` mapping shipped (PR #197
    review). The turn stops at the result instead, and the last request
    that really was handed over stands as the latest snapshot."""
    provider = ScriptedProvider(
        [
            [{"type": "text_delta", "text": "hi"}, {"type": "done"}],
            *_custom_turn(),
        ]
    )
    runtime = AgentRuntime(
        provider,
        _RepeatedKeyExecutor(),
        tools=[_CUSTOM_TOOL],
        custom_tool_results=[CustomToolResult("fetch_manifest", "structured_yaml")],
    )
    await collect(runtime, "hello")

    events = await collect(runtime, "fetch it")

    assert [event for event in events if isinstance(event, AgentError)]
    # The turn's first request went out; the one that would have carried
    # the result never did.
    assert len(provider.calls) == 2
    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert "Y2EtY2VydGlmaWNhdGUtYm9keQ==" not in snapshot.payload_json
    assert not [
        message
        for message in json.loads(snapshot.payload_json)["messages"]
        if message["role"] == "tool"
    ]
    assert not any("Y2EtY2VydGlmaWNhdGUtYm9keQ==" in json.dumps(call) for call in provider.calls)
    assert not any(
        "Y2EtY2VydGlmaWNhdGUtYm9keQ==" in event.message
        for event in events
        if isinstance(event, AgentError)
    )


async def test_a_blocked_repeated_key_leaves_the_session_usable() -> None:
    """A refused document is one bad result, not the end of the session."""
    provider = ScriptedProvider(
        [*_custom_turn(), [{"type": "text_delta", "text": "ok"}, {"type": "done"}]]
    )
    runtime = AgentRuntime(
        provider,
        _RepeatedKeyExecutor(),
        tools=[_CUSTOM_TOOL],
        custom_tool_results=[CustomToolResult("fetch_manifest", "structured_yaml")],
    )

    blocked = await collect(runtime, "fetch it")
    recovered = await collect(runtime, "just talk")

    assert [event for event in blocked if isinstance(event, AgentError)]
    assert not [event for event in recovered if isinstance(event, AgentError)]
    assert runtime.latest_outbound_payload is not None
    assert "Y2EtY2VydGlmaWNhdGUtYm9keQ==" not in runtime.latest_outbound_payload.payload_json


async def test_the_incarnation_a_read_reports_reaches_the_ledger() -> None:
    """The UID must survive the boundary, or the citation cannot check it.

    The executor scopes `get_events` to one incarnation; if the runtime
    drops that on the way to the ledger, opening the citation after the
    pod was recreated shows the replacement silently (#250).
    """

    class IncarnationExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "BackOff"

        async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
            return ToolOutcome(text="BackOff", incarnation="uid-1")

    p = ScriptedProvider(
        [
            [
                {
                    "type": "tool_call",
                    "id": "c1",
                    "name": "get_events",
                    "arguments": '{"kind": "Pod", "namespace": "d", "name": "web"}',
                },
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(p, IncarnationExecutor())

    await collect(runtime, "events?")

    item = runtime.evidence.resolve("E1")
    assert item is not None
    assert item.incarnation == "uid-1"
