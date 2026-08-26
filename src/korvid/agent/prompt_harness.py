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
from korvid.core.redaction import RedactionRecord, redact_document
from korvid.k8s.csp import UNKNOWN_PROVIDER
from korvid.tools.executor import UI_TOOL_NAMES, WRITE_TOOL_NAMES

#: Every dynamic text field is bounded before it reaches a provider — an
#: unbounded namespace, filter, or context name is attacker-controlled
#: text that would otherwise ride into the prompt on every later request
#: of the turn (design doc §7 decision).
_DEFAULT_FIELD_BOUND: Final[int] = 512
_FILTER_FIELD_BOUND: Final[int] = 2_048

#: The handoff note quotes two context names inside one sentence, so both
#: are bounded harder than a standalone field would be — a note is worth
#: at most a couple of lines of the system prompt.
_HANDOFF_CONTEXT_BOUND: Final[int] = 200

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
    not re-validate that). `previous_interaction` is the snapshot the
    *previous* turn started from, or None on the first turn: the session
    (task 11) hands over typed state and this harness alone decides
    whether a handoff note is warranted and what it says. A raw note
    string would make the session author model-facing prose, which is
    exactly the split this module exists to prevent.

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
    previous_interaction: InteractionContext | None = None


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
        packs: Layer-3 registry, keyed by prompt pack id. Defaults to the
            shipped `PROMPT_PACKS`. Injectable for exactly one reason:
            the eval harness grinds tier-pack wording to find better text
            (issue #316 task 13). A ground pack still layers *after* the
            immutable safety contract and never replaces it.
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
        packs: Mapping[str, str] | None = None,
        provider_overlays: Mapping[str, str] | None = None,
        model_overlays: Mapping[str, str] | None = None,
    ) -> None:
        self._packs = packs if packs is not None else PROMPT_PACKS
        self._provider_overlays = (
            provider_overlays if provider_overlays is not None else PROVIDER_PROMPT_OVERLAYS
        )
        self._model_overlays = (
            model_overlays if model_overlays is not None else MODEL_PROMPT_OVERLAYS
        )

    def validate(self, policy: ResolvedAgentPolicy, user_rules: tuple[str, ...] = ()) -> None:
        """Check that `policy` composes, without needing a live snapshot.

        Layers 1-7 depend only on the policy and the operator rules, so
        they can be checked before there is any workspace to snapshot —
        which is what lets `AgentSession` (issue #316 task 11) refuse a
        bad policy at construction time and refuse a bad *retarget*
        before it swaps anything on a live session. The dynamic layers
        (cluster note, handoff note, workspace context) are per-turn and
        deliberately out of scope here.

        Args:
            policy: The resolved policy to check.
            user_rules: `config.agent_rules` the same turn would compose.

        Raises:
            UnknownPromptPackError: `policy.prompt_pack_id` is not a
                shipped pack.
            UnknownPromptOverlayError: `policy.prompt_overlay_ids` names
                an id absent from the shipped/injected registry.
            StaticPromptTooLargeError: the static layers (1-7) exceed
                `_MAX_STATIC_PROMPT_FRACTION` of
                `policy.max_history_chars`.
        """
        self._static_prompt(policy, user_rules)

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
        static_prompt = self._static_prompt(inputs.policy, inputs.user_rules)

        dynamic_layers = [
            note
            for note in (
                cluster_context_note(inputs.cluster),
                _handoff_note(inputs.previous_interaction, inputs.interaction),
            )
            if note
        ]
        system_message = "\n\n".join([static_prompt, *dynamic_layers])

        user_message = f"{user_text}\n\n{interaction_context_note(inputs.interaction)}"

        return ComposedPrompt(system_message=system_message, user_message=user_message)

    def _static_prompt(self, policy: ResolvedAgentPolicy, user_rules: tuple[str, ...]) -> str:
        """Build and budget-check layers 1-7. One builder, so `validate`
        cannot accept what `compose` would refuse.
        """
        static_layers = [
            SAFETY_CONTRACT,
            COMMON_ROLE,
            self._tier_pack(policy.prompt_pack_id),
            *self._overlay_layers(policy),
            *_user_rule_layer(user_rules),
            _capability_clauses(policy.tools),
        ]
        static_prompt = "\n\n".join(layer for layer in static_layers if layer)
        _check_static_budget(static_prompt, policy.max_history_chars)
        return static_prompt

    def _tier_pack(self, prompt_pack_id: str) -> str:
        try:
            return self._packs[prompt_pack_id]
        except KeyError:
            raise UnknownPromptPackError(
                f"prompt pack {prompt_pack_id!r} is not a shipped pack"
            ) from None

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


def _handoff_note(previous: InteractionContext | None, current: InteractionContext) -> str | None:
    """Layer 9: tell the model the workspace moved under it.

    Driven by `context_epoch`, not by the context *name*: the epoch is
    the interaction layer's own "everything you knew is stale" counter
    (task 1), so a reconnect to a same-named context still invalidates
    prior reads and a pure pane move inside one context does not. The
    caller (`AgentSession`) supplies typed snapshots and never a
    sentence; every word the model sees about the switch is written
    here, and both names are bounded and escaped like any other
    untrusted field.

    Args:
        previous: Snapshot the previous turn started from, or None on a
            first turn.
        current: Snapshot this turn is starting from.

    Returns:
        A one-paragraph note naming both contexts and both epochs, or
        `None` when nothing changed.
    """
    if previous is None or previous.context_epoch == current.context_epoch:
        return None
    return (
        "The workspace changed since the previous turn: it was "
        f"{_context_label(previous.kube_context)} at context epoch "
        f"{previous.context_epoch} and the user has since switched to "
        f"{_context_label(current.kube_context)} at context epoch "
        f"{current.context_epoch}. Resource state, names, and identifiers "
        "you learned before this switch may not exist here — re-read what "
        "you need in the current context before acting on it, and never "
        "carry a resource identity across the switch."
    )


def _context_label(kube_context: str | None) -> str:
    bounded = _bounded(kube_context, _HANDOFF_CONTEXT_BOUND)
    if not bounded:
        return "an unnamed context"
    return _encode(bounded)


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


def interaction_context_note(
    interaction: InteractionContext,
    *,
    max_chars: int | None = None,
) -> str:
    """Bounded model-facing encoding of the currently visible workspace."""
    return _bounded_interaction_note(
        _interaction_payload(interaction),
        max_chars,
    )


def interaction_context_note_with_redactions(
    interaction: InteractionContext,
    *,
    max_chars: int | None = None,
) -> tuple[str, tuple[RedactionRecord, ...]]:
    """Structurally redact and encode the visible workspace as valid JSON."""
    payload, records = redact_document(
        _interaction_payload(interaction),
        path="workspace_context",
    )
    return (
        _bounded_interaction_note(payload, max_chars),
        tuple(records),
    )


def _bounded_interaction_note(
    payload: Any,
    max_chars: int | None,
) -> str:
    """Encode one payload while keeping truncation structurally valid."""
    note = f"{_WORKSPACE_CONTEXT_LABEL}{_encode(payload)}"
    if max_chars is None or len(note) <= max_chars:
        return note
    for field_limit in (256, 128, 64, 32):
        compact = _bound_payload_strings(payload, field_limit)
        note = f"{_WORKSPACE_CONTEXT_LABEL}{_encode(compact)}"
        if len(note) <= max_chars:
            return note
    source = payload if isinstance(payload, dict) else {}
    focused = source.get("focused_pane")
    focused_source = focused if isinstance(focused, dict) else {}
    minimal = {
        "context_epoch": source.get("context_epoch"),
        "focused_pane": {
            "kind": _bounded(focused_source.get("kind"), 32),
            "scope": _bounded(focused_source.get("scope"), 32),
        },
        "truncated": True,
    }
    return f"{_WORKSPACE_CONTEXT_LABEL}{_encode(minimal)}"


def _bound_payload_strings(value: Any, limit: int) -> Any:
    """Recursively bound strings while preserving JSON structure."""
    if isinstance(value, str):
        return _bounded(value, limit)
    if isinstance(value, dict):
        return {key: _bound_payload_strings(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_bound_payload_strings(item, limit) for item in value]
    return value
