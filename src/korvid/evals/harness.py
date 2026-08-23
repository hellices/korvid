"""One eval composition helper, building production's own agent graph.

An eval that composed its own loop would measure a program the operator
never runs. So this module builds exactly what `korvid.__main__` builds in
`_build_session`, from parts the eval owns:

    ModelRouter(MODEL_CATALOG) -> ResolvedAgentPolicy
    ToolHarness(policy, RecordedExecution, AgentUiBridge, EvidenceLedger)
    ConversationState(policy budgets)
    RequestGateway(provider, RequestGateway.prepare_policy(policy))
    NativeAgentEngine(conversation, gateway, tools)
    DefaultAgentSession(engine, bridge, PromptHarness(), ...)

Nothing is bypassed and nothing is substituted: the same request gateway
sanitizes and snapshots the payload, the same tool harness decides what is
armed, and the same prompt harness composes the layers. The three things
an eval *does* decide are all inputs to that graph, never replacements for
part of it:

- **The environment is read-only.** `EVAL_ENVIRONMENT` sets
  `readonly=True`, so `agent_tool_schemas` never puts a write schema on
  the surface. A model that asks for one is answered by the tool harness
  with "not armed"; the executor and the approval path are never reached,
  which is what makes an unattended campaign safe to run against a fixture
  *and* against a guarded live namespace.
- **The tier may be named.** `--model-tier` becomes the router's
  `explicit_tier`, recorded as route source `user`; omitting it is normal
  automatic routing, exactly as in the TUI.
- **The tier pack and one overlay may be ground.** `PromptGrind` is the
  eval-only prompt lever (issue #316 task 13). It replaces layer 3 and
  adds a layer-5 overlay; it can never touch layer 1, because
  `PromptHarness` always composes the immutable safety contract first.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from korvid.agent.conversation import ConversationState
from korvid.agent.evidence import EvidenceLedger
from korvid.agent.interaction import AgentUiBridge, ClusterFacts, InteractionContext, PaneContext
from korvid.agent.model_catalog import MODEL_CATALOG
from korvid.agent.model_policy import (
    ModelCapabilities,
    ModelDescriptor,
    ModelRouter,
    ModelTier,
    PolicyEnvironment,
    ResolvedAgentPolicy,
)
from korvid.agent.native_engine import NativeAgentEngine
from korvid.agent.outbound import OutboundPolicy
from korvid.agent.prompt_harness import PromptHarness, PromptInputs
from korvid.agent.prompt_packs import MODEL_PROMPT_OVERLAYS, PROMPT_PACKS
from korvid.agent.request_gateway import RequestGateway
from korvid.agent.session import DefaultAgentSession
from korvid.agent.tool_harness import ToolHarness
from korvid.k8s.csp import UNKNOWN_PROVIDER
from korvid.tools.executor import RecordedExecution, as_recorded

#: The capability facts every eval is resolved against. Writes are
#: disabled: an eval runs unattended, and only a user keystroke may ever
#: approve a write, so the surface simply never carries one. Pod resize
#: and observability backends are absent for the same reason a fixture
#: cluster has no metrics server — they are not part of what is measured.
EVAL_ENVIRONMENT: Final[PolicyEnvironment] = PolicyEnvironment(
    readonly=True,
    resize_supported=False,
    observability_backends=frozenset(),
)

#: The cluster an eval runs against unless a pack says otherwise. Unknown
#: on purpose: a fixture cluster runs on no cloud, and claiming one would
#: put a provider-specific note in front of every scored answer.
EVAL_CLUSTER: Final[ClusterFacts] = ClusterFacts(provider=UNKNOWN_PROVIDER, distribution=None)

#: The overlay id a ground overlay is published under. Named rather than
#: anonymous so `meta.prompts.overlays` says what was layered.
EVAL_OVERLAY_ID: Final[str] = "eval-overlay"

#: A fixed workspace used only to render the static prompt layers for the
#: report's fingerprint. It never runs a turn; it exists so the digest is
#: a property of the policy and the grind, not of whichever scenario
#: happened to be fingerprinted.
_FINGERPRINT_INTERACTION: Final[InteractionContext] = InteractionContext(
    kube_context=None,
    context_epoch=0,
    focused_pane=PaneContext(kind="pods", scope="default", filter_pattern=None, selected=None),
    secondary_pane=None,
    timeline_cursor=None,
)


@dataclass(frozen=True, slots=True)
class PromptGrind:
    """The eval-only prompt levers, layered after the safety contract.

    Attributes:
        tier_pack: Replacement text for layer 3, the tier operating pack.
            `None` keeps the shipped pack.
        overlay: Additional layer-5 text, published as `eval-overlay`.
            `None` adds nothing.
    """

    tier_pack: str | None = None
    overlay: str | None = None

    @property
    def active(self) -> bool:
        """True when this grind changes anything the model reads."""
        return self.tier_pack is not None or self.overlay is not None


#: The grind that changes nothing — the shipped packs, no overlay.
NO_GRIND: Final[PromptGrind] = PromptGrind()


class UnknownEvalToolError(ValueError):
    """`omit_tools` names a tool the resolved policy never armed."""


def resolve_eval_policy(
    provider: Any,
    *,
    model_tier: str | None = None,
    environment: PolicyEnvironment = EVAL_ENVIRONMENT,
    omit_tools: frozenset[str] = frozenset(),
) -> ResolvedAgentPolicy:
    """Route one provider onto the policy an eval run will use.

    The production router over the production catalog, with the eval
    environment and (optionally) an operator-named tier. `omit_tools`
    narrows the *armed* surface for a controlled arm (#221): dropping a
    name here really unarms it, so the tool harness refuses the call and
    the executor is never reached.

    Args:
        provider: The `LLMProvider` whose descriptor and capabilities are
            routed. Never called — only read.
        model_tier: `"low"`, `"high"`, or `None` for automatic routing.
        environment: Capability facts to resolve against.
        omit_tools: Names to drop from the armed surface.

    Returns:
        The immutable policy every repetition of the run is composed
        against.

    Raises:
        UnknownEvalToolError: `omit_tools` names something this policy
            did not arm, which would publish a reduced arm identical to
            the full one.
    """
    policy = ModelRouter(MODEL_CATALOG).resolve(
        descriptor=provider.descriptor,
        provider_capabilities=provider.capabilities,
        explicit_tier=model_tier,
        environment=environment,
    )
    if not omit_tools:
        return policy
    armed = {str(tool["function"]["name"]) for tool in policy.tools}
    unknown = sorted(omit_tools - armed)
    if unknown:
        raise UnknownEvalToolError(
            f"{', '.join(unknown)} not on the armed surface; this policy arms "
            f"{', '.join(sorted(armed))}"
        )
    kept = tuple(tool for tool in policy.tools if tool["function"]["name"] not in omit_tools)
    return replace(policy, tools=kept)


def armed_tool_names(policy: ResolvedAgentPolicy) -> tuple[str, ...]:
    """The exact tool names this policy armed, sorted for reporting."""
    return tuple(sorted(str(tool["function"]["name"]) for tool in policy.tools))


def _ground_policy(policy: ResolvedAgentPolicy, grind: PromptGrind) -> ResolvedAgentPolicy:
    """The policy a ground run composes against (overlay ids only)."""
    if grind.overlay is None:
        return policy
    return replace(policy, prompt_overlay_ids=(*policy.prompt_overlay_ids, EVAL_OVERLAY_ID))


def build_prompt_harness(policy: ResolvedAgentPolicy, grind: PromptGrind) -> PromptHarness:
    """The production prompt harness, with the eval's ground layers injected."""
    packs: Mapping[str, str] | None = None
    if grind.tier_pack is not None:
        packs = {**PROMPT_PACKS, policy.prompt_pack_id: grind.tier_pack}
    overlays: Mapping[str, str] | None = None
    if grind.overlay is not None:
        overlays = {**MODEL_PROMPT_OVERLAYS, EVAL_OVERLAY_ID: grind.overlay}
    return PromptHarness(packs=packs, model_overlays=overlays)


def _compose_static(policy: ResolvedAgentPolicy, prompts: PromptHarness) -> str:
    """Layers 1-7 for an already-ground policy.

    Composed through the real harness against a fixed workspace and an
    unknown cluster, so no per-turn layer (cluster note, handoff note)
    contributes: what comes back is exactly the static prompt every turn
    of the run will carry.
    """
    return prompts.compose(
        "",
        PromptInputs(
            policy=policy,
            interaction=_FINGERPRINT_INTERACTION,
            cluster=EVAL_CLUSTER,
            user_rules=(),
            previous_interaction=None,
        ),
    ).system_message


def static_prompt(policy: ResolvedAgentPolicy, grind: PromptGrind = NO_GRIND) -> str:
    """The system message layers 1-7 a resolved policy and grind produce.

    Args:
        policy: The policy as `resolve_eval_policy` returned it — not one
            a grind has already been applied to.
        grind: The eval-only prompt levers.
    """
    return _compose_static(_ground_policy(policy, grind), build_prompt_harness(policy, grind))


@dataclass(frozen=True, slots=True)
class EvalHarness:
    """One composed eval session and the collaborators it was built from.

    Every field is the *same object* the session runs on, so a test or a
    runner can read the conversation, the gateway snapshot, or the bridge
    the model drove without reaching into the session's internals.
    """

    session: DefaultAgentSession
    engine: NativeAgentEngine
    gateway: RequestGateway
    outbound_policy: OutboundPolicy
    tools: ToolHarness
    conversation: ConversationState
    prompts: PromptHarness
    policy: ResolvedAgentPolicy
    bridge: AgentUiBridge
    execution: RecordedExecution
    cluster: ClusterFacts
    user_rules: tuple[str, ...]
    grind: PromptGrind

    @property
    def armed_tool_names(self) -> tuple[str, ...]:
        """The exact tool names this run armed."""
        return armed_tool_names(self.policy)

    @property
    def overlay_ids(self) -> tuple[str, ...]:
        """Prompt overlay ids composed into this run's system message."""
        return self.policy.prompt_overlay_ids

    def static_prompt(self) -> str:
        """The system-prompt layers every turn of this run carries."""
        return _compose_static(self.policy, self.prompts)


def build_eval_harness(
    *,
    provider: Any,
    execution: Any,
    bridge: AgentUiBridge,
    policy: ResolvedAgentPolicy | None = None,
    model_tier: str | None = None,
    omit_tools: frozenset[str] = frozenset(),
    environment: PolicyEnvironment = EVAL_ENVIRONMENT,
    cluster: ClusterFacts = EVAL_CLUSTER,
    user_rules: tuple[str, ...] = (),
    grind: PromptGrind = NO_GRIND,
) -> EvalHarness:
    """Compose one whole eval session over an already-built provider.

    The composition sequence is `__main__._build_session`'s, in the same
    order and with the same collaborators, so an eval measures the graph
    the TUI runs.

    Args:
        provider: The `LLMProvider` this run talks to (live, or scripted).
        execution: The tool executor. A plain string-only executor is
            adapted with `as_recorded`, so a pack may hand over whatever
            it built.
        bridge: The typed workspace bridge, normally an `EvalUiBridge`
            built from the fixture's authored interaction.
        policy: An already-resolved policy. When `None`, one is resolved
            from `model_tier`, `environment` and `omit_tools`; when given,
            those three are already reflected in it and are ignored.
        model_tier: `"low"`, `"high"`, or `None` for automatic routing.
        omit_tools: Names to drop from the armed surface (#221).
        environment: Capability facts to route against.
        cluster: The cluster facts composed into the prompt.
        user_rules: Operator rules composed into every turn.
        grind: The eval-only prompt levers.

    Returns:
        The composed session plus every collaborator it owns.
    """
    base = (
        policy
        if policy is not None
        else resolve_eval_policy(
            provider, model_tier=model_tier, environment=environment, omit_tools=omit_tools
        )
    )
    # One policy from here on: the grind only ever *adds* an overlay id,
    # and every collaborator must agree on which surface is armed.
    resolved = _ground_policy(base, grind)
    recorded = as_recorded(execution)
    tools = ToolHarness(
        policy=resolved,
        execution=recorded,
        bridge=bridge,
        evidence=EvidenceLedger(),
    )
    conversation = ConversationState(
        max_history_chars=resolved.max_history_chars,
        strict_history_budget=resolved.strict_history_budget,
    )
    outbound_policy = RequestGateway.prepare_policy(resolved)
    gateway = RequestGateway(provider, outbound_policy)
    engine = NativeAgentEngine(conversation=conversation, gateway=gateway, tools=tools)
    prompts = build_prompt_harness(base, grind)
    session = DefaultAgentSession(
        engine=engine,
        bridge=bridge,
        prompt_harness=prompts,
        conversation=conversation,
        gateway=gateway,
        tools=tools,
        policy=resolved,
        cluster=cluster,
        user_rules=user_rules,
    )
    return EvalHarness(
        session=session,
        engine=engine,
        gateway=gateway,
        outbound_policy=outbound_policy,
        tools=tools,
        conversation=conversation,
        prompts=prompts,
        policy=resolved,
        bridge=bridge,
        execution=recorded,
        cluster=cluster,
        user_rules=user_rules,
        grind=grind,
    )


def eval_surface_names(model_tier: str | None = None) -> frozenset[str]:
    """The tool names an eval at *model_tier* can arm, without a provider.

    The armed surface is a function of the tier and the eval environment
    alone, so it can be answered while parsing arguments — before any
    provider exists. An omitted tier answers for the low surface, which
    is what automatic routing picks for every model the catalog knows
    today; naming `--model-tier high` is how a campaign reduces a
    high-tier-only tool.
    """
    probe = ModelRouter(MODEL_CATALOG).resolve(
        descriptor=ModelDescriptor("eval", "eval"),
        provider_capabilities=ModelCapabilities.unknown(),
        explicit_tier=model_tier or ModelTier.LOW.value,
        environment=EVAL_ENVIRONMENT,
    )
    return frozenset(armed_tool_names(probe))
