"""LiteLLM-backed model catalog — the primary, air-gapped layer.

All data comes from tables shipped inside the `litellm` wheel. No network
call is needed: `providers/_litellm_import.py` ensures `LITELLM_LOCAL_MODEL_COST_MAP`
is set before `import litellm`, so `model_cost` is always the bundled copy.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Final

from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    DeviceLoginPrompt,
    EndpointRequirement,
    MetadataRefresh,
    ModelCatalog,
    ModelConnectionConfig,
    ModelEntry,
    ModelEntrySource,
    SetupField,
    SetupFieldKind,
    SpecialFlow,
    split_reference,
)
from korvid.providers.endpoint_discovery import EndpointDiscovery
from korvid.providers.litellm_runtime import (
    model_cost_entry,
    models_by_provider,
    supported_params,
)
from korvid.providers.litellm_settings import DEVICE_LOGIN_PREFIXES
from korvid.providers.models_dev import ModelMetadataSource, RefreshOutcome
from korvid.providers.special_flows import SpecialFlowRegistry, normalize_prefix

#: The one place a provider-layer refresh outcome becomes an operator-facing
#: one. A `dict` rather than same-named members so the two vocabularies stay
#: free to diverge: `NOT_MODIFIED` is HTTP's word for it, and the UI says
#: "already up to date".
_REFRESH_OUTCOMES: Final[dict[RefreshOutcome, MetadataRefresh]] = {
    RefreshOutcome.UPDATED: MetadataRefresh.UPDATED,
    RefreshOutcome.NOT_MODIFIED: MetadataRefresh.UNCHANGED,
    RefreshOutcome.CACHED: MetadataRefresh.CACHED,
    RefreshOutcome.UNAVAILABLE: MetadataRefresh.UNAVAILABLE,
}

#: The prefixes whose *resolution* is an interactive login, in korvid's
#: normalized spelling. LiteLLM publishes their ids already qualified
#: (`github_copilot/claude-haiku-4.5`, `chatgpt/gpt-5.2`), so an entry is
#: either re-prefixed onto the spelling korvid claims — which is what the
#: flow serving it answers to — or dropped when nothing serves it. Read
#: from the deny-list rather than restated, so the picker and the factory
#: cannot end up disagreeing about which references are safe.
_DEVICE_LOGIN_PREFIXES: Final[frozenset[str]] = frozenset(
    normalize_prefix(prefix) for prefix in DEVICE_LOGIN_PREFIXES
)

# ---------------------------------------------------------------------------
# Static auth-method descriptors
# ---------------------------------------------------------------------------

_ENV_KEY_FIELD: Final = SetupField(
    key="key",
    label="Environment variable name",
    kind=SetupFieldKind.SECRET_REF,
    required=True,
    help_text="Name of the environment variable holding the API key.",
)

_ENVIRONMENT_AUTH: Final = AuthMethodDescriptor(
    id="environment",
    display_name="Environment variable",
    fields=(_ENV_KEY_FIELD,),
)

_KEYRING_AUTH: Final = AuthMethodDescriptor(
    id="keyring",
    display_name="System keyring",
    fields=(),
)

_PROVIDER_DEFAULT_AUTH: Final = AuthMethodDescriptor(
    id="provider-default",
    display_name="Provider default (SDK credential chain)",
    fields=(),
)

_NONE_AUTH_METHOD: Final = AuthMethodDescriptor(
    id="none",
    display_name="No authentication (keyless endpoint)",
    fields=(),
)

#: Generic methods offered for every reference, before the endpoint check.
#: `none` is conditionally appended in `auth_methods`; the rule is
#: expressed there in a single `if endpoint:` that mirrors the factory's
#: own check exactly.
_GENERIC_AUTH_METHODS: Final[tuple[AuthMethodDescriptor, ...]] = (
    _ENVIRONMENT_AUTH,
    _KEYRING_AUTH,
    _PROVIDER_DEFAULT_AUTH,
)

# ---------------------------------------------------------------------------
# Option-field helpers
# ---------------------------------------------------------------------------

_NUMERIC_PARAMS: Final[tuple[str, ...]] = ("temperature", "max_tokens", "seed", "timeout")

_PARAM_FIELDS: Final[dict[str, SetupField]] = {
    "temperature": SetupField(
        key="temperature",
        label="Temperature",
        kind=SetupFieldKind.TEXT,
        help_text="Sampling temperature (0.0 - 2.0). Leave blank for the provider default.",
    ),
    "max_tokens": SetupField(
        key="max_tokens",
        label="Max tokens",
        kind=SetupFieldKind.INTEGER,
        help_text="Maximum tokens in the response.",
    ),
    "seed": SetupField(
        key="seed",
        label="Random seed",
        kind=SetupFieldKind.INTEGER,
        help_text="Seed for deterministic outputs.",
    ),
    "timeout": SetupField(
        key="timeout",
        label="Timeout (seconds)",
        kind=SetupFieldKind.INTEGER,
        help_text="Request timeout in seconds.",
    ),
    "api_version": SetupField(
        key="api_version",
        label="API version",
        kind=SetupFieldKind.TEXT,
        help_text="Provider API version string, if the endpoint requires one (e.g. 2024-02-01).",
    ),
}

_VERSIONED_PARAM: Final = "api_version"


def _strict_bool(value: object) -> bool | None:
    """Return *value* only when it is exactly `True` or `False`.

    `None`, missing keys and non-bool truthy values all become `None` here.
    "The table has no opinion" must stay distinguishable from "the table
    says no".
    """
    if value is True or value is False:
        return value
    return None


def _positive_int(value: object) -> int | None:
    """Return *value* only when it is a positive integer.

    Rejects `bool` (an `int` subclass) and non-positive values so a zero
    context window stored in error does not surface as "0 tokens".
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


_ALNUM_RE: Final = re.compile(r"[a-z0-9]")

#: An async probe of one profile, returning a short human-readable result.
#: The catalog owns no transport, so whoever does is injected here.
ProfileTester = Callable[[ModelConnectionConfig], Awaitable[str]]


class ProfileTestUnavailable(RuntimeError):
    """No transport was injected, so this catalog cannot probe a profile.

    Raised rather than returning a cheerful string: a wizard that reports
    a working connection it never made is worse than one that says it
    could not check.
    """


class LiteLLMModelCatalog(ModelCatalog):
    """`ModelCatalog` over LiteLLM's shipped tables.

    Args:
        flows: The special-flow registry (Task 8). Empty is valid and
            fully functional.
        enrichment: An optional metadata source (Task 7). `None` means
            "offline only", which is the air-gapped default.
        discovery: The bounded endpoint prober (Task 8). Injected so the
            catalog stays testable without a network; `None` means this
            installation cannot list an endpoint's models.
        tester: Probes one profile and returns what to show the operator.
            Injected because the catalog is a data index — it holds no
            client, no credential and no transport of its own. `None`
            means this installation cannot test a connection, and `test`
            says so instead of pretending.
    """

    def __init__(
        self,
        *,
        flows: SpecialFlowRegistry | None = None,
        enrichment: ModelMetadataSource | None = None,
        discovery: EndpointDiscovery | None = None,
        tester: ProfileTester | None = None,
    ) -> None:
        self._flows = flows
        self._enrichment = enrichment
        self._discovery = discovery
        self._tester = tester

    # ------------------------------------------------------------------
    # Index construction (lazy, built once per instance)
    # ------------------------------------------------------------------

    @functools.cached_property
    def _index(self) -> tuple[ModelEntry, ...]:
        return self._build_index()

    @functools.cached_property
    def _by_reference(self) -> dict[str, ModelEntry]:
        return {e.reference: e for e in self._index}

    def _build_index(self) -> tuple[ModelEntry, ...]:
        entries: list[ModelEntry] = []
        for provider, models in models_by_provider().items():
            for model_id in models:
                if model_id == "sample_spec":
                    continue
                record = model_cost_entry(provider, model_id)
                mode = record.get("mode") if record else None
                if mode is not None and mode != "chat":
                    # Image, embedding, rerank and audio entries share the
                    # table. Offering them as chat models would be a lie.
                    continue
                reference = (
                    model_id if model_id.startswith(f"{provider}/") else f"{provider}/{model_id}"
                )
                entry_provider = provider
                claimed = normalize_prefix(provider)
                if claimed in _DEVICE_LOGIN_PREFIXES:
                    if self._flows is None or self._flows.claim(f"{claimed}/") is None:
                        # Nothing owns this prefix in this installation, so
                        # there is nothing safe to route these to. Drop them
                        # rather than offer a reference whose resolution
                        # blocks on a device-login poll.
                        continue
                    _, tag = split_reference(reference)
                    reference = f"{claimed}/{tag}"
                    entry_provider = claimed
                entries.append(self._entry_from(entry_provider, reference, record))
        return tuple(entries)

    def _entry_from(
        self,
        provider: str,
        reference: str,
        record: dict[str, Any] | None,
    ) -> ModelEntry:
        entry = ModelEntry(
            reference=reference,
            provider_id=provider,
            display_name=split_reference(reference)[1],
            context_window_tokens=_positive_int(record.get("max_input_tokens")) if record else None,
            max_output_tokens=_positive_int(record.get("max_output_tokens")) if record else None,
            supports_tools=_strict_bool(record.get("supports_function_calling"))
            if record
            else None,
            supports_reasoning=_strict_bool(record.get("supports_reasoning")) if record else None,
            source=ModelEntrySource.LITELLM,
            credential_env_hints=self._env_hints(provider),
        )
        return self._overlay(entry)

    def _overlay(self, entry: ModelEntry) -> ModelEntry:
        """Overlay models.dev enrichment onto a LiteLLM entry.

        LiteLLM wins routing-relevant capability conflicts. models.dev may
        replace the bare model-id display label with its human-readable name.
        `source` is re-labelled `MODELS_DEV` only when the overlay actually
        contributed a new fact — restating known data must not claim credit,
        because the UI's provenance line would be false.
        """
        if self._enrichment is None:
            return entry
        extra = self._enrichment.metadata(entry.reference)
        if extra is None:
            return entry
        enriched = replace(
            entry,
            display_name=extra.display_name or entry.display_name,
            context_window_tokens=entry.context_window_tokens or extra.context_window_tokens,
            max_output_tokens=entry.max_output_tokens or extra.max_output_tokens,
            supports_tools=(
                entry.supports_tools if entry.supports_tools is not None else extra.supports_tools
            ),
            supports_reasoning=(
                entry.supports_reasoning
                if entry.supports_reasoning is not None
                else extra.supports_reasoning
            ),
            credential_env_hints=entry.credential_env_hints or extra.credential_env_hints,
        )
        if enriched == entry:
            # models.dev only restated what LiteLLM already knew.
            # Re-labelling provenance here would credit a source that
            # contributed nothing, and the UI's "where did this come
            # from" line would be false.
            return entry
        return replace(enriched, source=ModelEntrySource.MODELS_DEV)

    def _env_hints(self, provider: str) -> tuple[str, ...]:
        """Return credential env-var hints from the enrichment source.

        Returns `()` when no enrichment source is injected. Never reads
        `os.environ` — a hint names a variable the operator should set,
        it does not read one.
        """
        if self._enrichment is None:
            return ()
        return self._enrichment.env_hints(provider)

    def _invalidate_index(self) -> None:
        """Drop the memoised index so the next read rebuilds the overlay.

        `_index` and `_by_reference` are `cached_property` values built
        from the enrichment source as it was at first read. Without this,
        a refresh that genuinely updated the source would report success
        and change nothing the operator can see until korvid restarts.
        """
        self.__dict__.pop("_index", None)
        self.__dict__.pop("_by_reference", None)

    # ------------------------------------------------------------------
    # ModelCatalog interface
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        extra: tuple[ModelEntry, ...] = (),
    ) -> tuple[ModelEntry, ...]:
        """Rank catalog entries against a free-text query. Never raises.

        `extra` accepts caller-supplied entries (manual references and live
        endpoint results) that are merged ahead of the ranked catalog hits.
        Deduplication is by `reference`, with catalog entries winning over
        extras when both contain the same reference.
        """
        q = query.strip().lower()
        if not _ALNUM_RE.search(q):
            return ()
        results: list[ModelEntry] = []
        # Catalog entries ranked first
        for entry in self._index:
            ref = entry.reference.lower()
            if q in ref:
                results.append(entry)
        # Deterministic ranking: exact ref > tag prefix > tag substring > tag anywhere > alphabetical
        results.sort(
            key=lambda e: (
                e.reference.lower() != q,
                not split_reference(e.reference.lower())[1].startswith(q),
                q not in split_reference(e.reference.lower())[1],
                e.reference.lower(),
            )
        )
        # Merge extras, deduplicating — catalog wins on conflict
        catalog_refs = {e.reference for e in results}
        merged: list[ModelEntry] = list(results)
        for e in extra:
            if e.reference not in catalog_refs and q in e.reference.lower():
                merged.append(e)
        return tuple(merged[:limit])

    def entry(self, reference: str) -> ModelEntry | None:
        """The catalog's record for an exact reference, or None."""
        return self._by_reference.get(reference)

    def auth_methods(
        self, reference: str, *, endpoint: str | None = None
    ) -> tuple[AuthMethodDescriptor, ...]:
        """Auth methods valid for this reference, most specific first.

        When a special flow claims the reference, its declared auth methods
        replace the generic list. `none` (keyless) is offered only when
        `endpoint` is a non-empty string — a flow cannot override this rule.
        """
        flow = self._flows.claim(reference) if self._flows else None
        if flow is not None and flow.auth_methods:
            methods = [m for m in flow.auth_methods if m.id != "none" or bool(endpoint)]
            return tuple(methods)
        methods = list(_GENERIC_AUTH_METHODS)
        if endpoint:
            methods.append(_NONE_AUTH_METHOD)
        return tuple(methods)

    def option_fields(self, reference: str) -> tuple[SetupField, ...]:
        """Declarative option prompts for this reference.

        When a special flow claims the reference, its option_fields are
        returned ahead of the LiteLLM-derived numeric/versioned params.
        """
        flow = self._flows.claim(reference) if self._flows else None
        flow_fields: tuple[SetupField, ...] = flow.option_fields if flow is not None else ()

        provider, model_tag = split_reference(reference)
        params = supported_params(model_tag, provider)
        fields: list[SetupField] = list(flow_fields)
        for param in _NUMERIC_PARAMS:
            if param in params:
                field = _PARAM_FIELDS.get(param)
                if field is not None:
                    fields.append(field)
        if _VERSIONED_PARAM in params:
            field = _PARAM_FIELDS.get(_VERSIONED_PARAM)
            if field is not None:
                fields.append(field)
        return tuple(fields)

    def endpoint_requirement(self, reference: str) -> EndpointRequirement:
        """Whether the setup UI must, may, or must not ask for an endpoint.

        Answered from the special-flow registry alone. Everything else is
        OPTIONAL — LiteLLM's model_cost records carry no host field, so
        no data exists from which "this provider needs an endpoint" could
        be derived. A flow that declares REQUIRED or UNSUPPORTED is the
        only source of a non-OPTIONAL answer.
        """
        flow = self._flows.claim(reference) if self._flows else None
        if flow is not None:
            return flow.endpoint
        return EndpointRequirement.OPTIONAL

    def manual_entry(self, reference: str) -> ModelEntry:
        """Construct a stub entry for an operator-supplied reference.

        On the concrete class rather than the ABC: "the operator typed
        something" is not a question the UI asks the catalog to *answer*.
        """
        provider, _ = split_reference(reference)
        return ModelEntry(
            reference=reference,
            provider_id=provider,
            display_name=split_reference(reference)[1],
            source=ModelEntrySource.MANUAL,
        )

    async def discover(self, profile: ModelConnectionConfig) -> tuple[ModelEntry, ...]:
        """Live-list models from the profile's endpoint.

        Every discovered entry is labelled with the profile's own provider
        prefix, because that is the only prefix korvid knows the operator
        chose. A reference with no prefix therefore discovers nothing:
        inventing one would write a provider the operator never named into
        every entry offered, pointing the resulting reference at a vendor
        that may have nothing to do with the endpoint.

        Returns `()` when no discovery prober is injected, when the profile
        has a config error, when the profile has no endpoint, or when its
        reference names no provider.
        """
        if self._discovery is None:
            return ()
        if profile.config_error is not None:
            return ()
        if not profile.endpoint:
            return ()
        prefix = split_reference(profile.model)[0]
        if not prefix:
            return ()

        # Resolve the key from the profile's explicit auth config, without
        # falling back to the ambient environment.
        api_key: str | None = None
        if profile.auth.method == "environment":
            key_name = str(profile.auth.settings.get("key", ""))
            if key_name:
                import os  # lazy import — os is stdlib, not a dep

                api_key = os.environ.get(key_name)

        return await self._discovery.list_models(
            base_url=profile.endpoint,
            api_key=api_key,
            prefix=prefix,
        )

    async def refresh_metadata(self, *, force: bool = False) -> MetadataRefresh:
        """Revalidate the enrichment source, because a human asked.

        The only caller is the setup UI's explicit action. Nothing here
        runs at startup, on a search, or on a routing call — the source's
        own contract forbids it, and this is the single path that could
        break that promise.

        A source that updated invalidates the memoised index, so the very
        next search shows what was fetched. Every failure the source can
        report arrives as an outcome, never an exception: this runs in a
        UI worker, and a raise there would tear down a live screen.

        Args:
            force: Carried straight through to the source. The catalog has
                no opinion about freshness windows — it owns the
                vocabulary, not the caching — but dropping the flag here
                would leave the operator's keypress meaning "read the
                cache" for as long as that window lasts.
        """
        source = self._enrichment
        if source is None:
            # Deliberately not an error: `agent.model_search.models_dev:
            # false` and a base install both land here, and both are
            # working configurations. Forcing cannot conjure a source an
            # installation deliberately does not have.
            return MetadataRefresh.DISABLED
        outcome = await source.refresh(force=force)
        if outcome is RefreshOutcome.UPDATED:
            self._invalidate_index()
        return _REFRESH_OUTCOMES.get(outcome, MetadataRefresh.UNAVAILABLE)

    async def test(self, profile: ModelConnectionConfig) -> str:
        """Probe the profile and return a short human-readable result.

        Delegates to the injected tester. Nothing is normalised on the way
        through: a refusal is the answer the wizard renders, and swallowing
        it would report a profile that cannot connect as working.

        Raises:
            ProfileTestUnavailable: When no tester was injected.
        """
        tester = self._tester
        if tester is None:
            raise ProfileTestUnavailable(
                "this installation cannot test a model connection — no transport is wired"
            )
        return await tester(profile)

    async def begin_auth(self, profile: ModelConnectionConfig) -> DeviceLoginPrompt | None:
        """Start the claiming flow's own sign-in, if it declares one.

        The catalog owns no vendor knowledge and no transport: a sign-in
        belongs to the flow that declared it. `None` means the wizard
        skips the stage — every ordinary API-key profile lands there, so
        it must not look like a failure.

        Args:
            profile: The connection being set up.

        Returns:
            What the operator has to act on, or None when this profile
            needs no interactive sign-in.
        """
        flow = self._claiming_flow(profile)
        if flow is None or flow.begin_auth is None:
            return None
        return await flow.begin_auth(profile)

    async def finish_auth(self, profile: ModelConnectionConfig) -> str | None:
        """Complete the claiming flow's sign-in and name the credential.

        Args:
            profile: The connection being set up.

        Returns:
            The credential key the profile will reference — never the
            secret itself — or None when there was no sign-in to finish.
        """
        flow = self._claiming_flow(profile)
        if flow is None or flow.finish_auth is None:
            return None
        return await flow.finish_auth(profile)

    def _claiming_flow(self, profile: ModelConnectionConfig) -> SpecialFlow | None:
        """The flow this profile's reference resolves to, by prefix or option.

        Mirrors `litellm_factory._claim`, and has to: the two read the
        same registry about the same profile, and a disagreement means
        the wizard signs an operator in through a flow the factory will
        then decline to build with.

        A flow that declares `claims_option` *shares* its prefix rather
        than owning it. The bare `claim()` still resolves it — the wizard
        needs that to render the option's own fields — but answering the
        *sign-in* there would start a login for every ordinary reference
        under the prefix, including the ones that never turned the option
        on. Only `claim_by_option` may select such a flow, and only when
        the option is strictly `True`.
        """
        if self._flows is None:
            return None
        flow = self._flows.claim(profile.model)
        if flow is not None and flow.claims_option is None:
            return flow
        return self._flows.claim_by_option(profile.model, profile.options)
