from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from korvid.agent.events import (
    AgentError,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
)
from korvid.agent.provider_plugin import ProviderPluginConfig
from korvid.agent.runtime import AgentRuntime
from korvid.core.secrets import MASK_PLACEHOLDER
from korvid.providers.plugin_registry import ProviderPluginRegistry
from korvid.tools.executor import RecordedExecution
from tests.agent.test_runtime import EchoExecutor, collect
from tests.fixtures.provider_plugin.site_helpers import (
    FIXTURES_DIR,
    build_dist_info,
    discover_provider_entry_points,
)


def _install_plugin_site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build_dist_info(
        tmp_path,
        dist_name="company_provider",
        version="1.0",
        entry_point_name="company-llm",
        entry_point_value="company_provider:CompanyProviderPlugin",
    )
    build_dist_info(
        tmp_path,
        dist_name="unselected_provider",
        version="1.0",
        entry_point_name="unselected-thing",
        entry_point_value="unselected_provider:UnselectedPlugin",
    )
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    monkeypatch.setattr(
        "korvid.providers.plugin_registry._discover_entry_points",
        lambda: discover_provider_entry_points(tmp_path),
    )


def _create_runtime_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    scripted_turns: list[list[object]],
) -> object:
    _install_plugin_site(monkeypatch, tmp_path)
    registry = ProviderPluginRegistry()
    registry.load_selected("company-llm")
    return registry.create(
        "company-llm",
        ProviderPluginConfig(
            base_url="https://fixtures.example.test/v1",
            model="fixture-model",
            auth_method="api_key",
            api_key_env=None,
            options={"scripted_turns": scripted_turns},
        ),
        credentials=None,
    )


async def test_packaged_plugin_runtime_normalizes_text_usage_tool_and_done(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _create_runtime_provider(
        monkeypatch,
        tmp_path,
        scripted_turns=[
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
        ],
    )

    runtime = AgentRuntime(cast("Any", provider), EchoExecutor())
    events = await collect(runtime, "show me the logs")

    assert [type(event).__name__ for event in events] == [
        "ToolCallStarted",
        "ToolCallFinished",
        "TextDelta",
        "TurnComplete",
    ]
    assert events[0] == ToolCallStarted(call_id="c1", name="get_logs", arguments="{}")
    assert events[1] == ToolCallFinished(
        call_id="c1",
        name="get_logs",
        ok=True,
        summary="result-of-get_logs",
    )
    assert events[2] == TextDelta(text="done")
    assert events[3] == TurnComplete(input_tokens=120, output_tokens=14, estimated=False)

    inner = cast("Any", provider)._provider
    assert inner.calls[0][0]["role"] == "system"
    assert inner.tools_seen[0] == runtime._tools
    assert inner.calls[1][-1]["role"] == "tool"
    assert inner.calls[1][-1]["content"] == "result-of-get_logs"


async def test_packaged_plugin_receives_masked_secret_tool_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sentinel = "plugin-must-not-see-this-secret"

    class SecretExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return (
                "apiVersion: v1\n"
                "kind: Secret\n"
                "data:\n"
                f"  token: {sentinel}\n"
                "nested:\n"
                f"  password: {sentinel}\n"
            )

    provider = _create_runtime_provider(
        monkeypatch,
        tmp_path,
        scripted_turns=[
            [
                {
                    "type": "tool_call",
                    "id": "c1",
                    "name": "get_resource",
                    "arguments": "{}",
                },
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ],
    )
    runtime = AgentRuntime(cast("Any", provider), SecretExecutor())

    events = await collect(runtime, "inspect the Secret")

    assert isinstance(events[-1], TurnComplete)
    inner = cast("Any", provider)._provider
    outbound = str(inner.calls[1][-1]["content"])
    assert sentinel not in outbound
    assert MASK_PLACEHOLDER in outbound


@pytest.mark.parametrize(
    ("bad_event", "message"),
    [
        ({"type": "usage", "input_tokens": -1, "output_tokens": 0}, "usage.input_tokens"),
        ({"type": "text_delta", "text": "x" * 65_537}, "text_delta.text"),
    ],
)
async def test_packaged_plugin_runtime_rejects_invalid_events_before_history_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bad_event: object,
    message: str,
) -> None:
    provider = _create_runtime_provider(
        monkeypatch,
        tmp_path,
        scripted_turns=[
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
                bad_event,
            ],
            [{"type": "text_delta", "text": "recovered"}, {"type": "done"}],
        ],
    )

    runtime = AgentRuntime(cast("Any", provider), EchoExecutor())
    failed = await collect(runtime, "first question")

    error = next(event for event in failed if isinstance(event, AgentError))
    assert message in error.message
    assert not any(isinstance(event, ToolCallStarted | ToolCallFinished) for event in failed)

    recovered = await collect(runtime, "second question")
    assert recovered[0] == TextDelta(text="recovered")
    assert isinstance(recovered[-1], TurnComplete)

    inner = cast("Any", provider)._provider
    second_call_messages = inner.calls[1]
    assert not any(message.get("role") == "tool" for message in second_call_messages)
    assert not any(
        message.get("role") == "assistant" and message.get("tool_calls")
        for message in second_call_messages
    )


async def test_tool_call_then_no_done_becomes_agent_error_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A tool_call followed by stream exhaustion (no done) must produce an
    AgentError without dispatching the tool or corrupting history."""
    provider = _create_runtime_provider(
        monkeypatch,
        tmp_path,
        scripted_turns=[
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
                # No done event — stream ends here
            ],
            [{"type": "text_delta", "text": "recovered"}, {"type": "done"}],
        ],
    )

    runtime = AgentRuntime(cast("Any", provider), EchoExecutor())
    failed = await collect(runtime, "first question")

    error = next(event for event in failed if isinstance(event, AgentError))
    assert "done" in error.message.lower()
    # Tool must NOT have been dispatched
    assert not any(isinstance(event, ToolCallStarted | ToolCallFinished) for event in failed)

    # Recovery: next turn must not see the corrupt tool_call in history
    recovered = await collect(runtime, "second question")
    assert recovered[0] == TextDelta(text="recovered")
    assert isinstance(recovered[-1], TurnComplete)

    inner = cast("Any", provider)._provider
    second_call_messages = inner.calls[1]
    assert not any(msg.get("role") == "tool" for msg in second_call_messages)
    assert not any(
        msg.get("role") == "assistant" and msg.get("tool_calls") for msg in second_call_messages
    )


async def test_underlying_contract_error_with_secret_becomes_agent_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A ProviderPluginContractError raised by the plugin iterator with
    secret payload must become a bounded AgentError with no secret leakage
    into history."""
    provider = _create_runtime_provider(
        monkeypatch,
        tmp_path,
        scripted_turns=[
            [
                {"type": "text_delta", "text": "hi"},
                # The fixture provider raises when it sees this sentinel
                {"type": "__raise_contract_error__"},
            ],
            [{"type": "text_delta", "text": "recovered"}, {"type": "done"}],
        ],
    )

    runtime = AgentRuntime(cast("Any", provider), EchoExecutor())
    failed = await collect(runtime, "first question")

    error = next(event for event in failed if isinstance(event, AgentError))
    assert "SECRET" not in error.message
    assert "stream failed" in error.message.lower() or "contract" in error.message.lower()

    # Recovery must work cleanly
    recovered = await collect(runtime, "second question")
    assert recovered[0] == TextDelta(text="recovered")
    assert isinstance(recovered[-1], TurnComplete)
