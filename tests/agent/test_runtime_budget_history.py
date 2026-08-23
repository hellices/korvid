import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import yaml

from korvid.agent.events import (
    AgentError,
    TextDelta,
    ToolCallFinished,
    TurnComplete,
)
from korvid.agent.model_policy import ModelDescriptor
from korvid.agent.profiles import build_profile
from korvid.agent.runtime import MAX_HISTORY_TURNS, AgentRuntime
from korvid.tools.executor import (
    MAX_RESULT_CHARS,
    READ_TOOLS,
    RecordedExecution,
)
from tests.agent.runtime_fakes import (
    EchoExecutor,
    ScriptedProvider,
    _bulk_pod_manifest,
    _bulk_text_executor,
    _get_resource_provider,
    _manifest_executor,
    _profile_runtime,
    _read_tools_request_ceiling,
    _tool_then_text_script,
    collect,
)


async def test_history_persists_across_turns() -> None:
    p = ScriptedProvider(
        [
            [{"type": "text_delta", "text": "a"}, {"type": "done"}],
            [{"type": "text_delta", "text": "b"}, {"type": "done"}],
        ]
    )
    rt = AgentRuntime(p, EchoExecutor())
    await collect(rt, "first")
    await collect(rt, "second")
    contents = [m.get("content", "") for m in p.calls[1]]
    assert any("first" in c for c in contents)
    assert any(m["role"] == "assistant" for m in p.calls[1])


async def test_usage_accumulates() -> None:
    p = ScriptedProvider(
        [
            [
                {"type": "text_delta", "text": "x"},
                {"type": "usage", "input_tokens": 100, "output_tokens": 7},
                {"type": "done"},
            ]
        ]
    )
    rt = AgentRuntime(p, EchoExecutor())
    events = await collect(rt, "q")
    tc = next(e for e in events if isinstance(e, TurnComplete))
    assert (tc.input_tokens, tc.output_tokens, tc.estimated) == (100, 7, False)
    assert rt.total_tokens == (100, 7)


async def test_usage_accumulates_across_tool_iterations() -> None:
    """Usage from intermediate (tool-calling) iterations must count toward the turn."""
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
                {"type": "usage", "input_tokens": 50, "output_tokens": 5},
                {"type": "done"},
            ],
            [
                {"type": "text_delta", "text": "answer"},
                {"type": "usage", "input_tokens": 70, "output_tokens": 9},
                {"type": "done"},
            ],
        ]
    )
    rt = AgentRuntime(p, EchoExecutor())
    events = await collect(rt, "q")
    tc = next(e for e in events if isinstance(e, TurnComplete))
    assert (tc.input_tokens, tc.output_tokens, tc.estimated) == (120, 14, False)
    assert rt.total_tokens == (120, 14)


async def test_history_trimmed_to_recent_turns() -> None:
    text_turn = [{"type": "text_delta", "text": "ok"}, {"type": "done"}]
    p = ScriptedProvider([list(text_turn) for _ in range(12)])
    runtime = AgentRuntime(p, EchoExecutor())
    for i in range(12):
        _ = await collect(runtime, f"question {i}")
    last_call = p.calls[-1]
    user_msgs = [m for m in last_call if m["role"] == "user"]
    assert len(user_msgs) <= MAX_HISTORY_TURNS
    assert last_call[0]["role"] == "system"
    # oldest turns dropped, newest kept
    assert "question 0" not in str(last_call)
    assert "question 11" in str(last_call)


async def test_provider_error_still_accounts_usage() -> None:
    tool_turn: list[dict[str, Any]] = [
        {"type": "usage", "input_tokens": 40, "output_tokens": 7},
        {"type": "tool_call", "id": "c", "name": "get_logs", "arguments": "{}"},
        {"type": "done"},
    ]

    class FailsSecondCall(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            self.calls.append([dict(m) for m in messages])
            if len(self.calls) > 1:
                raise RuntimeError("api down")
            for ev in tool_turn:
                yield ev

    runtime = AgentRuntime(FailsSecondCall([]), EchoExecutor())
    events = await collect(runtime, "q")
    assert any(isinstance(e, AgentError) for e in events)
    assert runtime.total_tokens == (40, 7)


async def test_provider_error_estimates_streamed_text_without_usage() -> None:
    """A stream that dies after emitting text but before its usage event
    still cost output tokens — the exception path must apply the same
    estimate as the normal path, not record zero."""

    class DiesMidStream(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "text_delta", "text": "x" * 40}
            raise RuntimeError("connection dropped")

    runtime = AgentRuntime(DiesMidStream([]), EchoExecutor())
    events = await collect(runtime, "q")
    assert any(isinstance(e, AgentError) for e in events)
    in_tok, out_tok = runtime.total_tokens
    assert out_tok == 10
    assert in_tok > 0  # the prompt was really sent — estimated, not zero
    assert runtime.usage_estimated is True


async def test_usage_estimated_is_sticky() -> None:
    no_usage: list[dict[str, Any]] = [{"type": "text_delta", "text": "hi"}, {"type": "done"}]
    with_usage: list[dict[str, Any]] = [
        {"type": "text_delta", "text": "hi"},
        {"type": "usage", "input_tokens": 5, "output_tokens": 2},
        {"type": "done"},
    ]
    runtime = AgentRuntime(ScriptedProvider([no_usage, with_usage]), EchoExecutor())
    assert runtime.usage_estimated is False
    _ = await collect(runtime, "first")
    assert runtime.usage_estimated is True
    _ = await collect(runtime, "second")
    assert runtime.usage_estimated is True  # sticky: earlier totals remain estimates


async def test_usage_missing_in_any_iteration_marks_estimated() -> None:
    """Exactness requires usage from EVERY iteration, not just one."""
    p = ScriptedProvider(
        [
            [
                # tool-calling iteration omits usage …
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
                {"type": "done"},
            ],
            [
                # … final iteration reports it — turn is still an estimate.
                {"type": "text_delta", "text": "answer"},
                {"type": "usage", "input_tokens": 70, "output_tokens": 9},
                {"type": "done"},
            ],
        ]
    )
    rt = AgentRuntime(p, EchoExecutor())
    events = await collect(rt, "q")
    tc = next(e for e in events if isinstance(e, TurnComplete))
    assert tc.estimated is True
    assert rt.usage_estimated is True


async def test_history_trimmed_by_char_budget() -> None:
    """A few huge turns must not blow the request size even under the turn cap."""
    big = "x" * 4000
    p = ScriptedProvider(
        [[{"type": "text_delta", "text": big}, {"type": "done"}] for _ in range(4)]
    )
    rt = AgentRuntime(
        p,
        EchoExecutor(),
        tools=[],
        max_history_chars=10_000,
        strict_history_budget=True,
    )
    for i in range(4):
        await collect(rt, f"question-{i} {big}")
    # The last provider call must fit the budget…
    last_call_chars = sum(len(str(m.get("content") or "")) for m in p.calls[-1])
    assert last_call_chars <= 10_000 + len(big) + 100  # budget + newest user msg
    # …and the newest turn is always retained.
    assert any("question-3" in str(m.get("content") or "") for m in p.calls[-1])
    assert not any("question-0" in str(m.get("content") or "") for m in p.calls[-1])


async def test_missing_usage_estimates_prompt_tokens_too() -> None:
    """A provider that omits usage must not record zero input tokens: the
    prompt was really sent, so both directions get the heuristic estimate."""
    no_usage: list[dict[str, Any]] = [
        {"type": "text_delta", "text": "a diagnosis long enough to estimate"},
        {"type": "done"},
    ]
    runtime = AgentRuntime(ScriptedProvider([no_usage]), EchoExecutor())
    _ = await collect(runtime, "a question long enough to estimate")
    in_tok, out_tok = runtime.total_tokens
    assert in_tok > 0
    assert out_tok > 0
    assert runtime.usage_estimated is True


async def test_missing_usage_estimates_include_tool_schemas_and_payloads() -> None:
    """Prompt estimates must cover the transmitted tool schemas, and output
    estimates the generated tool-call payload — a tool-only iteration is
    not free just because the provider omitted usage."""
    import json

    args = json.dumps({"pod": "checkout-1", "namespace": "shop", "container": "app"})
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": args},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(p, EchoExecutor())
    _ = await collect(runtime, "logs?")
    in_tok, out_tok = runtime.total_tokens
    # The tool-call name + JSON arguments dominate the tiny "done" text.
    assert out_tok >= (len("get_logs") + len(args)) // 4
    # A content-only estimate is far below the serialized schema cost that
    # every request really transmits.
    assert in_tok > len(json.dumps(READ_TOOLS)) // 4
    assert runtime.usage_estimated is True


async def test_missing_usage_estimates_measure_the_prepared_payload() -> None:
    """The estimate must measure what was sent, not runtime history.

    A provider dialect hook runs inside the boundary and can add real
    payload — Ollama re-attaches `thinking`, names the executed tool,
    expands arguments into objects. Counting `_messages` plus the tool
    schemas cannot see any of it, so a usage-less request is billed for
    a payload smaller than the one the provider received (PR #197
    review).
    """
    thinking = "reasoning about the tool result. " * 500

    class DialectProvider(ScriptedProvider):
        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor("test", "dialect")

        def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {**message, "thinking": thinking} if message.get("role") == "assistant" else message
                for message in messages
            ]

    provider = DialectProvider(
        [
            [{"type": "text_delta", "text": "first"}, {"type": "usage", "in": 5, "out": 5}],
            [{"type": "text_delta", "text": "second"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(provider, EchoExecutor())

    await collect(runtime, "first question")
    events = await collect(runtime, "second question")

    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert thinking in json.loads(snapshot.payload_json)["messages"][2]["thinking"]
    complete = next(event for event in events if isinstance(event, TurnComplete))
    assert complete.estimated is True
    assert complete.input_tokens == len(snapshot.payload_json) // 4


async def test_provider_error_estimates_measure_the_prepared_payload() -> None:
    """The same exact payload backs the estimate when the stream dies."""

    class DyingProvider(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "text_delta", "text": "partial"}
            raise RuntimeError("connection dropped")

    runtime = AgentRuntime(DyingProvider([]), EchoExecutor())
    events = await collect(runtime, "a question")

    assert any(isinstance(event, AgentError) for event in events)
    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert runtime.total_tokens[0] == len(snapshot.payload_json) // 4


async def test_provider_error_after_tool_call_estimates_output() -> None:
    """A stream that dies after emitting only a tool call (no text) still
    generated output — its payload is charged, not recorded as zero."""

    class DiesAfterToolCall(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {
                "type": "tool_call",
                "id": "c1",
                "name": "get_logs",
                "arguments": '{"pod": "checkout-1", "namespace": "shop"}',
            }
            raise RuntimeError("connection dropped")

    runtime = AgentRuntime(DiesAfterToolCall([]), EchoExecutor())
    events = await collect(runtime, "q")
    assert any(isinstance(e, AgentError) for e in events)
    in_tok, out_tok = runtime.total_tokens
    assert out_tok > 0
    assert in_tok > 0
    assert runtime.usage_estimated is True


async def test_runtime_caps_tool_results_at_max_result_chars() -> None:
    """A per-result cap below the executor's own 8k limit must bind — the
    small profile (issue #71) sizes it so one full turn of results fits
    inside its retained-history budget."""

    class HugeExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "x" * 5_000

    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(p, HugeExecutor(), max_result_chars=1_000)
    await collect(runtime, "logs?")
    tool_msgs = [m for m in p.calls[1] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert len(tool_msgs[0]["content"]) <= 1_000 + len("\n… [truncated — narrow the query]")
    assert "truncated" in tool_msgs[0]["content"]


async def test_profile_result_cap_preserves_the_tail_evidence() -> None:
    """diagnose_pod places Warning events and log excerpts last by design;
    a prefix-only cap would chop the most diagnostic sections. The profile
    cap must keep both ends of an oversized result."""

    class HeadTailExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "IDENTITY line\n" + "x" * 5_000 + "\nLOG EXCERPT: OOMKilled"

    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "diagnose_pod", "arguments": "{}"},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(p, HeadTailExecutor(), max_result_chars=1_000)
    await collect(runtime, "diagnose")
    tool_msg = next(m for m in p.calls[1] if m["role"] == "tool")
    assert "IDENTITY line" in tool_msg["content"]
    assert "LOG EXCERPT: OOMKilled" in tool_msg["content"]
    assert "truncated" in tool_msg["content"]
    assert len(tool_msg["content"]) < 1_200


async def test_tool_call_limit_per_iteration_is_enforced() -> None:
    """The small prompt says 'call one tool at a time' but text does not
    enforce anything: extra parallel calls must be discarded so one
    iteration cannot blow the per-turn size bound."""
    executed: list[str] = []

    class SpyExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            executed.append(name)
            return "ok"

    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
                {"type": "tool_call", "id": "c2", "name": "get_events", "arguments": "{}"},
                {"type": "tool_call", "id": "c3", "name": "get_resource", "arguments": "{}"},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(p, SpyExecutor(), max_tool_calls_per_iteration=1)
    events = await collect(runtime, "go")
    assert executed == ["get_logs"]
    assistant = next(m for m in p.calls[1] if m["role"] == "assistant")
    assert len(assistant["tool_calls"]) == 1  # excess calls never enter history
    tool_msgs = [m for m in p.calls[1] if m["role"] == "tool"]
    assert len(tool_msgs) == 1  # matches the stored assistant tool calls
    assert "2 extra tool call" in tool_msgs[0]["content"]
    assert "one tool at a time" in tool_msgs[0]["content"]
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert [f.ok for f in finished] == [True, False, False]


async def test_discarded_excess_calls_do_not_grow_history() -> None:
    """Refusing execution is not enough: the arguments of excess parallel
    calls (and per-call refusal messages) must not be retained either, or a
    model emitting many large parallel calls each iteration still exceeds
    the history budget mid-turn (trimming never drops the newest turn)."""

    class SpyExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ok"

    huge = json.dumps({"manifest": "x" * 50_000})
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
                {"type": "tool_call", "id": "c2", "name": "apply", "arguments": huge},
                {"type": "tool_call", "id": "c3", "name": "apply", "arguments": huge},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(p, SpyExecutor(), max_tool_calls_per_iteration=1)
    await collect(runtime, "go")
    retained = json.dumps(p.calls[1])
    assert "x" * 1_000 not in retained
    assert len(retained) < 2_000


async def test_in_turn_history_budget_ends_the_turn_early() -> None:
    """Capping tool results does not bound history growth by itself:
    assistant text and kept-call arguments are stored verbatim, and
    trimming never drops the sole current turn. A follow-up provider call
    must not be sent once the in-turn total exceeds the history budget."""

    class SpyExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ok"

    huge = json.dumps({"manifest": "x" * 30_000})
    # A second provider call would exhaust this one-batch script and raise;
    # the budget guard must end the turn before requesting it.
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": huge},
                {"type": "done"},
            ],
        ]
    )
    runtime = AgentRuntime(p, SpyExecutor(), max_history_chars=10_000, strict_history_budget=True)
    events = await collect(runtime, "go")
    assert len(p.calls) == 1
    errors = [e for e in events if isinstance(e, AgentError)]
    assert any("budget" in e.message for e in errors)
    assert any(isinstance(e, TurnComplete) for e in events)


async def test_history_budget_stays_soft_without_strict_mode() -> None:
    """Soft history mode skips the legacy mid-turn guard, but the final
    provider-boundary policy still rejects an oversized follow-up request."""

    class SpyExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ok"

    huge = json.dumps({"manifest": "x" * 30_000})
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "apply", "arguments": huge},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(p, SpyExecutor(), max_history_chars=10_000)
    events = await collect(runtime, "go")
    assert len(p.calls) == 1
    assert any(
        isinstance(event, AgentError) and "outbound policy blocked" in event.message
        for event in events
    )
    # The first iteration was prepared and sent; only the follow-up was
    # blocked, so the sent request stays inspectable.
    sent = runtime.latest_outbound_payload
    assert sent is not None
    assert sent.iteration == 1


async def test_strict_trim_drops_an_oversized_sole_previous_turn() -> None:
    """After the mid-turn guard ends a turn early, that turn is oversized
    forever; iteration zero of the NEXT turn sends unconditionally, so
    strict trimming must drop the oversized completed turn instead of
    resending it (the default keeps it — most recent turn always retained)."""

    class SpyExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ok"

    huge = json.dumps({"manifest": "x" * 30_000})
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "apply", "arguments": huge},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "hello"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(p, SpyExecutor(), max_history_chars=10_000, strict_history_budget=True)
    await collect(runtime, "first")  # ends early over budget
    await collect(runtime, "second")
    first_request = json.dumps(p.calls[1])
    assert "x" * 1_000 not in first_request  # oversized turn not resent
    assert "second" in first_request
    assert len(first_request) < 10_000


async def test_strict_mode_rejects_a_prompt_that_cannot_fit() -> None:
    """Iteration zero sends unconditionally, so strict mode must catch an
    over-budget first request before it goes over the wire: after trimming
    with the new user message in place, a prompt that cannot fit by itself
    is rejected — and dropped, so it cannot poison later turns."""

    class SpyExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ok"

    p = ScriptedProvider(
        [
            [{"type": "text_delta", "text": "hi"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(p, SpyExecutor(), max_history_chars=20_000, strict_history_budget=True)
    events = await collect(runtime, "x" * 30_000)
    assert len(p.calls) == 0  # never sent
    errors = [e for e in events if isinstance(e, AgentError)]
    assert any("too large" in e.message for e in errors)
    assert any(isinstance(e, TurnComplete) for e in events)
    # The rejected prompt was dropped: a normal follow-up works cleanly.
    events2 = await collect(runtime, "hello")
    assert len(p.calls) == 1
    assert "x" * 1_000 not in json.dumps(p.calls[0])
    assert not any(isinstance(e, AgentError) for e in events2)


@pytest.mark.parametrize("profile_name", ["full", "small"])
async def test_profiles_keep_a_hard_cap_on_the_final_provider_request(profile_name: str) -> None:
    """The reconciled ceiling is still a real cap, not an open door.

    A prompt several times the profile's own history budget cannot be made
    to fit by dropping older turns, so it must be rejected before the
    provider is called — and the session must still accept the next,
    reasonable prompt (issue #189).
    """
    profile = build_profile(profile_name, readonly=False, resize_supported=True)
    provider = ScriptedProvider([[{"type": "text_delta", "text": "recovered"}, {"type": "done"}]])
    runtime = AgentRuntime(
        provider,
        EchoExecutor(),
        tools=profile.tools,
        max_iterations=profile.max_iterations,
        max_history_chars=profile.max_history_chars,
        max_result_chars=profile.max_result_chars,
        max_tool_calls_per_iteration=profile.max_tool_calls_per_iteration,
        strict_history_budget=profile.strict_history_budget,
        system_prompt=profile.system_prompt,
        ui_prompt=profile.ui_prompt,
    )

    events = await collect(runtime, "x" * (profile.max_history_chars * 4))

    assert len(provider.calls) == 0
    assert any(isinstance(event, AgentError) for event in events)
    assert getattr(runtime, "latest_outbound_payload", None) is None

    recovered = await collect(runtime, "inspect")

    assert not [event for event in recovered if isinstance(event, AgentError)]
    assert recovered[0] == TextDelta(text="recovered")
    assert len(provider.calls) == 1


async def test_oversized_manifest_reaches_the_model_as_bounded_valid_yaml() -> None:
    """A benign manifest larger than the 8k ingest cap must not block the
    turn: capping happens *before* the policy parses the structured
    result, so a byte-level truncation would make it unparsable YAML and
    the whole turn would be rolled back (issue #189 final review)."""
    executor = _manifest_executor(_bulk_pod_manifest(labels=200))
    provider = _get_resource_provider()
    runtime = AgentRuntime(provider, executor)

    events = await collect(runtime, "show me the pod")

    assert not [event for event in events if isinstance(event, AgentError)]
    assert isinstance(events[-1], TurnComplete)
    assert len(provider.calls) == 2
    tool_message = provider.calls[1][-1]
    assert tool_message["role"] == "tool"
    manifest = yaml.safe_load(tool_message["content"])
    assert isinstance(manifest, dict)
    assert manifest["kind"] == "Pod"
    assert manifest["metadata"]["name"] == "api-0"
    assert len(tool_message["content"]) <= MAX_RESULT_CHARS
    payload = json.dumps(provider.calls)
    assert "last-applied-hunter2" not in payload
    assert "env-hunter2" not in payload
    assert "env-raw-key" not in payload
    # The benign turn stays in history: the next question keeps its context.
    assert any(message["role"] == "tool" for message in runtime._messages)


async def test_small_profile_bounds_oversized_manifest_without_blocking() -> None:
    """Same failure with the small profile's tighter per-result cap: its
    head+tail compaction cuts the YAML mid-document (issue #189)."""
    profile = build_profile("small", readonly=True, resize_supported=False)
    assert profile.max_result_chars is not None
    executor = _manifest_executor(_bulk_pod_manifest(labels=60))
    provider = _get_resource_provider()
    runtime = AgentRuntime(
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
    )

    events = await collect(runtime, "show me the pod")

    assert not [event for event in events if isinstance(event, AgentError)]
    assert isinstance(events[-1], TurnComplete)
    assert len(provider.calls) == 2
    tool_message = provider.calls[1][-1]
    manifest = yaml.safe_load(tool_message["content"])
    assert isinstance(manifest, dict)
    assert manifest["kind"] == "Pod"
    assert len(tool_message["content"]) <= profile.max_result_chars
    payload = json.dumps(provider.calls)
    assert "last-applied-hunter2" not in payload
    assert "env-hunter2" not in payload


async def test_full_profile_session_survives_a_near_budget_previous_turn() -> None:
    """A turn that fills the retained-history budget must not brick the
    session. The policy ceiling was the *same number* as the message-only
    history budget, so the serialized payload (tool schemas, JSON
    envelopes, escaping) always overshot it: every later prompt was
    blocked and erased, with no way back except `:ai off` (issue #189)."""
    script = _tool_then_text_script(14, "here is the summary")
    script.append([{"type": "text_delta", "text": "second answer"}, {"type": "done"}])
    script.append([{"type": "text_delta", "text": "third answer"}, {"type": "done"}])
    provider = ScriptedProvider(script)
    runtime = _profile_runtime("full", provider, _bulk_text_executor(8_000))

    first = await collect(runtime, "investigate the outage")
    assert not [event for event in first if isinstance(event, AgentError)]
    assert len(provider.calls) == 15

    second = await collect(runtime, "and now?")
    third = await collect(runtime, "anything else?")

    assert not [event for event in second if isinstance(event, AgentError)]
    assert not [event for event in third if isinstance(event, AgentError)]
    assert second[0] == TextDelta(text="second answer")
    assert third[0] == TextDelta(text="third answer")
    assert len(provider.calls) == 17
    assert runtime.latest_outbound_payload is not None


async def test_small_profile_session_survives_a_near_budget_previous_turn() -> None:
    """Same reconciliation for the small profile's tighter budgets: its
    retained turn plus a long follow-up question fits the history budget
    but not the identically-sized policy ceiling."""
    script = _tool_then_text_script(5, "here is the summary")
    script.append([{"type": "text_delta", "text": "second answer"}, {"type": "done"}])
    provider = ScriptedProvider(script)
    runtime = _profile_runtime("small", provider, _bulk_text_executor(3_000))

    first = await collect(runtime, "investigate the outage")
    assert not [event for event in first if isinstance(event, AgentError)]

    second = await collect(runtime, "and now? explain each failing container. " * 75)

    assert not [event for event in second if isinstance(event, AgentError)]
    assert second[0] == TextDelta(text="second answer")
    assert runtime.latest_outbound_payload is not None


async def test_over_ceiling_request_drops_the_oldest_turn_and_still_reaches_the_model() -> None:
    """An oversized payload is recoverable, not terminal.

    Trimming the oldest retained turn shrinks the same conversation until
    it fits, so a long session keeps working. The current prompt is never
    the thing that gets dropped (issue #189)."""
    provider = ScriptedProvider(
        [
            [{"type": "text_delta", "text": "first answer"}, {"type": "done"}],
            [{"type": "text_delta", "text": "second answer"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(
        provider,
        EchoExecutor(),
        max_history_chars=40_000,
        max_request_chars=_read_tools_request_ceiling(10_000),
    )

    first = await collect(runtime, "a" * 6_000)
    second = await collect(runtime, "b" * 6_000)

    assert not [event for event in first if isinstance(event, AgentError)]
    assert not [event for event in second if isinstance(event, AgentError)]
    assert second[0] == TextDelta(text="second answer")
    sent = json.dumps(provider.calls[1])
    assert "b" * 6_000 in sent
    assert "a" * 6_000 not in sent
    assert runtime.latest_outbound_payload is not None


async def test_a_blocked_oversized_prompt_leaves_the_session_usable() -> None:
    """The sole current prompt is reported, never silently dropped from
    the request — and the *next* prompt must still go through, so the
    session never needs `:ai off` to recover (issue #189)."""
    provider = ScriptedProvider([[{"type": "text_delta", "text": "recovered"}, {"type": "done"}]])
    runtime = AgentRuntime(
        provider,
        EchoExecutor(),
        max_history_chars=60_000,
        max_request_chars=12_000,
    )

    blocked = await collect(runtime, "z" * 30_000)
    recovered = await collect(runtime, "short question")

    assert [event for event in blocked if isinstance(event, AgentError)]
    assert len(provider.calls) == 1
    assert not [event for event in recovered if isinstance(event, AgentError)]
    assert recovered[0] == TextDelta(text="recovered")
    assert "z" * 30_000 not in json.dumps(provider.calls[0])


async def test_strict_rejection_after_trimming_does_not_poison_later_turns() -> None:
    """Rejecting an unfittable prompt must delete *that* prompt.

    The pre-flight trims history first, which shifts every index down; a
    rollback computed before the trim then pointed past the end and left
    the rejected prompt in history, blocking every later turn (issue
    #189)."""
    script = _tool_then_text_script(5, "here is the summary")
    script.append([{"type": "text_delta", "text": "recovered"}, {"type": "done"}])
    provider = ScriptedProvider(script)
    runtime = _profile_runtime("small", provider, _bulk_text_executor(3_000))

    await collect(runtime, "investigate the outage")
    blocked = await collect(runtime, "y" * 30_000)
    recovered = await collect(runtime, "short follow-up")

    assert any(
        isinstance(event, AgentError) and "too large for the history budget" in event.message
        for event in blocked
    )
    assert not [event for event in recovered if isinstance(event, AgentError)]
    assert recovered[0] == TextDelta(text="recovered")
    assert "y" * 1_000 not in json.dumps(runtime._messages)
    assert "y" * 1_000 not in json.dumps(provider.calls[-1])


async def test_rollback_after_recovery_trimming_removes_the_whole_blocked_turn() -> None:
    """A rollback must delete the turn it belongs to, not a stale slice.

    Recovery trimming shifts every index down mid-turn; a rollback that
    still used the pre-trim offset left a half-turn (here: an assistant
    message with duplicate tool-call IDs) in history, which the policy
    rejects on every later turn — a permanently bricked session (issue
    #189)."""
    provider = ScriptedProvider(
        [
            [{"type": "text_delta", "text": "first answer"}, {"type": "done"}],
            [
                {
                    "type": "tool_call",
                    "id": "c1",
                    "name": "get_logs",
                    "arguments": '{"pod":"api-0","namespace":"prod"}',
                },
                {"type": "done"},
            ],
            [
                {"type": "tool_call", "id": "dup", "name": "get_logs", "arguments": "{}"},
                {"type": "tool_call", "id": "dup", "name": "get_logs", "arguments": "{}"},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "third answer"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(
        provider,
        _bulk_text_executor(2_500),
        max_history_chars=40_000,
        max_request_chars=12_000,
    )

    await collect(runtime, "a" * 3_000)
    blocked = await collect(runtime, "b" * 1_000)
    recovered = await collect(runtime, "third question")

    assert any(
        isinstance(event, AgentError) and "outbound policy blocked" in event.message
        for event in blocked
    )
    assert "b" * 1_000 not in json.dumps(runtime._messages)
    assert not [event for event in recovered if isinstance(event, AgentError)]
    assert recovered[-1] == TurnComplete(
        input_tokens=recovered[-1].input_tokens,
        output_tokens=recovered[-1].output_tokens,
        estimated=recovered[-1].estimated,
    )
    assert TextDelta(text="third answer") in recovered


async def test_estimated_prompt_cost_reflects_the_history_actually_sent() -> None:
    """A usage-less provider is charged for the request that shipped.

    Recovery trimming happens during preparation, so an estimate taken
    before it would bill the caller for turns that were dropped from the
    payload (issue #189)."""
    provider = ScriptedProvider(
        [
            [{"type": "text_delta", "text": "first answer"}, {"type": "done"}],
            [{"type": "text_delta", "text": "second answer"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(
        provider,
        EchoExecutor(),
        max_history_chars=40_000,
        max_request_chars=_read_tools_request_ceiling(10_000),
    )

    first = await collect(runtime, "a" * 6_000)
    assert not [event for event in first if isinstance(event, AgentError)]
    first_total_in = runtime.total_tokens[0]
    second = await collect(runtime, "b" * 6_000)
    assert not [event for event in second if isinstance(event, AgentError)]

    sent_chars = len(json.dumps(provider.calls[1], ensure_ascii=False))
    second_turn_in = runtime.total_tokens[0] - first_total_in
    assert runtime.usage_estimated
    assert second_turn_in <= (sent_chars + runtime._tools_chars) // 4
    assert second_turn_in < (sent_chars + runtime._tools_chars + 6_000) // 4
