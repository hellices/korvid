"""Public model-profile vocabulary (design §Public Agent Boundary).

`ui/` imports this module to render the setup wizard. It must never grow
an import of `korvid.providers` or of any model SDK: the base TUI has
neither, and the layer rules forbid the first outright.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from korvid.core.config import (
    ConnectionAuthConfig,
    ModelConnectionConfig,
    ModelConnectionsConfig,
    is_valid_profile_name,
)

if TYPE_CHECKING:
    from korvid.agent.provider import LLMProvider

    #: Builds the live provider for a reference its flow claimed. Typed
    #: under `TYPE_CHECKING` only: `ui/` imports this module to render the
    #: setup wizard, and `agent.provider` drags the whole session graph in.
    SpecialFlowProviderBuilder = Callable[[ModelConnectionConfig], LLMProvider | None]

    #: Starts an interactive sign-in for a claimed reference and returns
    #: what the operator has to act on, or `None` when the profile needs
    #: no sign-in.
    SpecialFlowAuthStarter = Callable[
        [ModelConnectionConfig], Awaitable["DeviceLoginPrompt | None"]
    ]

    #: Completes a sign-in and returns the *credential key* the profile
    #: will name — never the secret itself.
    SpecialFlowAuthFinisher = Callable[[ModelConnectionConfig], Awaitable[str | None]]

__all__ = [
    "AuthMethodDescriptor",
    "ConnectionAuthConfig",
    "DeviceLoginPrompt",
    "EndpointRequirement",
    "MetadataRefresh",
    "ModelCatalog",
    "ModelConnectionConfig",
    "ModelConnectionsConfig",
    "ModelEntry",
    "ModelEntrySource",
    "SetupField",
    "SetupFieldKind",
    "SpecialFlow",
    "split_reference",
    "suggest_profile_name",
]


class SetupFieldKind(Enum):
    TEXT = "text"
    SECRET_REF = "secret_ref"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    CHOICE = "choice"


@dataclass(frozen=True, slots=True)
class SetupField:
    """One declarative prompt. Data, never executable UI."""

    key: str
    label: str
    kind: SetupFieldKind
    required: bool = False
    default: str | None = None
    choices: tuple[str, ...] = ()
    help_text: str | None = None


@dataclass(frozen=True, slots=True)
class AuthMethodDescriptor:
    id: str
    display_name: str
    fields: tuple[SetupField, ...] = ()


class EndpointRequirement(Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    UNSUPPORTED = "unsupported"


class ModelEntrySource(Enum):
    """Where a catalog entry came from. Display and provenance only."""

    LITELLM = "litellm"
    MODELS_DEV = "models.dev"
    ENDPOINT = "endpoint"
    MANUAL = "manual"


class MetadataRefresh(Enum):
    """What an explicit metadata refresh did, in the operator's terms.

    The catalog's optional enrichment layer is the only thing behind this,
    and `ui/` must be able to render the answer without importing
    `korvid.providers` — the layer rules forbid it, and the base install
    does not ship that stack at all. So the vocabulary names *outcomes*,
    never a source, a vendor or an HTTP status.
    """

    #: New metadata was fetched and is now in the catalog.
    UPDATED = "updated"
    #: The source was revalidated and had nothing new.
    UNCHANGED = "unchanged"
    #: A fresh local copy was used; no request was made.
    CACHED = "cached"
    #: The refresh could not complete. Whatever was there is still there.
    UNAVAILABLE = "unavailable"
    #: This installation has no metadata source — switched off in config,
    #: or the optional extra is absent. Nothing was contacted.
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One connectable model, as data.

    `provider_id` is informational — it is rendered and used for grouping
    in search results. Nothing dispatches on it: routing is
    `litellm.get_llm_provider`'s job.

    Every capability field is independently unknown (`None`). A fact is
    set only where the source directly asserts the equivalent fact; it is
    never inferred from the name.
    """

    reference: str
    provider_id: str
    display_name: str | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    source: ModelEntrySource = ModelEntrySource.LITELLM
    credential_env_hints: tuple[str, ...] = ()
    endpoint_hint: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceLoginPrompt:
    verification_uri: str
    user_code: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class SpecialFlow:
    """A flow the standard transport cannot own, declared as data.

    Not a provider list. A flow claims *one* reference prefix, or a named
    boolean option on a reference it otherwise shares, and supplies auth
    and transport for exactly what it claims. It never contributes to
    model search as a vendor choice and never gates a reference it did
    not claim.
    """

    prefix: str
    display_name: str
    auth_methods: tuple[AuthMethodDescriptor, ...]
    option_fields: tuple[SetupField, ...] = ()
    endpoint: EndpointRequirement = EndpointRequirement.OPTIONAL
    claims_option: str | None = None
    #: Builds the provider for a reference this flow claimed, or None when
    #: it cannot. `None` means the flow is a *declaration only*: it still
    #: keeps the reference away from the standard transport (which is the
    #: whole point of claiming a prefix), but nothing can be built from it
    #: yet, so the factory refuses rather than falling through to routing.
    build_provider: SpecialFlowProviderBuilder | None = None
    #: Starts the flow's own sign-in. `None` means the flow needs none:
    #: the wizard skips the stage rather than inventing one.
    begin_auth: SpecialFlowAuthStarter | None = None
    #: Completes the sign-in and names the stored credential. `None` for
    #: the same reason as `begin_auth`.
    finish_auth: SpecialFlowAuthFinisher | None = None


def split_reference(reference: str) -> tuple[str, str]:
    """Split `provider/model` on the **first** slash.

    Returns `("", reference)` when there is no slash: LiteLLM resolves a
    bare reference against its own default-provider rules, and korvid
    must not pretend to know better.
    """
    prefix, separator, tag = reference.partition("/")
    if not separator:
        return "", reference
    return prefix, tag


def suggest_profile_name(reference: str, taken: Collection[str]) -> str:
    """Return a readable valid profile name that does not collide."""
    _provider, tag = split_reference(reference)
    base = tag or reference
    if not is_valid_profile_name(base):
        base = "default"
    name = base
    counter = 1
    while name in taken:
        name = f"{base}-{counter}"
        counter += 1
    return name


class ModelCatalog(ABC):
    """Everything the setup UI needs to know, answered from data."""

    @abstractmethod
    def search(self, query: str, *, limit: int = 50) -> tuple[ModelEntry, ...]:
        """Rank catalog entries against a free-text query. Never raises."""

    @abstractmethod
    def entry(self, reference: str) -> ModelEntry | None:
        """The catalog's record for an exact reference, or None."""

    @abstractmethod
    def auth_methods(
        self, reference: str, *, endpoint: str | None = None
    ) -> tuple[AuthMethodDescriptor, ...]:
        """Auth methods valid for this reference, most specific first.

        `endpoint` is the endpoint the operator has entered so far, or
        None if they have not entered one. It is here so the catalog can
        mirror the factory's `none`-auth rule **exactly** rather than
        approximating it: `none` is offered only when `endpoint` is a
        non-empty string, because a keyless request with no operator
        endpoint goes to whatever default host the SDK picks. Offering a
        method the factory will refuse at build time is a trap; deciding
        it from a provider table is impossible, because LiteLLM's data
        carries no host (see the API baseline table).

        The parameter is a plain string the caller already has, not a
        lookup — implementations must not route, resolve or fetch to
        answer it. The UI's endpoint stage therefore runs *before* its
        auth-method stage (Task 11).
        """

    @abstractmethod
    def option_fields(self, reference: str) -> tuple[SetupField, ...]:
        """Declarative option prompts for this reference."""

    @abstractmethod
    def endpoint_requirement(self, reference: str) -> EndpointRequirement:
        """Whether the setup UI must, may, or must not ask for an endpoint.

        Answered from the special-flow registry alone: a flow that
        declares a requirement wins, and everything else is OPTIONAL.
        There is no provider table behind this — LiteLLM's `model_cost`
        records carry no host field of any kind, so no data exists from
        which "this provider needs an endpoint" could be derived.
        OPTIONAL is also the honest answer: any reference may be pointed
        at a proxy, a gateway or a self-hosted clone.
        """

    @abstractmethod
    async def discover(self, profile: ModelConnectionConfig) -> tuple[ModelEntry, ...]:
        """Live-list models from the profile's endpoint. Best effort: an
        empty tuple means "type it yourself", never an error dialog."""

    @abstractmethod
    async def refresh_metadata(self, *, force: bool = False) -> MetadataRefresh:
        """Revalidate the optional metadata layer, because a human asked.

        This is the *only* way that layer is ever contacted: it is never
        awaited at startup, on mount, on a keystroke, or on any routing
        path. The setup UI binds one visible key to it and renders the
        answer; everything else in korvid reads whatever is already
        cached.

        Implementations return an outcome rather than raising: the caller
        is a UI worker, and an exception there would tear down a screen
        the operator is in the middle of using. `DISABLED` is a normal
        answer — an installation may deliberately have no source at all.

        Args:
            force: `True` when the request came from an operator who is
                waiting for it. A metadata layer may keep a local copy and
                serve it without asking anyone — the right default for a
                refresh korvid decided to make, and the wrong answer for a
                key a human pressed *because* they want the current
                document. `ui/` cannot reach past this boundary to say so,
                so the boundary carries it.
        """

    @abstractmethod
    async def test(self, profile: ModelConnectionConfig) -> str:
        """Probe the profile and return a short human-readable result."""

    @abstractmethod
    async def begin_auth(self, profile: ModelConnectionConfig) -> DeviceLoginPrompt | None: ...

    @abstractmethod
    async def finish_auth(self, profile: ModelConnectionConfig) -> str | None: ...
