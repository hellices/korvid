"""The live agent session contract (issue #316, Task 11).

`DefaultAgentSession` is the only component that knows there is a *screen*:
it reads the workspace through `AgentUiBridge` at the start of every turn,
composes that snapshot through `PromptHarness`, hands one frozen
`AgentTurnRequest` to an `AgentEngine`, and owns the iterator it starts
until the turn ends. Everything else — durable history, provider requests,
tool routing — belongs to the collaborators it was constructed with.

The behaviour pinned here is the behaviour a live TUI depends on:

- every turn sees the workspace as it is *now*, and direct navigation
  between turns reaches the model as fresh typed state, never as a
  synthetic user message inserted into the transcript;
- a Kubernetes context switch produces exactly one handoff note, naming
  the context that was left and the one now in force, and only once;
- retargeting is atomic: an unusable policy is refused before the live
  policy, tool surface, or cluster facts move;
- an interrupted or closed turn leaves state a later turn can start from,
  and a write awaiting approval when the session closes is neither
  approved nor replayed.

The session is driven through real collaborators (a real
`ConversationState`, a real `RequestGateway` over a scripted provider, a
real `ToolHarness`) so every assertion is made on observable behaviour:
the payloads the provider received, retained history, emitted events.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import pytest

from korvid.agent.conversation import INTERRUPT_MARKER, ConversationState
from korvid.agent.engine import AgentEngine, AgentTurnRequest
from korvid.agent.events import (
    AgentEvent,
    TextDelta,
    ToolCallFinished,
    TurnComplete,
    TurnInterrupted,
)
from korvid.agent.interaction import ClusterFacts, InteractionContext, PaneContext, ResourceIdentity
from korvid.agent.model_policy import ModelDescriptor, ModelTier, ResolvedAgentPolicy
from korvid.agent.prompt_harness import PromptHarness, UnknownPromptPackError
from korvid.agent.request_gateway import RequestGateway
from korvid.agent.session import AgentSession, DefaultAgentSession, SessionRetargetError
from korvid.agent.tool_harness import ToolHarness
from korvid.tools.registry import TOOLS_BY_NAME

from .engine_fakes import (
    DONE,
    USER_TEXT,
    RecordingBridge,
    RecordingExecution,
    ScriptedProvider,
    build_harness,
    make_policy,
    system_message,
    text_delta,
    text_turn,
    tool_turn,
    usage,
)

UNKNOWN_CLUSTER = ClusterFacts(provider="unknown", distribution=None)
AZURE_CLUSTER = ClusterFacts(provider="azure", distribution="aks")

SCALE_ARGUMENTS = '{"kind":"Deployment","name":"api","namespace":"prod","replicas":3}'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def workspace(
    *,
    kube_context: str | None = "kind-dev",
    epoch: int = 1,
    kind: str = "pods",
    scope: str = "default",
    filter_pattern: str | None = None,
    selected: ResourceIdentity | None = None,
) -> InteractionContext:
    """One human-visible workspace snapshot."""
    return InteractionContext(
        kube_context=kube_context,
        context_epoch=epoch,
        focused_pane=PaneContext(
            kind=kind, scope=scope, filter_pattern=filter_pattern, selected=selected
        ),
        secondary_pane=None,
        timeline_cursor=None,
    )


class SessionBridge(RecordingBridge):
    """A bridge whose snapshot the test moves, counting every read."""

    def __init__(self, context: InteractionContext | None = None) -> None:
        super().__init__()
        self.context = context if context is not None else workspace()
        self.snapshots = 0

    def snapshot(self) -> InteractionContext:
        self.snapshots += 1
        return self.context


class SpyEngine(AgentEngine):
    """Delegates to a real engine while counting the session's calls."""

    def __init__(self, inner: AgentEngine) -> None:
        self.inner = inner
        self.requests: list[AgentTurnRequest] = []
        self.interrupts = 0
        self.closes = 0

    def run(self, request: AgentTurnRequest) -> AsyncIterator[AgentEvent]:
        self.requests.append(request)
        return self.inner.run(request)

    def interrupt(self) -> None:
        self.interrupts += 1
        self.inner.interrupt()

    async def aclose(self) -> None:
        self.closes += 1
        await self.inner.aclose()


def session_policy(
    *,
    tool_names: Sequence[str] = ("get_logs",),
    tier: ModelTier = ModelTier.HIGH,
    max_history_chars: int = 24_000,
    max_tool_calls: int | None = None,
    model: str = "qwen3:8b",
) -> ResolvedAgentPolicy:
    """A resolved policy naming a *shipped* prompt pack, as the router does."""
    pack = "low-korvid-operator" if tier is ModelTier.LOW else "high-korvid-operator"
    base = make_policy(
        tool_names=tool_names,
        tier=tier,
        max_history_chars=max_history_chars,
        max_tool_calls=max_tool_calls,
    )
    return replace(base, prompt_pack_id=pack, model=ModelDescriptor("test", model))


def unregistered_policy() -> ResolvedAgentPolicy:
    """A policy arming a tool name the registry does not define."""
    schema = copy.deepcopy(TOOLS_BY_NAME["get_logs"].schema)
    schema["function"]["name"] = "not_a_registered_tool"
    return replace(session_policy(), tools=(schema,))


@dataclass
class SessionHarness:
    """One session and the collaborators a test inspects."""

    session: DefaultAgentSession
    engine: SpyEngine
    conversation: ConversationState
    gateway: RequestGateway
    tools: ToolHarness
    provider: ScriptedProvider
    execution: RecordingExecution
    bridge: SessionBridge
    prompts: PromptHarness
    policy: ResolvedAgentPolicy

    async def run(self, user_text: str = USER_TEXT) -> list[AgentEvent]:
        """Drive one whole turn and collect its events."""
        return [event async for event in self.session.run_turn(user_text)]

    def rebuild(
        self,
        policy: ResolvedAgentPolicy,
        *,
        cluster: ClusterFacts = UNKNOWN_CLUSTER,
        user_rules: tuple[str, ...] = (),
    ) -> DefaultAgentSession:
        """A second session over the same collaborators (constructor checks)."""
        return DefaultAgentSession(
            engine=self.engine,
            bridge=self.bridge,
            prompt_harness=self.prompts,
            conversation=self.conversation,
            gateway=self.gateway,
            tools=self.tools,
            policy=policy,
            cluster=cluster,
            user_rules=user_rules,
        )


def build_session(
    turns: Sequence[Sequence[Any]] = (),
    *,
    policy: ResolvedAgentPolicy | None = None,
    provider: ScriptedProvider | None = None,
    execution: RecordingExecution | None = None,
    bridge: SessionBridge | None = None,
    cluster: ClusterFacts = UNKNOWN_CLUSTER,
    user_rules: tuple[str, ...] = (),
) -> SessionHarness:
    """Wire a live session over a native engine and scripted edges."""
    resolved = policy if policy is not None else session_policy()
    ui = bridge if bridge is not None else SessionBridge()
    executor = execution if execution is not None else RecordingExecution()
    scripted = provider if provider is not None else ScriptedProvider(turns)
    inner = build_harness(policy=resolved, provider=scripted, execution=executor, bridge=ui)
    spy = SpyEngine(inner.engine)
    prompts = PromptHarness()
    session = DefaultAgentSession(
        engine=spy,
        bridge=ui,
        prompt_harness=prompts,
        conversation=inner.conversation,
        gateway=inner.gateway,
        tools=inner.tools,
        policy=resolved,
        cluster=cluster,
        user_rules=user_rules,
    )
    return SessionHarness(
        session=session,
        engine=spy,
        conversation=inner.conversation,
        gateway=inner.gateway,
        tools=inner.tools,
        provider=scripted,
        execution=executor,
        bridge=ui,
        prompts=prompts,
        policy=resolved,
    )


# -- payload readers ---------------------------------------------------------


def user_messages(call: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every user message of one recorded request, in order."""
    return [str(message["content"]) for message in call if message.get("role") == "user"]


def latest_user_message(call: Sequence[Mapping[str, Any]]) -> str:
    """The user message this request was made for."""
    return user_messages(call)[-1]


def tool_surface(surface: Sequence[Mapping[str, Any]]) -> list[str]:
    """The tool names one recorded request offered."""
    return [str(schema["function"]["name"]) for schema in surface]


# ---------------------------------------------------------------------------
# A fresh workspace snapshot per turn
# ---------------------------------------------------------------------------


async def test_each_turn_composes_the_workspace_as_it_is_now() -> None:
    bridge = SessionBridge(workspace(selected=ResourceIdentity("Pod", "default", "api-1", "uid-1")))
    harness = build_session([text_turn("first"), text_turn("second")], bridge=bridge)

    await harness.run("what is wrong?")
    bridge.context = workspace(
        kind="deployments",
        scope="prod",
        selected=ResourceIdentity("Deployment", "prod", "api", "uid-9"),
    )
    await harness.run("and now?")

    first = latest_user_message(harness.provider.calls[0])
    second = latest_user_message(harness.provider.calls[1])
    assert "api-1" in first
    assert "Deployment" not in first
    assert "Deployment" in second
    assert "api-1" not in second
    assert bridge.snapshots == 2


async def test_a_filter_typed_between_turns_reaches_the_next_turn() -> None:
    bridge = SessionBridge()
    harness = build_session([text_turn("first"), text_turn("second")], bridge=bridge)

    await harness.run("what is wrong?")
    bridge.context = workspace(filter_pattern="crashloop")
    await harness.run("and now?")

    assert "crashloop" not in latest_user_message(harness.provider.calls[0])
    assert "crashloop" in latest_user_message(harness.provider.calls[1])


async def test_direct_navigation_adds_no_synthetic_transcript_entry() -> None:
    """The workspace is state, not something the user said.

    A synthetic "the user navigated to ..." message would be a durable
    turn the operator never wrote, and it would still be there — stale —
    long after the screen moved on.
    """
    bridge = SessionBridge()
    harness = build_session([text_turn("first"), text_turn("second")], bridge=bridge)

    await harness.run("what is wrong?")
    first_turn_prompt = latest_user_message(harness.provider.calls[0])
    bridge.context = workspace(kind="deployments", scope="prod", epoch=1)
    await harness.run("and now?")

    assert [str(message["role"]) for message in harness.conversation.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    second_request_users = user_messages(harness.provider.calls[1])
    assert len(second_request_users) == 2
    assert second_request_users[0] == first_turn_prompt


async def test_the_session_writes_no_model_facing_prose_into_history() -> None:
    bridge = SessionBridge()
    harness = build_session([text_turn("first"), text_turn("second")], bridge=bridge)

    await harness.run("what is wrong?")
    bridge.context = workspace(kube_context="prod-east", epoch=2)
    await harness.run("and now?")

    stored = " ".join(
        str(message.get("content") or "") for message in harness.conversation.messages
    )
    assert "switched" not in stored
    assert not any(message.get("role") == "system" for message in harness.conversation.messages)


# ---------------------------------------------------------------------------
# Context handoff
# ---------------------------------------------------------------------------


async def test_a_context_switch_names_the_old_and_new_context_once() -> None:
    bridge = SessionBridge()
    harness = build_session([text_turn("first"), text_turn("second")], bridge=bridge)

    await harness.run("what is wrong?")
    bridge.context = workspace(kube_context="prod-east", epoch=2)
    await harness.run("and now?")

    first = system_message(harness.provider.calls[0])
    second = system_message(harness.provider.calls[1])
    assert "kind-dev" not in first
    assert second.count("kind-dev") == 1
    assert second.count("prod-east") == 1
    assert second.count("context epoch 1") == 1
    assert second.count("context epoch 2") == 1


async def test_the_handoff_note_is_not_repeated_on_the_following_turn() -> None:
    bridge = SessionBridge()
    harness = build_session(
        [text_turn("first"), text_turn("second"), text_turn("third")], bridge=bridge
    )

    await harness.run("what is wrong?")
    bridge.context = workspace(kube_context="prod-east", epoch=2)
    await harness.run("and now?")
    await harness.run("still?")

    assert "kind-dev" in system_message(harness.provider.calls[1])
    assert "kind-dev" not in system_message(harness.provider.calls[2])


async def test_an_undriven_turn_does_not_consume_the_handoff() -> None:
    """The last *started* turn is the one a handoff is measured against."""
    bridge = SessionBridge()
    harness = build_session([text_turn("first"), text_turn("second")], bridge=bridge)

    await harness.run("what is wrong?")
    bridge.context = workspace(kube_context="prod-east", epoch=2)
    abandoned = harness.session.run_turn("never driven")
    await harness.run("and now?")

    assert abandoned is not None
    assert "kind-dev" in system_message(harness.provider.calls[1])


async def test_a_turn_without_a_predecessor_carries_no_handoff_note() -> None:
    harness = build_session([text_turn("first")])

    await harness.run("what is wrong?")

    assert "switched" not in system_message(harness.provider.calls[0])


# ---------------------------------------------------------------------------
# Retarget
# ---------------------------------------------------------------------------


async def test_retarget_installs_the_new_policy_and_cluster_together() -> None:
    bridge = SessionBridge()
    harness = build_session([text_turn("first"), text_turn("second")], bridge=bridge)
    await harness.run("what is wrong?")
    retargeted = session_policy(tool_names=("get_logs", "get_events"))

    bridge.context = workspace(kube_context="prod-east", epoch=2)
    harness.session.retarget(retargeted, AZURE_CLUSTER)
    await harness.run("and now?")

    assert harness.session.policy is retargeted
    assert harness.engine.requests[-1].policy is retargeted
    system = system_message(harness.provider.calls[1])
    assert "AKS" in system
    assert tool_surface(harness.provider.tool_surfaces[1]) == ["get_logs", "get_events"]


async def test_retarget_clears_evidence_before_the_next_request() -> None:
    """A citation minted against the old cluster must not resolve after a switch."""
    harness = build_session(
        [tool_turn("get_logs", '{"pod":"api-0","namespace":"prod"}'), text_turn("healthy [E1]")]
    )
    await harness.run("what is wrong?")
    assert harness.session.evidence.references() == ("E1",)

    harness.session.retarget(session_policy(), AZURE_CLUSTER)

    assert harness.session.evidence.references() == ()
    assert harness.session.evidence.resolve("E1") is None


async def test_retarget_does_not_consume_the_pending_context_handoff() -> None:
    bridge = SessionBridge()
    harness = build_session([text_turn("first"), text_turn("second")], bridge=bridge)
    await harness.run("what is wrong?")

    bridge.context = workspace(kube_context="prod-east", epoch=2)
    harness.session.retarget(session_policy(), AZURE_CLUSTER)
    await harness.run("and now?")

    second = system_message(harness.provider.calls[1])
    assert "kind-dev" in second
    assert "prod-east" in second


async def test_retarget_preserves_completed_history() -> None:
    harness = build_session([text_turn("first"), text_turn("second")])
    await harness.run("what is wrong?")
    before = copy.deepcopy(harness.conversation.messages)

    harness.session.retarget(session_policy(tool_names=("get_events",)), AZURE_CLUSTER)

    assert harness.conversation.messages == before
    assert harness.session.total_tokens == harness.conversation.total_tokens


async def test_retarget_does_not_mutate_the_previous_frozen_policy() -> None:
    harness = build_session([text_turn("first")])
    previous = harness.session.policy
    snapshot = repr(previous)

    harness.session.retarget(session_policy(tool_names=("get_events",)), AZURE_CLUSTER)

    assert repr(previous) == snapshot
    assert harness.session.policy is not previous


async def test_a_retargeted_surface_is_visible_to_the_engine_already_wired() -> None:
    """The engine keeps the same `ToolHarness`, so a swap must be seen at once."""
    harness = build_session(
        [tool_turn("get_events", '{"namespace":"prod"}'), text_turn("nothing recent")],
        policy=session_policy(tool_names=("get_logs",)),
    )

    harness.session.retarget(session_policy(tool_names=("get_events",)), UNKNOWN_CLUSTER)
    events = await harness.run("what happened?")

    assert harness.execution.names == ["get_events"]
    assert isinstance(events[-1], TurnComplete)


async def test_retarget_refuses_an_unknown_prompt_pack_without_moving_anything() -> None:
    harness = build_session([text_turn("first")])
    previous = harness.session.policy
    unusable = replace(
        session_policy(tool_names=("get_logs", "get_events")),
        prompt_pack_id="not-a-shipped-pack",
    )

    with pytest.raises(UnknownPromptPackError, match="not-a-shipped-pack"):
        harness.session.retarget(unusable, AZURE_CLUSTER)
    await harness.run("what is wrong?")

    assert harness.session.policy is previous
    assert "AKS" not in system_message(harness.provider.calls[0])
    assert tool_surface(harness.provider.tool_surfaces[0]) == ["get_logs"]


async def test_retarget_refuses_an_unregistered_armed_tool_without_moving_anything() -> None:
    harness = build_session([text_turn("first")])
    previous = harness.session.policy

    with pytest.raises(ValueError, match="not_a_registered_tool"):
        harness.session.retarget(unregistered_policy(), AZURE_CLUSTER)
    await harness.run("what is wrong?")

    assert harness.session.policy is previous
    assert tool_surface(harness.provider.tool_surfaces[0]) == ["get_logs"]
    assert "AKS" not in system_message(harness.provider.calls[0])


async def test_retarget_refuses_a_different_model_descriptor() -> None:
    """A model change is a rebuild: the provider and gateway are not ours to swap."""
    harness = build_session([text_turn("first")])
    previous = harness.session.policy

    with pytest.raises(SessionRetargetError, match="rebuild"):
        harness.session.retarget(session_policy(model="llama3.1:70b"), AZURE_CLUSTER)

    assert harness.session.policy is previous


async def test_retarget_refuses_a_different_history_budget() -> None:
    harness = build_session([text_turn("first")])
    previous = harness.session.policy

    with pytest.raises(SessionRetargetError, match="rebuild"):
        harness.session.retarget(session_policy(max_history_chars=120_000), AZURE_CLUSTER)

    assert harness.session.policy is previous


async def test_retarget_refuses_a_different_history_budget_mode() -> None:
    harness = build_session([text_turn("first")])
    previous = harness.session.policy
    stricter = replace(session_policy(), strict_history_budget=True)

    with pytest.raises(SessionRetargetError, match="rebuild"):
        harness.session.retarget(stricter, AZURE_CLUSTER)

    assert harness.session.policy is previous


async def test_retarget_accepts_environment_derived_cap_changes() -> None:
    harness = build_session([text_turn("first")])
    retargeted = session_policy(tool_names=("get_logs", "get_events"), max_tool_calls=1)

    harness.session.retarget(retargeted, AZURE_CLUSTER)

    assert harness.session.policy is retargeted


async def test_retarget_is_rejected_while_a_turn_is_running() -> None:
    stall = asyncio.Event()
    provider = ScriptedProvider([[text_delta("thinking"), stall]])
    harness = build_session(provider=provider)

    task = asyncio.create_task(harness.run("what is wrong?"))
    await asyncio.wait_for(provider.stalled.wait(), timeout=5)
    with pytest.raises(RuntimeError, match="running"):
        harness.session.retarget(session_policy(tool_names=("get_events",)), AZURE_CLUSTER)
    await harness.session.aclose()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert harness.session.policy.tools == harness.policy.tools


async def test_retarget_is_rejected_after_the_session_is_closed() -> None:
    harness = build_session([text_turn("first")])

    await harness.session.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        harness.session.retarget(session_policy(tool_names=("get_events",)), AZURE_CLUSTER)


async def test_retarget_is_rejected_before_an_interrupted_turn_is_finalized() -> None:
    stall = asyncio.Event()
    provider = ScriptedProvider([[text_delta("thinking"), stall]])
    harness = build_session(provider=provider)

    task = asyncio.create_task(harness.run("what is wrong?"))
    await asyncio.wait_for(provider.stalled.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    with pytest.raises(RuntimeError, match="finaliz"):
        harness.session.retarget(session_policy(tool_names=("get_events",)), AZURE_CLUSTER)


# ---------------------------------------------------------------------------
# Overlap and undriven iterators
# ---------------------------------------------------------------------------


async def test_a_second_turn_is_rejected_while_one_is_running() -> None:
    stall = asyncio.Event()
    provider = ScriptedProvider([[text_delta("thinking"), stall], text_turn("second")])
    harness = build_session(provider=provider)

    task = asyncio.create_task(harness.run("first"))
    await asyncio.wait_for(provider.stalled.wait(), timeout=5)
    with pytest.raises(RuntimeError, match="already running"):
        harness.session.run_turn("second")
    stall.set()
    events = await task

    assert isinstance(events[-1], TurnComplete)


async def test_an_iterator_that_is_never_driven_does_not_wedge_the_session() -> None:
    """A turn that never started claims nothing — no engine, no snapshot."""
    bridge = SessionBridge()
    harness = build_session([text_turn("answer")], bridge=bridge)

    abandoned = harness.session.run_turn("never driven")
    events = await harness.run("driven")

    assert abandoned is not None
    assert isinstance(events[-1], TurnComplete)
    assert len(harness.provider.calls) == 1
    assert bridge.snapshots == 1


async def test_a_finished_turn_releases_the_session_for_the_next_one() -> None:
    harness = build_session([text_turn("first"), text_turn("second")])

    first = await harness.run("first")
    second = await harness.run("second")

    assert isinstance(first[-1], TurnComplete)
    assert isinstance(second[-1], TurnComplete)
    assert len(harness.provider.calls) == 2


# ---------------------------------------------------------------------------
# Interruption
# ---------------------------------------------------------------------------


async def test_interrupt_before_the_turn_starts_is_inert() -> None:
    """The window between `run_turn` and the first event belongs to no turn."""
    harness = build_session([text_turn("the pod is healthy")])

    stream = harness.session.run_turn("what is wrong?")
    harness.session.interrupt()
    events = [event async for event in stream]

    assert harness.engine.interrupts == 0
    assert events[0] == TextDelta(text="the pod is healthy")
    assert isinstance(events[-1], TurnComplete)


async def test_interrupt_after_the_turn_starts_signals_the_engine() -> None:
    harness = build_session([tool_turn(), text_turn()])

    events: list[AgentEvent] = []
    async for event in harness.session.run_turn("what is wrong?"):
        events.append(event)
        if isinstance(event, ToolCallFinished):
            harness.session.interrupt()

    assert harness.engine.interrupts == 1
    assert not any(isinstance(event, TurnComplete) for event in events)
    assert len(harness.provider.calls) == 1


async def test_an_advisory_interrupt_leaves_the_turn_awaiting_finalization() -> None:
    harness = build_session([tool_turn(), text_turn()])

    async for event in harness.session.run_turn("what is wrong?"):
        if isinstance(event, ToolCallFinished):
            harness.session.interrupt()
    interrupted = harness.session.finalize_interrupt()

    assert isinstance(interrupted, TurnInterrupted)
    assert harness.conversation.turn_active is False
    assert not harness.conversation.has_unmatched_tool_calls


async def test_a_cancelled_turn_is_finalized_synchronously_and_the_next_turn_starts() -> None:
    stall = asyncio.Event()
    provider = ScriptedProvider([[text_delta("thinking about"), stall], text_turn("second answer")])
    harness = build_session(provider=provider)

    task = asyncio.create_task(harness.run("first"))
    await asyncio.wait_for(provider.stalled.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    interrupted = harness.session.finalize_interrupt()
    events = await harness.run("second")

    assert isinstance(interrupted, TurnInterrupted)
    note = str(harness.conversation.messages[1]["content"])
    assert note.startswith("thinking about")
    assert note.endswith(INTERRUPT_MARKER)
    assert isinstance(events[-1], TurnComplete)


async def test_a_turn_cannot_start_before_the_interrupted_one_is_finalized() -> None:
    stall = asyncio.Event()
    provider = ScriptedProvider([[text_delta("thinking"), stall], text_turn("second")])
    harness = build_session(provider=provider)

    task = asyncio.create_task(harness.run("first"))
    await asyncio.wait_for(provider.stalled.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    with pytest.raises(RuntimeError, match="finaliz"):
        harness.session.run_turn("second")


async def test_finalize_interrupt_is_one_shot() -> None:
    harness = build_session([tool_turn(), text_turn()])

    async for event in harness.session.run_turn("what is wrong?"):
        if isinstance(event, ToolCallFinished):
            harness.session.interrupt()
    harness.session.finalize_interrupt()

    with pytest.raises(RuntimeError, match="no interrupted turn"):
        harness.session.finalize_interrupt()


async def test_finalize_interrupt_rejects_a_completed_turn() -> None:
    harness = build_session([text_turn("the pod is healthy")])

    await harness.run("what is wrong?")

    with pytest.raises(RuntimeError, match="no interrupted turn"):
        harness.session.finalize_interrupt()


async def test_finalize_interrupt_rejects_a_running_turn() -> None:
    stall = asyncio.Event()
    provider = ScriptedProvider([[text_delta("thinking"), stall]])
    harness = build_session(provider=provider)

    task = asyncio.create_task(harness.run("first"))
    await asyncio.wait_for(provider.stalled.wait(), timeout=5)
    with pytest.raises(RuntimeError, match="running"):
        harness.session.finalize_interrupt()
    await harness.session.aclose()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert harness.conversation.turn_active is False


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


async def test_aclose_is_idempotent_and_closes_the_engine_once() -> None:
    harness = build_session([text_turn("the pod is healthy")])

    await harness.run("what is wrong?")
    await harness.session.aclose()
    await harness.session.aclose()

    assert harness.engine.closes == 1


async def test_a_closed_session_refuses_another_turn() -> None:
    harness = build_session([text_turn("first"), text_turn("second")])

    await harness.run("first")
    await harness.session.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        harness.session.run_turn("second")


async def test_aclose_waits_for_the_driving_task_and_finalizes_the_turn() -> None:
    stall = asyncio.Event()
    provider = ScriptedProvider([[text_delta("thinking about"), stall]])
    harness = build_session(provider=provider)

    task = asyncio.create_task(harness.run("what is wrong?"))
    await asyncio.wait_for(provider.stalled.wait(), timeout=5)
    await harness.session.aclose()

    assert task.done()
    assert harness.conversation.turn_active is False
    assert str(harness.conversation.messages[-1]["content"]).endswith(INTERRUPT_MARKER)
    assert provider.closed == 1
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_aclose_closes_a_turn_no_task_is_driving() -> None:
    harness = build_session([[text_delta("one"), text_delta("two"), DONE]])

    stream = harness.session.run_turn("what is wrong?")
    first = await anext(stream)
    await harness.session.aclose()

    assert first == TextDelta(text="one")
    assert harness.provider.closed == 1
    assert harness.conversation.turn_active is False
    assert str(harness.conversation.messages[-1]["content"]).endswith(INTERRUPT_MARKER)


async def test_aclose_leaves_nothing_for_the_caller_to_finalize() -> None:
    stall = asyncio.Event()
    provider = ScriptedProvider([[text_delta("thinking"), stall]])
    harness = build_session(provider=provider)

    task = asyncio.create_task(harness.run("what is wrong?"))
    await asyncio.wait_for(provider.stalled.wait(), timeout=5)
    await harness.session.aclose()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    with pytest.raises(RuntimeError, match="no interrupted turn"):
        harness.session.finalize_interrupt()


async def test_no_event_and_no_history_arrive_after_aclose_returns() -> None:
    stall = asyncio.Event()
    provider = ScriptedProvider([[text_delta("thinking"), stall, text_delta("more")]])
    harness = build_session(provider=provider)
    events: list[AgentEvent] = []

    async def drive() -> None:
        async for event in harness.session.run_turn("what is wrong?"):
            events.append(event)

    task = asyncio.create_task(drive())
    await asyncio.wait_for(provider.stalled.wait(), timeout=5)
    await harness.session.aclose()
    seen, history = list(events), copy.deepcopy(harness.conversation.messages)
    stall.set()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert events == seen
    assert harness.conversation.messages == history


async def test_closing_during_a_pending_write_neither_approves_nor_replays_it() -> None:
    """A close while the approval dialog is open must not become an approval."""
    execution = RecordingExecution()
    gate = asyncio.Event()
    execution.gate = gate
    provider = ScriptedProvider([tool_turn("scale_resource", SCALE_ARGUMENTS), text_turn("scaled")])
    harness = build_session(
        provider=provider,
        execution=execution,
        policy=session_policy(tool_names=("get_logs", "scale_resource")),
    )

    task = asyncio.create_task(harness.run("scale the api deployment"))
    await asyncio.wait_for(execution.entered.wait(), timeout=5)
    await harness.session.aclose()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert execution.names == ["scale_resource"]
    assert not gate.is_set()
    assert len(provider.calls) == 1
    assert not any(message.get("role") == "tool" for message in harness.conversation.messages)
    assert not harness.conversation.has_unmatched_tool_calls
    assert str(harness.conversation.messages[-1]["content"]).endswith(INTERRUPT_MARKER)


# ---------------------------------------------------------------------------
# Construction and eager validation
# ---------------------------------------------------------------------------


def test_the_constructor_does_not_snapshot_the_bridge() -> None:
    """Task 12 wires a bridge proxy that has no app to read yet."""
    harness = build_session([text_turn("first")])

    assert harness.bridge.snapshots == 0


def test_the_constructor_refuses_an_unknown_prompt_pack() -> None:
    harness = build_session([text_turn("first")])
    unusable = replace(session_policy(), prompt_pack_id="not-a-shipped-pack")

    with pytest.raises(UnknownPromptPackError, match="not-a-shipped-pack"):
        harness.rebuild(unusable)


def test_the_constructor_refuses_a_policy_arming_an_unregistered_tool() -> None:
    harness = build_session([text_turn("first")])

    with pytest.raises(ValueError, match="not_a_registered_tool"):
        harness.rebuild(unregistered_policy())


def test_the_constructor_refuses_user_rules_over_the_static_budget() -> None:
    harness = build_session([text_turn("first")])
    rules = tuple("r" * 900 for _ in range(6))

    with pytest.raises(ValueError, match="history budget"):
        harness.rebuild(session_policy(), user_rules=rules)


async def test_configured_user_rules_reach_every_composed_turn() -> None:
    harness = build_session(
        [text_turn("first")], user_rules=("Never touch namespace kube-system.",)
    )

    await harness.run("what is wrong?")

    assert "Never touch namespace kube-system." in system_message(harness.provider.calls[0])


# ---------------------------------------------------------------------------
# Read-only properties
# ---------------------------------------------------------------------------


async def test_properties_read_through_to_the_collaborators() -> None:
    harness = build_session([[text_delta("the pod is healthy"), usage(11, 7), DONE]])

    assert harness.session.total_tokens == (0, 0)
    assert harness.session.latest_outbound_payload is None
    await harness.run("what is wrong?")

    assert harness.session.total_tokens == (11, 7)
    assert harness.session.usage_estimated is harness.conversation.usage_estimated
    assert harness.session.latest_outbound_payload is harness.gateway.latest_outbound_payload
    assert harness.session.latest_outbound_payload is not None
    assert harness.session.evidence is harness.tools.evidence
    assert harness.session.policy is harness.policy


def test_the_session_abc_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError, match="abstract"):
        AgentSession()  # type: ignore[abstract]  # the point of the test


def test_the_default_session_implements_the_abc() -> None:
    harness = build_session([text_turn("first")])

    assert isinstance(harness.session, AgentSession)
