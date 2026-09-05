"""Where a profile becomes a live provider — and where the refusals live.

Everything korvid must *not* build is decided here, in one ordered pass,
and every refusal is a `logger.warning` plus `None`. Returning `None` is
deliberate: a profile that is merely unconfigured or misconfigured
disables the agent, it never stops korvid from starting.

Routing is delegated. There is no mapping from a provider name to a class
in this module — `get_llm_provider` resolves the reference and names the
provider, and a test greps this file for six vendor names and finds none.
The one thing korvid keeps for itself is the *order*: a claimed prefix is
taken by its own flow, or refused, before the routing call can see it,
because resolving one of those references starts an interactive device
login inside that call and writes a credential file.

The refusal order is cheapest-first, and each step needs nothing from the
steps below it:

1. a profile the parser already rejected,
2. a reference korvid would have to guess at,
3. a reference a special flow claims (delegated) or the deny-list claims
   (refused) — both before any LiteLLM call,
4. a trust bundle that cannot be loaded (a flow owns its own transport,
   so this comes after the claim),
5. keyless auth with no endpoint (one field of the operator's profile),
6. the credential, resolved by exactly the method the profile names,
7. the routing call,
8. a provider that genuinely has nowhere to send the request.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol

from korvid.agent.model_policy import CapabilitySource, ModelCapabilities, ModelDescriptor
from korvid.agent.model_profiles import (
    ModelCatalog,
    ModelConnectionConfig,
    ModelEntry,
    SpecialFlow,
    split_reference,
)
from korvid.providers import litellm_runtime, net
from korvid.providers.litellm_provider import LiteLLMProvider
from korvid.providers.litellm_request import OMIT_API_KEY, ResolvedApiKey, build_plan
from korvid.providers.litellm_settings import DEVICE_LOGIN_PREFIXES
from korvid.providers.special_flows import SpecialFlowRegistry, normalize_prefix

if TYPE_CHECKING:
    from korvid.agent.provider import LLMProvider

__all__ = ["OMIT_API_KEY", "CredentialStore", "create_provider_from_profile"]

logger = logging.getLogger(__name__)

#: The auth-settings key that names the environment variable, or the
#: keyring entry, holding the credential. A *name*, never a value.
_KEY_SETTING: Final = "key"

#: The service every korvid keyring entry is stored under.
_KEYRING_SERVICE: Final = "korvid"

#: Catalog fields that translate onto a `ModelCapabilities` fact of the
#: same name. `ModelEntry.max_output_tokens` is deliberately absent: it
#: has no capability counterpart and is display-only.
_CATALOG_FACTS: Final[tuple[str, ...]] = (
    "context_window_tokens",
    "supports_tools",
    "supports_reasoning",
)

#: Profile options that state a capability outright, as
#: `(option key, capability fact)`. An operator who sets one knows their
#: own deployment better than a table does, so these win.
_OPTION_FACTS: Final[tuple[tuple[str, str], ...]] = (("num_ctx", "context_window_tokens"),)


class CredentialStore(Protocol):
    """The one method a secret store has to offer to be usable here."""

    def load(self, key: str) -> str | None:
        """Return the stored secret for *key*, or None when there is none."""


class _KeyringStore:
    """The default `keyring` lookup, imported only when it is used."""

    def load(self, key: str) -> str | None:
        import keyring

        password: str | None = keyring.get_password(_KEYRING_SERVICE, key)
        return password


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


def create_provider_from_profile(
    profile: ModelConnectionConfig,
    *,
    catalog: ModelCatalog | None = None,
    flows: SpecialFlowRegistry | None = None,
    credentials: CredentialStore | None = None,
    ca_bundle: str | None = None,
) -> LLMProvider | None:
    """Build a provider, or None when the profile is unusable.

    Returns None — never raises — for a profile that is merely
    unconfigured or misconfigured: a bad profile disables the agent, it
    does not stop korvid from starting.

    Args:
        profile: The connection the operator configured.
        catalog: The model index, used for capability facts only. `None`
            leaves every fact unknown, which is the honest answer.
        flows: The special-flow registry. `None` behaves as an empty
            registry, which still claims the prefixes routing must never
            resolve.
        credentials: The secret store `keyring` auth reads. `None` uses
            the OS keyring directly.
        ca_bundle: Path to the trust bundle from `network.ca_bundle`, or
            `None` for the SDK's own trust. One trust decision covers
            every korvid-owned HTTPS client, so the same setting that
            reaches the cluster client reaches this one.

    Returns:
        The provider, or None with the reason logged as a warning.
    """
    if profile.config_error is not None:
        _refuse("profile %r was rejected: %s", profile.model, profile.config_error)
        return None

    reference = profile.model
    problem = _refuse_malformed_reference(reference)
    if problem is not None:
        _refuse("%s", problem)
        return None

    claimed, provider = _claimed_provider(profile, reference, flows)
    if claimed:
        return provider

    problem = _apply_trust(ca_bundle)
    if problem is not None:
        _refuse("%s", problem)
        return None

    problem = _refuse_keyless_without_endpoint(profile)
    if problem is not None:
        _refuse("%s", problem)
        return None

    resolved, api_key = _resolve_credential(profile, credentials)
    if not resolved:
        return None

    routed = _route(reference, _endpoint(profile))
    if routed is None:
        return None
    provider_id, model_tag = routed

    problem = _refuse_unreachable_provider(reference, _endpoint(profile), api_key)
    if problem is not None:
        _refuse("%s", problem)
        return None

    plan = build_plan(
        model=reference,
        api_key=api_key,
        base_url=_endpoint(profile),
        options=profile.options,
        supported=litellm_runtime.supported_params(model_tag, provider_id),
    )
    return LiteLLMProvider(
        plan=plan,
        descriptor=ModelDescriptor(provider=provider_id, model=model_tag),
        capabilities=_capabilities(reference, profile.options, catalog),
    )


def _refuse(message: str, *args: object) -> None:
    """Log one refusal and return None, so every caller reads the same."""
    logger.warning(f"{message} — the agent is disabled", *args)
    return None


def _endpoint(profile: ModelConnectionConfig) -> str | None:
    """The endpoint the operator named, or None. Whitespace is not a host."""
    endpoint = profile.endpoint
    if endpoint is None or not endpoint.strip():
        return None
    return endpoint.strip()


# ---------------------------------------------------------------------------
# Step 2 — a reference korvid would have to guess at
# ---------------------------------------------------------------------------


def _refuse_malformed_reference(reference: str) -> str | None:
    """Refuse a reference that is empty, half-written, or spaced.

    A bare tag with no separator is *not* malformed: LiteLLM resolves one
    against its own tables, and korvid must not pretend to know better.
    What it cannot do is invent the missing half of a written separator.
    """
    if not reference.strip():
        return "the profile names no model"
    if any(character.isspace() for character in reference):
        return f"model reference {reference!r} contains whitespace"
    prefix, tag = split_reference(reference)
    if "/" in reference and (not prefix or not tag):
        return f"model reference {reference!r} is missing its provider or its model"
    return None


# ---------------------------------------------------------------------------
# Step 3 — claims, before any LiteLLM call
# ---------------------------------------------------------------------------


def _claimed_provider(
    profile: ModelConnectionConfig,
    reference: str,
    flows: SpecialFlowRegistry | None,
) -> tuple[bool, LLMProvider | None]:
    """Whether a claim owns this reference, and what it built.

    Returns `(True, provider)` when a flow built one, `(True, None)` when
    the reference is claimed but nothing can serve it, and
    `(False, None)` when the reference is free to be routed. A claimed
    reference is never routed: that ordering is the whole reason this
    step exists.
    """
    registry = flows if flows is not None else SpecialFlowRegistry()
    flow = _claim(registry, reference, profile.options)
    if flow is not None:
        return True, _build_from_flow(flow, profile, reference)
    if _prefix_is_claimed(registry, reference):
        _refuse(
            "%r names a prefix korvid claims, and no flow is installed to serve it",
            reference,
        )
        return True, None
    return False, None


def _claim(
    registry: SpecialFlowRegistry, reference: str, options: Mapping[str, object]
) -> SpecialFlow | None:
    """The flow owning this reference by prefix or by named option.

    Every registry call is guarded: a third-party plugin that raises must
    disable itself, not the profiles it has nothing to do with.
    """
    lookups: tuple[Callable[[], SpecialFlow | None], ...] = (
        lambda: registry.claim(reference),
        lambda: registry.claim_by_option(reference, options),
    )
    for lookup in lookups:
        try:
            flow = lookup()
        except Exception:  # third-party plugin code can raise anything
            logger.warning("a special flow raised while claiming %r; ignoring it", reference)
            continue
        if flow is not None:
            return flow
    return None


def _prefix_is_claimed(registry: SpecialFlowRegistry, reference: str) -> bool:
    """Is this prefix claimed even though no flow answered for it?

    Falls back to the built-in deny-list when the registry itself raises,
    because that list is exactly the set of references that must never
    reach routing.
    """
    prefix, _tag = split_reference(reference)
    if not prefix:
        return False
    try:
        claimed = registry.claimed_prefixes
    except Exception:  # a broken registry must not unclaim the deny-list
        claimed = frozenset(normalize_prefix(name) for name in DEVICE_LOGIN_PREFIXES)
    return normalize_prefix(prefix) in claimed


def _build_from_flow(
    flow: SpecialFlow, profile: ModelConnectionConfig, reference: str
) -> LLMProvider | None:
    """Delegate to the flow that claimed the reference."""
    builder = flow.build_provider
    if builder is None:
        _refuse("the flow claiming %r declares no transport", reference)
        return None
    try:
        provider = builder(profile)
    except Exception:  # third-party flow code can raise anything
        _refuse("the flow claiming %r failed to build a provider", reference)
        return None
    if provider is None:
        _refuse("the flow claiming %r could not build a provider", reference)
        return None
    return provider


# ---------------------------------------------------------------------------
# Step 4 — the trust the operator configured
# ---------------------------------------------------------------------------


def _apply_trust(ca_bundle: str | None) -> str | None:
    """Put the operator's CA bundle in front of every request, or refuse.

    Validated before it is applied, and refused if it will not load. The
    trap is that LiteLLM does not refuse: measured on 1.98.0, a bundle
    path it cannot read falls back to its own certifi store, silently,
    with verification still on — so a typo'd path looks like a working
    profile right up until the corporate endpoint's certificate is
    rejected, or worse, until a certificate the operator never trusted is
    accepted. `build_verify` opens it first and names the path.

    Verification is never weakened here: the bundle *adds* the roots the
    operator chose, and the context LiteLLM builds from it still requires
    a certificate and still checks the hostname.

    Args:
        ca_bundle: Path from `network.ca_bundle`, or None for the SDK's
            own trust store.

    Returns:
        None when there is nothing to do or the trust was applied, or the
        refusal text.
    """
    if ca_bundle is None:
        return None
    try:
        net.build_verify(ca_bundle)
        litellm_runtime.apply_ca_bundle(ca_bundle)
    except ValueError as exc:
        return f"{exc}"
    return None


# ---------------------------------------------------------------------------
# Step 5 — keyless auth, against an endpoint the operator named
# ---------------------------------------------------------------------------


def _refuse_keyless_without_endpoint(profile: ModelConnectionConfig) -> str | None:
    """`none` auth is only meaningful against an endpoint the operator named.

    With no endpoint the request goes to whatever default host the SDK
    picks, and an unauthenticated request to a host the operator did not
    choose is a request to somebody else's service. With an endpoint it
    is their own gateway, proxy or local runtime, which is the entire
    reason `none` exists.

    Deliberately no provider dimension: it reads one field of the
    operator's own profile and nothing else. An earlier revision asked
    the routing call whether the provider had a default host; that value
    is a dynamic *override*, unset for exactly the hosted vendors this
    rule exists to protect, so the rule inverted on every reference that
    mattered.
    """
    if profile.auth.method != "none":
        return None
    if profile.endpoint and profile.endpoint.strip():
        return None
    return (
        "keyless auth ('none') requires an endpoint: set base_url on this "
        "profile, or choose an auth method that supplies a credential"
    )


# ---------------------------------------------------------------------------
# Step 6 — the credential, by exactly the method the profile names
# ---------------------------------------------------------------------------


def _resolve_credential(
    profile: ModelConnectionConfig, credentials: CredentialStore | None
) -> tuple[bool, ResolvedApiKey]:
    """Resolve `profile.auth` into a credential, or refuse.

    Five methods, each explicit, none falling back to another: an
    explicit variable name that is unset is a refusal, not a quiet switch
    to whichever ambient variable the SDK would have found.

    Returns:
        `(True, credential)`, or `(False, None)` with the reason logged.
        `provider-default` resolves to `OMIT_API_KEY` rather than `None`,
        because passing an explicit `api_key=None` stops the vendor SDK
        consulting its own credential chain — which is the only thing
        that method is for.
    """
    method = profile.auth.method
    if method == "none":
        return True, None
    if method == "environment":
        return _from_environment(profile.auth.settings)
    if method == "keyring":
        return _from_keyring(profile, credentials)
    if method == "provider-default":
        return True, OMIT_API_KEY
    if method == "device-login":
        _refuse(
            "auth method 'device-login' is only available for a reference a special "
            "flow claims, and nothing claims %r",
            profile.model,
        )
        return False, None
    _refuse("auth method %r is not one korvid knows", method)
    return False, None


def _named_setting(settings: Mapping[str, object]) -> str | None:
    """The `auth.key` name, or None when it is absent or blank."""
    value = settings.get(_KEY_SETTING)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _from_environment(settings: Mapping[str, object]) -> tuple[bool, ResolvedApiKey]:
    """Read exactly the named variable, and no other."""
    name = _named_setting(settings)
    if name is None:
        _refuse(
            "auth method 'environment' needs the name of the variable holding the key (auth.%s)",
            _KEY_SETTING,
        )
        return False, None
    value = os.environ.get(name)
    if not value:
        _refuse("auth method 'environment' names %s, which is not set", name)
        return False, None
    return True, value


def _from_keyring(
    profile: ModelConnectionConfig, credentials: CredentialStore | None
) -> tuple[bool, ResolvedApiKey]:
    """Read one keyring entry, named by the profile or by its reference.

    A lookup that raises disables the profile rather than propagating:
    an unavailable keyring backend is a configuration problem, not a
    reason for korvid to fail to start.
    """
    entry = _named_setting(profile.auth.settings) or profile.model
    store = credentials if credentials is not None else _KeyringStore()
    try:
        secret = store.load(entry)
    except Exception:  # any keyring backend, in any state
        _refuse("the keyring lookup for %r failed", entry)
        return False, None
    if not secret:
        _refuse("the keyring holds no entry named %r", entry)
        return False, None
    return True, secret


# ---------------------------------------------------------------------------
# Step 7 — routing, delegated
# ---------------------------------------------------------------------------


def _route(reference: str, endpoint: str | None) -> tuple[str, str] | None:
    """Resolve the reference through LiteLLM, or refuse.

    Safe by this point: every reference that makes the call dangerous was
    claimed or refused above. The reference is passed exactly as the
    operator wrote it, with **no** provider hint — the resolution korvid
    gets here is the same one every later call will get, so a reference
    that builds is a reference that can be dispatched.

    Supplying the written prefix as a hint would make this call succeed
    for any prefix at all. Measured on 1.98.0, that is validation
    bypassed rather than a private provider supported: the same hint at
    call time raises `Unmapped LLM provider for this endpoint`, and
    without it the call raises `LLM Provider NOT provided`. Either way
    the profile can never complete a request, so accepting it here only
    moves the failure from startup — where it names the field — to the
    first message the operator sends.

    The returned host is deliberately ignored — no refusal reads it.
    Routing is here to validate the reference and to name the provider
    for the parameter lookup, and for nothing else.

    Returns:
        `(provider id, model tag)`, or None with the reason logged.
    """
    prefix, tag = split_reference(reference)
    try:
        routed = litellm_runtime.get_llm_provider(model=reference, api_base=endpoint)
    except Exception:  # the SDK raises its own errors for a bad reference
        _refuse(
            "litellm cannot dispatch the model reference %r: set `model` on this "
            "profile to a reference litellm resolves, or install a flow that "
            "claims this prefix",
            reference,
        )
        return None
    provider_id = str(routed[1]) if len(routed) > 1 and routed[1] else prefix
    if not provider_id:
        _refuse("litellm resolved no provider for the model reference %r", reference)
        return None
    return provider_id, tag or reference
    provider_id = str(routed[1]) if len(routed) > 1 and routed[1] else prefix
    if not provider_id:
        _refuse("litellm resolved no provider for the model reference %r", reference)
        return None
    return provider_id, tag or reference


# ---------------------------------------------------------------------------
# Step 8 — a provider with nowhere to send the request
# ---------------------------------------------------------------------------


def _refuse_unreachable_provider(
    reference: str, endpoint: str | None, api_key: ResolvedApiKey
) -> str | None:
    """Refuse a provider that has no host, and no endpoint to borrow one from.

    A build-time refusal rather than a setup-time hint, because this is
    the point at which the consequence exists: LiteLLM has resolved the
    reference and says it still needs a base URL, and the profile names
    none. The credential is passed to the probe only so a missing key
    cannot be mistaken for a missing host; it is never logged.
    """
    if endpoint is not None:
        return None
    resolved = api_key if isinstance(api_key, str) else None
    if not litellm_runtime.requires_explicit_api_base(reference, api_key=resolved, api_base=None):
        return None
    return f"{reference!r} cannot be reached without an endpoint: set base_url on this profile"


# ---------------------------------------------------------------------------
# Capabilities — from the catalog and the operator, never from the name
# ---------------------------------------------------------------------------


def _catalog_entry(catalog: ModelCatalog | None, reference: str) -> ModelEntry | None:
    """The catalog's record, or None. A catalog that raises is not evidence."""
    if catalog is None:
        return None
    try:
        return catalog.entry(reference)
    except Exception:  # a catalog is an index, not a dependency
        logger.warning(
            "the model catalog failed to answer for %r; capabilities stay unknown", reference
        )
        return None


def _capabilities(
    reference: str, options: Mapping[str, object], catalog: ModelCatalog | None
) -> ModelCapabilities:
    """Translate catalog facts and operator options into capabilities.

    Starts from `unknown()` and fills only what a source directly
    asserts, recording per-fact provenance. Nothing is ever inferred from
    the reference: a model tag reading `gpt-4o-with-tools-2000k` proves
    nothing.
    """
    facts: dict[str, Any] = {}
    provenance: dict[str, CapabilitySource] = {}

    entry = _catalog_entry(catalog, reference)
    if entry is not None:
        for fact in _CATALOG_FACTS:
            value = getattr(entry, fact, None)
            if value is not None:
                facts[fact] = value
                provenance[fact] = CapabilitySource.CATALOG

    for option, fact in _OPTION_FACTS:
        value = options.get(option)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            facts[fact] = value
            provenance[fact] = CapabilitySource.USER

    return ModelCapabilities(provenance=provenance, **facts)
