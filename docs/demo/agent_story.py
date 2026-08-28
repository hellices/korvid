"""Deterministic `LLMProvider` for the documentation Agent capture.

Not shipped with the package — a development harness driven by
`docs/demo/agent.tape` (VHS). See `docs/demo/README.md`.

The capture must show korvid's own agent loop, so nothing here fabricates
panel events: the provider only decides *what the model would say*, and the
shipped interaction harness does the rest. `build_demo_agent_session`
composes exactly the graph `korvid.__main__._build_session` composes —

    ToolExecutor -> ToolHarness(policy, execution, bridge, EvidenceLedger)
    ConversationState -> RequestGateway -> NativeAgentEngine
    -> DefaultAgentSession

— so the tool calls are dispatched through the real `ToolExecutor`, the
results come back as `role="tool"` messages, the `[E1]`/`[E2]` references
are minted in the real `EvidenceLedger`, and the answer's citations are
validated against them.

Deterministic, and offline by construction: `complete` never opens a socket
and never reads a credential, so the recording needs no provider account, no
network and no cluster. The pauses are pacing for the camera — a turn that
resolves instantly reads as a canned animation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

from korvid.agent.conversation import ConversationState
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
from korvid.agent.model_catalog import MODEL_CATALOG
from korvid.agent.model_policy import (
    ModelCapabilities,
    ModelDescriptor,
    ModelRouter,
    PolicyEnvironment,
    ResolvedAgentPolicy,
)
from korvid.agent.native_engine import NativeAgentEngine
from korvid.agent.prompt_harness import PromptHarness
from korvid.agent.provider import LLMProvider
from korvid.agent.request_gateway import RequestGateway
from korvid.agent.session import DefaultAgentSession
from korvid.agent.tool_harness import ToolHarness
from korvid.k8s.csp import UNKNOWN_PROVIDER
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.reads import ReadOps
from korvid.tools.executor import ToolExecutor

#: The pod the whole story is about; it is the synthetic fixture's
#: CrashLoopBackOff pod, so both reads return real fixture content.
DEMO_POD = "payment-worker-6c9f7d-b3xnq"
DEMO_NAMESPACE = "shop"

#: How the capture identifies itself to the router and to the status bar.
#: The provider id is not a real backend and the model tag is not a real
#: model, so the shipped catalog matches neither and contributes no
#: capability fact — the routing this session runs under is decided by
#: `DemoAgentProvider.capabilities` alone.
DEMO_MODEL = ModelDescriptor("deterministic-demo", "korvid-demo")

#: The environment the capture's policy is resolved against. Read-only, so
#: `agent_tool_schemas` never puts a write schema on the surface: the
#: recording is unattended and only a user keystroke may ever approve a
#: write, so the surface simply never carries one. No pod resize and no
#: observability backend, because the synthetic fixture has neither.
DEMO_ENVIRONMENT = PolicyEnvironment(
    readonly=True,
    resize_supported=False,
    observability_backends=frozenset(),
)

#: The cluster the prompt describes. Unknown on purpose: the fixture runs on
#: no cloud, and claiming one would put a provider-specific note in front of
#: the recorded answer.
DEMO_CLUSTER = ClusterFacts(provider=UNKNOWN_PROVIDER, distribution=None)

#: The workspace a headless run composes its turn against — the same screen
#: `docs/demo/agent.tape` records: the `shop` pod table with the
#: CrashLoopBackOff pod selected. The recording itself never uses it; there
#: `docs/demo/demo.py` hands over the live `AgentWorkspaceBridge`.
DEMO_INTERACTION = InteractionContext(
    kube_context="current",
    context_epoch=0,
    focused_pane=PaneContext(
        kind="pods",
        scope=DEMO_NAMESPACE,
        filter_pattern=None,
        selected=ResourceIdentity(
            kind="Pod",
            namespace=DEMO_NAMESPACE,
            name=DEMO_POD,
            uid=None,
        ),
    ),
    secondary_pane=None,
    timeline_cursor=None,
)

#: The answer, split where the camera should see it arrive. Both markers are
#: claims about reads this turn performs — the session rejects any other
#: reference, so an edit that cites evidence the turn never gathered fails
#: `test_demo_agent_turn_uses_real_tools_and_mints_citations` instead of
#: quietly publishing an unsupported citation.
ANSWER_CHUNKS: tuple[str, ...] = (
    "The payment worker is repeatedly restarting after gateway failures. [E1] ",
    "Its recent logs show repeated gateway 503 responses; inspect the owner ",
    "and upstream availability before changing the workload. [E2]",
)


class RecordedScreenBridge(AgentUiBridge):
    """The workspace port for a run that has no workspace.

    `DefaultAgentSession` snapshots the screen at the start of every turn,
    so the contracts — which drive the turn headless, with no `KorvidApp`
    behind it — still need an `AgentUiBridge`. This one answers with the
    screen the tape records and refuses to apply anything: a UI action
    reported as applied when no UI exists would tell the model something
    happened that did not. The recording never reaches this class.
    """

    def __init__(self, interaction: InteractionContext = DEMO_INTERACTION) -> None:
        self._interaction = interaction

    def snapshot(self) -> InteractionContext:
        return self._interaction

    async def apply(self, action: UiAction) -> UiActionResult:
        return UiActionResult(
            ok=False,
            message=f"{type(action).__name__} needs a live workspace; this run has none",
            context=self._interaction,
        )


class DemoAgentProvider(LLMProvider):
    """Answers each iteration of one recorded turn with a fixed decision.

    Iteration 1 diagnoses the pod, iteration 2 reads its logs, iteration 3
    writes the answer. The messages it is handed are recorded rather than
    ignored: `seen_messages` is what proves the tool results really came
    back through the session, and is asserted on in the documentation
    contracts.
    """

    def __init__(self) -> None:
        self._iteration = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    @property
    def descriptor(self) -> ModelDescriptor:
        return DEMO_MODEL

    @property
    def capabilities(self) -> ModelCapabilities:
        """Only what this adapter can directly prove about itself.

        It emits `tool_call` events, so tool support is a fact. It has no
        context window, no reasoning and no parallel-call behaviour to
        report, so those stay unknown and the router does what it does for
        any model it has no evidence about: the conservative low tier.
        """
        return ModelCapabilities(supports_tools=True)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        del tools, stream
        self.seen_messages.append([dict(message) for message in messages])
        self._iteration += 1
        await asyncio.sleep(0.8)
        if self._iteration == 1:
            yield {
                "type": "tool_call",
                "id": "demo-diagnose",
                "name": "diagnose_pod",
                "arguments": f'{{"pod":"{DEMO_POD}","namespace":"{DEMO_NAMESPACE}"}}',
            }
        elif self._iteration == 2:
            yield {
                "type": "tool_call",
                "id": "demo-logs",
                "name": "get_logs",
                "arguments": (
                    f'{{"pod":"{DEMO_POD}","namespace":"{DEMO_NAMESPACE}"'
                    ',"container":"app","tail_lines":12}'
                ),
            }
        else:
            for chunk in ANSWER_CHUNKS:
                await asyncio.sleep(0.45)
                yield {"type": "text_delta", "text": chunk}


def resolve_demo_policy(provider: DemoAgentProvider) -> ResolvedAgentPolicy:
    """Route the deterministic provider through korvid's own model router.

    The production router resolves the production catalog, so the tool
    surface the capture arms, the prompt pack it composes and the budgets it
    runs under are all decided the way the TUI decides them.

    Args:
        provider: The provider whose descriptor and capabilities are
            routed. Never called — only read.

    Returns:
        The immutable policy the recorded turn is composed against.
    """
    return ModelRouter(MODEL_CATALOG).resolve(
        descriptor=provider.descriptor,
        provider_capabilities=provider.capabilities,
        explicit_tier=None,
        environment=DEMO_ENVIRONMENT,
    )


def build_demo_agent_session(
    reads: ReadOps,
    aliases: Mapping[str, ResourceMeta],
    *,
    bridge: AgentUiBridge | None = None,
    provider: DemoAgentProvider | None = None,
) -> DefaultAgentSession:
    """The shipped agent session, wired to the synthetic cluster.

    Composed in `korvid.__main__._build_session`'s order, from the same
    collaborators, so the capture runs the operator's own program: nothing
    is bypassed and nothing is substituted. What the harness decides are
    all *inputs* to that graph — the read-only environment, the synthetic
    `ReadOps` behind the executor, and the offline provider.

    Args:
        reads: the documentation fixture's `ReadOps` implementation.
        aliases: the kind aliases the executor resolves tool arguments with.
        bridge: the workspace port the session snapshots each turn and
            applies UI-drive tools through. `docs/demo/demo.py` passes the
            live app's `AgentWorkspaceBridge`; the contracts, which run
            headless, get `RecordedScreenBridge`.
        provider: the deterministic provider to drive the turn with.
            Defaults to a fresh one; the contracts pass their own so they
            can inspect the messages the session handed over.

    Returns:
        A real `DefaultAgentSession` over a real `ToolExecutor` — read-only,
        so no write tool is ever armed and no proposal tool exists.
    """
    provider = provider or DemoAgentProvider()
    bridge = bridge if bridge is not None else RecordedScreenBridge()
    policy = resolve_demo_policy(provider)
    tools = ToolHarness(
        policy=policy,
        execution=ToolExecutor(reads, aliases),
        bridge=bridge,
        evidence=EvidenceLedger(),
    )
    conversation = ConversationState(
        max_history_chars=policy.max_history_chars,
        strict_history_budget=policy.strict_history_budget,
    )
    gateway = RequestGateway(provider, RequestGateway.prepare_policy(policy))
    engine = NativeAgentEngine(conversation=conversation, gateway=gateway, tools=tools)
    return DefaultAgentSession(
        engine=engine,
        bridge=bridge,
        prompt_harness=PromptHarness(),
        conversation=conversation,
        gateway=gateway,
        tools=tools,
        policy=policy,
        cluster=DEMO_CLUSTER,
    )
