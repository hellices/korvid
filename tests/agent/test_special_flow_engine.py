"""A packaged SpecialFlow driven through the real provider factory and engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pytest

from korvid.agent.conversation import ConversationState
from korvid.agent.engine import AgentTurnRequest
from korvid.agent.events import (
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
)
from korvid.agent.evidence import EvidenceLedger
from korvid.agent.native_engine import NativeAgentEngine
from korvid.agent.outbound import OutboundPolicy, request_char_budget
from korvid.agent.prompt_harness import ComposedPrompt
from korvid.agent.provider import LLMProvider
from korvid.agent.request_gateway import RequestGateway
from korvid.agent.tool_harness import ToolHarness
from korvid.core.config import ModelConnectionConfig
from korvid.core.secrets import MASK_PLACEHOLDER
from korvid.providers.litellm_factory import create_provider_from_profile
from korvid.providers.special_flows import SpecialFlowRegistry
from korvid.tools.executor import RecordedExecution, ToolOutcome
from korvid.tools.registry import resolve_result_formats
from tests.agent.engine_fakes import (
    TURN_SYSTEM_MESSAGE,
    RecordingBridge,
    RecordingExecution,
    interaction,
    make_policy,
)
from tests.fixtures.provider_flow.site_helpers import (
    FIXTURES_DIR,
    build_dist_info,
    discover_provider_entry_points,
)


@runtime_checkable
class _RecordingProvider(Protocol):
    calls: list[list[dict[str, Any]]]


def _install_plugin_site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build_dist_info(
        tmp_path,
        dist_name="company_provider",
        version="1.0",
        entry_point_name="company-llm",
        entry_point_value="company_provider",
    )
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: [entry for entry, _dist in discover_provider_entry_points(tmp_path)],
    )


def _packaged_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    scripted_turns: list[list[object]],
) -> LLMProvider:
    """The provider a deployment gets: discovered, loaded, then created."""
    _install_plugin_site(monkeypatch, tmp_path)
    provider = create_provider_from_profile(
        ModelConnectionConfig(
            model="company-llm/fixture-model",
            endpoint="https://fixtures.example.test/v1",
            options={"scripted_turns": scripted_turns},
        ),
        flows=SpecialFlowRegistry.from_entry_points(),
    )
    assert provider is not None
    assert type(provider).__module__ == "company_provider"
    return provider


def _engine(
    provider: LLMProvider,
    execution: RecordedExecution,
) -> tuple[NativeAgentEngine, AgentTurnRequest]:
    policy = make_policy(tool_names=("get_logs",))
    schemas = [json.loads(json.dumps(schema)) for schema in policy.tools]
    outbound = OutboundPolicy(
        request_char_budget(
            max_history_chars=policy.max_history_chars,
            tools_chars=len(json.dumps(schemas)),
        ),
        resolve_result_formats(schemas),
    )
    engine = NativeAgentEngine(
        conversation=ConversationState(max_history_chars=policy.max_history_chars),
        gateway=RequestGateway(provider, outbound),
        tools=ToolHarness(
            policy=policy,
            execution=execution,
            bridge=RecordingBridge(),
            evidence=EvidenceLedger(),
        ),
    )
    request = AgentTurnRequest(
        prompt=ComposedPrompt(system_message=TURN_SYSTEM_MESSAGE, user_message="show me the logs"),
        policy=policy,
        interaction=interaction(),
    )
    return engine, request


async def _drive(engine: NativeAgentEngine, request: AgentTurnRequest) -> list[AgentEvent]:
    return [event async for event in engine.run(request)]


def _tool_then_text() -> list[list[object]]:
    """One tool round then a text answer, with junk keys the plugin must drop.

    Built fresh per test: the fixture provider pops from the list it is
    handed, so a shared module-level script would empty itself into
    whichever test happened to run first.
    """
    return [
        [
            {
                "type": "tool_call",
                "id": "c1",
                "name": "get_logs",
                "arguments": "{}",
                "ignored": "x",
            },
            {"type": "usage", "input_tokens": 50, "output_tokens": 5, "ignored": "x"},
            {"type": "done", "ignored": "x"},
        ],
        [
            {"type": "text_delta", "text": "done", "ignored": "x"},
            {"type": "usage", "input_tokens": 70, "output_tokens": 9, "ignored": "x"},
            {"type": "done", "ignored": "x"},
        ],
    ]


async def test_a_packaged_provider_rejects_unscripted_completions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = _packaged_provider(monkeypatch, tmp_path, scripted_turns=[[{"type": "done"}]])

    assert [event async for event in provider.complete([], [])] == [{"type": "done"}]
    with pytest.raises(AssertionError, match="scripted turns exhausted on completion 2"):
        await anext(provider.complete([], []))

    assert isinstance(provider, _RecordingProvider)
    assert len(provider.calls) == 2
    await provider.aclose()


async def test_a_packaged_plugin_drives_a_whole_engine_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The composed path: entry point -> SpecialFlow -> factory -> engine."""
    provider = _packaged_provider(monkeypatch, tmp_path, scripted_turns=_tool_then_text())
    execution = RecordingExecution({"get_logs": "result-of-get_logs"})
    engine, request = _engine(provider, execution)

    events = await _drive(engine, request)

    assert [type(event).__name__ for event in events] == [
        "ToolCallStarted",
        "ToolCallFinished",
        "TextDelta",
        "TurnComplete",
    ]
    assert events[0] == ToolCallStarted(call_id="c1", name="get_logs", arguments="{}")
    assert isinstance(events[1], ToolCallFinished)
    assert events[1].ok is True
    assert events[2] == TextDelta(text="done")
    assert events[3] == TurnComplete(input_tokens=120, output_tokens=14, estimated=False)
    assert execution.names == ["get_logs"]
    assert isinstance(provider, _RecordingProvider)
    assert len(provider.calls) == 2
    await engine.aclose()


async def test_a_packaged_plugin_only_ever_sees_a_sanitized_tool_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The masking pipeline runs before the third party, not after it.

    A provider plugin is code korvid did not write, reached over a network
    korvid does not control. If a Secret's data reached it verbatim, the
    masking pipeline would be protecting only the screen.
    """
    sentinel = "plugin-must-not-see-this-secret"

    class _SecretExecution(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return (await self.execute_recorded(name, arguments)).text

        async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
            del name, arguments
            return ToolOutcome(text=f"apiVersion: v1\nkind: Secret\ndata:\n  token: {sentinel}\n")

    provider = _packaged_provider(monkeypatch, tmp_path, scripted_turns=_tool_then_text())
    engine, request = _engine(provider, _SecretExecution())

    events = await _drive(engine, request)

    assert isinstance(provider, _RecordingProvider)
    assert len(provider.calls) == 2
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, AgentError) for event in events)
    sent = json.dumps(provider.calls, ensure_ascii=False)
    assert sentinel not in sent
    assert MASK_PLACEHOLDER in sent
    await engine.aclose()


async def test_a_plugin_failure_carrying_a_credential_reaches_the_panel_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plugin's own exception text is never trusted onto the screen."""
    provider = _packaged_provider(
        monkeypatch,
        tmp_path,
        scripted_turns=[[{"type": "__raise_contract_error__"}]],
    )
    engine, request = _engine(provider, RecordingExecution())

    events = await _drive(engine, request)

    assert [type(event).__name__ for event in events] == ["AgentError"]
    error = events[0]
    assert isinstance(error, AgentError)
    assert "SECRET_INTERNAL_TOKEN_xyz789" not in error.message
    assert isinstance(provider, _RecordingProvider)
    assert len(provider.calls) == 1
    await engine.aclose()


async def test_a_plugin_that_names_no_tool_dispatches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed call is discarded before any port, not after it."""
    provider = _packaged_provider(
        monkeypatch,
        tmp_path,
        scripted_turns=[
            [{"type": "tool_call", "id": "c1", "name": "", "arguments": "{}"}, {"type": "done"}],
            [{"type": "text_delta", "text": "sorry"}, {"type": "done"}],
        ],
    )
    execution = RecordingExecution()
    engine, request = _engine(provider, execution)

    events = await _drive(engine, request)

    assert execution.calls == []
    assert isinstance(provider, _RecordingProvider)
    assert len(provider.calls) == 1
    assert [type(event).__name__ for event in events] == [
        "ToolCallStarted",
        "ToolCallFinished",
        "AgentError",
        "TurnComplete",
    ]
    error = events[2]
    assert isinstance(error, AgentError)
    assert error.message == "no usable tool call in the response — turn ended early"
    await engine.aclose()
