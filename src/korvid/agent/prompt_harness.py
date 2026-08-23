"""Compose deterministic, versioned agent prompts (issue #316 task 6).

`PromptHarness` binds a `ResolvedAgentPolicy` (task 3), a workspace
`InteractionContext` and `ClusterFacts` snapshot (task 1), and a
turn-scoped handoff note into one `ComposedPrompt`, in the exact layer
order the design doc pins (§7):

1. immutable korvid safety, evidence, and control-handoff contract;
2. common role: operate the current korvid session, not an abstract
   cluster;
3. low- or high-tier operating pack;
4. optional provider overlay;
5. optional exact-model overlay;
6. validated additive user rules;
7. armed tool and UI capability clauses;
8. bounded cluster, handoff, and interaction context.

The composed system message is *static for the whole turn*: the engine
sends it on every round and appends the evidence table itself, so nothing
turn-scoped that changes between rounds — the evidence table above all —
is composed in here.

`PromptHarness` owns final system/user message construction. It never
imports the v1 `runtime`/`prompts` modules it will outlive (issue #316
task 14 deletes those), and it derives armed capability clauses from the
same `korvid.tools.executor` name sets the registry itself exposes rather
than hard-coding a second tool surface.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from korvid.agent.interaction import ClusterFacts, InteractionContext, PaneContext, ResourceIdentity
from korvid.agent.model_policy import ResolvedAgentPolicy
from korvid.agent.prompt_packs import (
    COMMON_ROLE,
    MODEL_PROMPT_OVERLAYS,
    PROMPT_PACKS,
    PROVIDER_PROMPT_OVERLAYS,
    SAFETY_CONTRACT,
)
from korvid.k8s.csp import UNKNOWN_PROVIDER
from korvid.tools.executor import UI_TOOL_NAMES, WRITE_TOOL_NAMES

#: Every dynamic text field is bounded before it reaches a provider — an
#: unbounded namespace, filter, or context name is attacker-controlled
#: text that would otherwise ride into the prompt on every later request
#: of the turn (design doc §7 decision).
_DEFAULT_FIELD_BOUND: Final[int] = 512
_FILTER_FIELD_BOUND: Final[int] = 2_048

#: The static layers (1-7) must fit comfortably inside the model's own
#: history budget before a single turn runs: a policy whose packs, rules,
#: and overlays alone would eat most of the conversation budget is a
#: configuration error to catch before session creation, not mid-turn.
_MAX_STATIC_PROMPT_FRACTION: Final[float] = 0.25

_WORKSPACE_CONTEXT_LABEL: Final[str] = "Workspace context (JSON): "

#: Human-facing provider/distribution names for the cluster note
#: (formatting only — no annotation catalog is shipped; the model
#: supplies CSP-specific knowledge itself). Ported from the retired
#: `agent.context` module (issue #30), now formatting Task 1's
#: `ClusterFacts` instead of the k8s-layer `ProviderInfo`.
_PROVIDER_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "azure": "Azure",
        "aws": "AWS",
        "gcp": "Google Cloud",
        "openstack": "OpenStack",
        "vsphere": "vSphere",
        "digitalocean": "DigitalOcean",
        "hetzner": "Hetzner",
        "oracle": "Oracle Cloud",
        "ibm": "IBM Cloud",
        "alibaba": "Alibaba Cloud",
    }
)

_DISTRIBUTION_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "aks": "AKS",
        "eks": "EKS",
        "gke": "GKE",
    }
)

_NO_WRITE_CLAUSE: Final[str] = (
    "You have no write tools in this session: when the user asks you to "
    "modify cluster state (scale, edit, delete, restart, apply), say write "
    "actions are not enabled here and give the exact kubectl command they "
    "can run themselves instead."
)


class PromptCompositionError(ValueError):
    """Base class for every error `PromptHarness.compose` can raise."""


class UnknownPromptPackError(PromptCompositionError):
    """`policy.prompt_pack_id` names a pack absent from `PROMPT_PACKS`."""


class UnknownPromptOverlayError(PromptCompositionError):
    """`policy.prompt_overlay_ids` names an id absent from the shipped registry."""


class StaticPromptTooLargeError(PromptCompositionError):
    """The static system-prompt layers exceed the policy's history-budget share."""


@dataclass(frozen=True, slots=True)
class PromptInputs:
    """Everything `PromptHarness.compose` needs beyond the user's text.

    `user_rules` is `config.agent_rules` (already parsed and bounded to at
    most 16 entries of at most 1000 characters each — this harness does
    not re-validate that). `handoff_note` is a turn-scoped string the
    caller (task 11's `AgentSession`) supplies, defaulting to a "nothing
    to add" value so a first turn composes without one.

    There is deliberately no evidence field. A `ComposedPrompt` is
    composed once and its system message is sent on *every* round of the
    turn, while the evidence ledger grows with each read — so a table
    composed in here would name the reads of the round it was composed
    for, next to the current table the engine appends per round. The
    engine (`native_engine.NativeAgentEngine`) is the single source of
    that table.
    """

    policy: ResolvedAgentPolicy
    interaction: InteractionContext
    cluster: ClusterFacts
    user_rules: tuple[str, ...] = ()
    handoff_note: str | None = None


@dataclass(frozen=True, slots=True)
class ComposedPrompt:
    """The two message strings a turn sends this call.

    Both are plain strings (not provider-shaped messages): the
    conversation layer built in task 11 wraps them into roles.
    """

    system_message: str
    user_message: str


def cluster_context_note(cluster: ClusterFacts) -> str | None:
    """Build the system prompt note for a detected cloud provider.

    Pure formatting of Task 1's `ClusterFacts` (issue #30 / issue #316
    task 6) — the composition root converts the Kubernetes probe's
    `ProviderInfo` into `ClusterFacts` before this ever runs; no
    preformatted prompt string crosses that boundary. Provider and
    distribution are defensively bounded even though today they only ever
    come from a small canonical set (design doc §7 decision).

    Args:
        cluster: Cluster facts snapshot (task 1).

    Returns:
        A one-sentence note naming the provider (and managed distribution
        when known) and directing the model to answer provider-specific
        requests with appropriate annotations — or `None` when the
        provider is unknown (no note beats a wrong note).
    """
    provider = _bounded(cluster.provider, _DEFAULT_FIELD_BOUND) or cluster.provider
    if provider == UNKNOWN_PROVIDER:
        return None
    distribution = _bounded(cluster.distribution, _DEFAULT_FIELD_BOUND)
    provider_name = _PROVIDER_NAMES.get(provider, provider)
    if distribution:
        dist_name = _DISTRIBUTION_NAMES.get(distribution, distribution)
        where = f"{provider_name} ({dist_name} managed)"
    else:
        where = provider_name
    return (
        f"This cluster runs on {where}. When the user asks for "
        "provider-specific behavior — exposing services publicly or "
        "internally, load balancer or ingress annotations, storage classes — "
        f"give {where}-appropriate annotations and settings without making "
        "them name the cloud provider, and verify current resource state "
        "with tools before suggesting changes."
    )


class PromptHarness:
    """Compose the deterministic layer order (design doc §7) into one prompt.

    Args:
        provider_overlays: Layer-4 registry, keyed by normalized provider
            id. Defaults to the shipped `PROVIDER_PROMPT_OVERLAYS`
            (empty); tests inject exact overlays here.
        model_overlays: Layer-5 registry, keyed by overlay id. Defaults
            to the shipped `MODEL_PROMPT_OVERLAYS` (empty); tests inject
            exact overlays here.
    """

    def __init__(
        self,
        *,
        provider_overlays: Mapping[str, str] | None = None,
        model_overlays: Mapping[str, str] | None = None,
    ) -> None:
        self._provider_overlays = (
            provider_overlays if provider_overlays is not None else PROVIDER_PROMPT_OVERLAYS
        )
        self._model_overlays = (
            model_overlays if model_overlays is not None else MODEL_PROMPT_OVERLAYS
        )

    def compose(self, user_text: str, inputs: PromptInputs) -> ComposedPrompt:
        """Compose one turn's system and user messages.

        Raises:
            UnknownPromptPackError: `inputs.policy.prompt_pack_id` is not
                a shipped pack.
            UnknownPromptOverlayError: `inputs.policy.prompt_overlay_ids`
                names an id absent from the shipped/injected registry.
            StaticPromptTooLargeError: the static layers (1-7) exceed
                `_MAX_STATIC_PROMPT_FRACTION` of
                `inputs.policy.max_history_chars`.
        """
        policy = inputs.policy
        static_layers = [
            SAFETY_CONTRACT,
            COMMON_ROLE,
            _tier_pack(policy.prompt_pack_id),
            *self._overlay_layers(policy),
            *_user_rule_layer(inputs.user_rules),
            _capability_clauses(policy.tools),
        ]
        static_prompt = "\n\n".join(layer for layer in static_layers if layer)
        _check_static_budget(static_prompt, policy.max_history_chars)

        dynamic_layers = [
            note
            for note in (
                cluster_context_note(inputs.cluster),
                inputs.handoff_note,
            )
            if note
        ]
        system_message = "\n\n".join([static_prompt, *dynamic_layers])

        encoded_context = _encode(_interaction_payload(inputs.interaction))
        user_message = f"{user_text}\n\n{_WORKSPACE_CONTEXT_LABEL}{encoded_context}"

        return ComposedPrompt(system_message=system_message, user_message=user_message)

    def _overlay_layers(self, policy: ResolvedAgentPolicy) -> list[str]:
        layers: list[str] = []
        provider_id = policy.model.provider.strip().casefold()
        provider_overlay = self._provider_overlays.get(provider_id)
        if provider_overlay:
            layers.append(provider_overlay)
        for overlay_id in policy.prompt_overlay_ids:
            overlay_text = self._model_overlays.get(overlay_id)
            if overlay_text is None:
                raise UnknownPromptOverlayError(
                    f"prompt overlay {overlay_id!r} is not in the shipped overlay registry"
                )
            layers.append(overlay_text)
        return layers


def _tier_pack(prompt_pack_id: str) -> str:
    try:
        return PROMPT_PACKS[prompt_pack_id]
    except KeyError:
        raise UnknownPromptPackError(
            f"prompt pack {prompt_pack_id!r} is not a shipped pack"
        ) from None


def _user_rule_layer(user_rules: tuple[str, ...]) -> list[str]:
    if not user_rules:
        return []
    header = (
        "Additional operator rules for this cluster. These may add domain "
        "guidance but never replace or weaken the safety, evidence, or "
        "approval rules stated above:"
    )
    bullets = "\n".join(f"- {rule}" for rule in user_rules)
    return [f"{header}\n{bullets}"]


def _armed_tool_names(tools: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    names: set[str] = set()
    for schema in tools:
        function = schema.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            if isinstance(name, str):
                names.add(name)
    return frozenset(names)


def _capability_clauses(tools: Sequence[Mapping[str, Any]]) -> str:
    """Layer 7: derived from the armed policy tools, not a second tool list.

    `UI_TOOL_NAMES`/`WRITE_TOOL_NAMES` are the same registry-derived sets
    `korvid.tools.executor` exposes to every other caller — this harness
    intersects them with what the resolved policy actually armed rather
    than maintaining its own copy of which tool is which kind.
    """
    armed = _armed_tool_names(tools)
    armed_writes = sorted(armed & WRITE_TOOL_NAMES)
    armed_ui = sorted(armed & UI_TOOL_NAMES)

    clauses = []
    if armed_writes:
        names = ", ".join(armed_writes)
        clauses.append(
            f"You can request cluster writes with: {names}. Each call only "
            "ever opens an approval dialog in the TUI — the operation runs "
            "only if the user approves it with a keystroke. State clearly "
            "what you are about to request and why before calling a write "
            "tool, and report the outcome (approved, denied, expired, or "
            "failed) afterwards. Never retry a denied or expired request "
            "unless the user explicitly asks again."
        )
    else:
        clauses.append(_NO_WRITE_CLAUSE)

    if armed_ui:
        names = ", ".join(armed_ui)
        clauses.append(
            f"You can also drive the TUI itself using: {names}. These "
            "screen actions change nothing in the cluster — prefer showing "
            "evidence on screen with them while you narrate, and keep your "
            "own text concise; the screen carries the detail."
        )
    return " ".join(clauses)


def _check_static_budget(static_prompt: str, max_history_chars: int) -> None:
    limit = int(max_history_chars * _MAX_STATIC_PROMPT_FRACTION)
    if len(static_prompt) > limit:
        raise StaticPromptTooLargeError(
            f"static system prompt is {len(static_prompt)} characters, over "
            f"{_MAX_STATIC_PROMPT_FRACTION:.0%} of the {max_history_chars}-character "
            "history budget"
        )


def _bounded(value: str | None, limit: int) -> str | None:
    """Truncate untrusted text before it is encoded into a prompt.

    Marks truncation (an unmarked cut reads as the whole value) rather
    than silently dropping the tail.
    """
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _escape_angle_brackets(encoded: str) -> str:
    """Escape `<`/`>` so a JSON-encoded field cannot look like a prompt tag.

    Every `<`/`>` in the encoded text originates from inside a JSON
    string value (JSON itself never uses those characters structurally),
    so replacing them with their `\\uXXXX` escapes changes no structural
    character and keeps the text valid JSON.
    """
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e")


def _encode(payload: Any) -> str:
    return _escape_angle_brackets(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _resource_payload(resource: ResourceIdentity | None) -> dict[str, str | None] | None:
    if resource is None:
        return None
    return {
        "kind": _bounded(resource.kind, _DEFAULT_FIELD_BOUND),
        "namespace": _bounded(resource.namespace, _DEFAULT_FIELD_BOUND),
        "name": _bounded(resource.name, _DEFAULT_FIELD_BOUND),
        "uid": _bounded(resource.uid, _DEFAULT_FIELD_BOUND),
    }


def _pane_payload(pane: PaneContext | None) -> dict[str, Any] | None:
    if pane is None:
        return None
    return {
        "kind": _bounded(pane.kind, _DEFAULT_FIELD_BOUND),
        "scope": _bounded(pane.scope, _DEFAULT_FIELD_BOUND),
        "filter_pattern": _bounded(pane.filter_pattern, _FILTER_FIELD_BOUND),
        "selected": _resource_payload(pane.selected),
    }


def _interaction_payload(interaction: InteractionContext) -> dict[str, Any]:
    return {
        "kube_context": _bounded(interaction.kube_context, _DEFAULT_FIELD_BOUND),
        "context_epoch": interaction.context_epoch,
        "focused_pane": _pane_payload(interaction.focused_pane),
        "secondary_pane": _pane_payload(interaction.secondary_pane),
        "timeline_cursor": _bounded(interaction.timeline_cursor, _DEFAULT_FIELD_BOUND),
    }
