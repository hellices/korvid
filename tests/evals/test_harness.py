"""The eval composition helper builds production's graph (issue #316 task 13).

An eval that composed its own loop would measure a program the operator
never runs. `korvid.evals.harness` builds exactly the graph
`korvid.__main__._build_session` builds — router over `MODEL_CATALOG`,
`PromptHarness`, `ConversationState`, `RequestGateway.prepare_policy`,
`ToolHarness`, `NativeAgentEngine`, `DefaultAgentSession` — from injected
parts, and these tests pin that it stays that graph and that its write
boundary stays shut.
"""

from __future__ import annotations

from typing import Any

from korvid.agent.events import ToolCallFinished
from korvid.evals.harness import (
    EvalHarness,
    build_eval_harness,
)
from korvid.evals.interaction import EvalUiBridge, load_interaction
from korvid.evals.scripted import ScriptedProvider

_INTERACTION = {
    "kube_context": "eval-cluster",
    "context_epoch": 1,
    "focused_pane": {"kind": "pods", "scope": "jobs"},
}


class _Executor:
    """A string-only executor, exactly what the eval packs hand over."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, dict(arguments)))
        return f"{name} ok: pod worker-1 exit=137 OOMKilled"


def _bridge() -> EvalUiBridge:
    return EvalUiBridge(load_interaction(_INTERACTION, "fixture: interaction"))


def _harness(
    provider: Any = None,
    executor: Any = None,
    **kwargs: Any,
) -> EvalHarness:
    return build_eval_harness(
        provider=provider if provider is not None else ScriptedProvider([[{"type": "done"}]]),
        execution=executor if executor is not None else _Executor(),
        bridge=_bridge(),
        **kwargs,
    )


async def test_a_write_request_never_reaches_the_executor() -> None:
    executor = _Executor()
    harness = _harness(
        provider=ScriptedProvider(
            [
                [
                    {
                        "type": "tool_call",
                        "id": "c1",
                        "name": "scale_resource",
                        "arguments": '{"kind": "deployments", "name": "api", "replicas": 5}',
                    },
                    {"type": "done"},
                ],
                [{"type": "text_delta", "text": "writes are not enabled"}, {"type": "done"}],
            ]
        ),
        executor=executor,
    )
    events = [event async for event in harness.session.run_turn("scale api to 5")]
    # Narrowed with `isinstance`, not a class-name string: the filter has to
    # tell the type checker which member of the `AgentEvent` union survived
    # it, or `.ok`/`.summary` below are unchecked attribute reads.
    finished = [event for event in events if isinstance(event, ToolCallFinished)]
    assert len(finished) == 1
    assert not finished[0].ok
    assert "not armed" in finished[0].summary
    assert executor.calls == []


async def test_a_read_flows_through_the_tool_harness_and_mints_evidence() -> None:
    executor = _Executor()
    harness = _harness(
        provider=ScriptedProvider(
            [
                [
                    {
                        "type": "tool_call",
                        "id": "c1",
                        "name": "diagnose_pod",
                        "arguments": '{"pod": "worker-1", "namespace": "jobs"}',
                    },
                    {"type": "done"},
                ],
                [{"type": "text_delta", "text": "OOMKilled [E1]"}, {"type": "done"}],
            ]
        ),
        executor=executor,
    )
    async for _event in harness.session.run_turn("why is worker-1 dying?"):
        pass
    assert executor.calls == [("diagnose_pod", {"pod": "worker-1", "namespace": "jobs"})]
    assert harness.session.evidence.references() == ("E1",)
