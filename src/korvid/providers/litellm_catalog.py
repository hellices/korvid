"""LiteLLM-backed model catalog — the primary, air-gapped layer.

All data comes from tables shipped inside the `litellm` wheel. No network
call is needed: `providers/_litellm_import.py` ensures `LITELLM_LOCAL_MODEL_COST_MAP`
is set before `import litellm`, so `model_cost` is always the bundled copy.
"""

from __future__ import annotations

import functools
import re
from typing import Any, Final

from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    EndpointRequirement,
    ModelCatalog,
    ModelConnectionConfig,
    ModelEntry,
    ModelEntrySource,
    SetupField,
    SetupFieldKind,
    SpecialFlowRegistry,
    split_reference,
)
from korvid.providers.litellm_runtime import (
    model_cost_entry,
    models_by_provider,
    supported_params,
)

#: LiteLLM's own spelling for the Copilot provider. Its ids ship
#: already-qualified (`github_copilot/claude-haiku-4.5`), and resolving the
#: prefix starts an interactive device login *inside* the routing call, so
#: the entries are re-prefixed onto korvid's own claimed spelling rather
#: than offered as LiteLLM writes them.
_LITELLM_COPILOT_PROVIDER: Final = "github_copilot"
_KORVID_COPILOT_PREFIX: Final = "github-copilot"

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
        help_text="Provider API version string (e.g. 2024-02-01 for Azure).",
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


class LiteLLMModelCatalog(ModelCatalog):
    """`ModelCatalog` over LiteLLM's shipped tables.

    Args:
        flows: The special-flow registry (Task 8). Empty is valid and
            fully functional.
        enrichment: An optional metadata source (Task 7). `None` means
            "offline only", which is the air-gapped default.
        discovery: The bounded endpoint prober. Injected so the catalog
            stays testable without a network. Task 8 fills this in.
    """

    def __init__(
        self,
        *,
        flows: SpecialFlowRegistry | None = None,
        enrichment: Any | None = None,
        discovery: Any | None = None,
    ) -> None:
        self._flows = flows
        self._enrichment = enrichment
        self._discovery = discovery

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
                if provider == _LITELLM_COPILOT_PROVIDER:
                    if (
                        self._flows is None
                        or self._flows.claim(f"{_KORVID_COPILOT_PREFIX}/") is None
                    ):
                        # No flow owns Copilot in this installation, so there
                        # is nothing safe to route these to. Drop them rather
                        # than offer a reference whose resolution blocks on a
                        # device-login poll.
                        continue
                    _, tag = split_reference(reference)
                    reference = f"{_KORVID_COPILOT_PREFIX}/{tag}"
                    entry_provider = _KORVID_COPILOT_PREFIX
                entries.append(self._entry_from(entry_provider, reference, record))
        return tuple(entries)

    def _entry_from(
        self,
        provider: str,
        reference: str,
        record: dict[str, Any] | None,
    ) -> ModelEntry:
        return ModelEntry(
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

    def _env_hints(self, provider: str) -> tuple[str, ...]:
        """Return credential env-var hints from the enrichment source.

        Returns `()` when no enrichment source is injected. Never reads
        `os.environ` — a hint names a variable the operator should set,
        it does not read one.
        """
        if self._enrichment is None:
            return ()
        # Task 7 wires the enrichment source; for now the interface is
        # not yet defined, so we return () regardless.
        return ()  # pragma: no cover - Task 7 fills this in

    # ------------------------------------------------------------------
    # ModelCatalog interface
    # ------------------------------------------------------------------

    def search(self, query: str, *, limit: int = 50) -> tuple[ModelEntry, ...]:
        """Rank catalog entries against a free-text query. Never raises."""
        q = query.strip().lower()
        if not _ALNUM_RE.search(q):
            return ()
        results: list[ModelEntry] = []
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
        return tuple(results[:limit])

    def entry(self, reference: str) -> ModelEntry | None:
        """The catalog's record for an exact reference, or None."""
        return self._by_reference.get(reference)

    def auth_methods(
        self, reference: str, *, endpoint: str | None = None
    ) -> tuple[AuthMethodDescriptor, ...]:
        """Auth methods valid for this reference, most specific first.

        `none` (keyless) is offered only when `endpoint` is a non-empty
        string — the single expression that mirrors the factory's own
        check exactly (Task 15). No provider dimension is consulted here.
        """
        methods = list(_GENERIC_AUTH_METHODS)
        if endpoint:
            methods.append(_NONE_AUTH_METHOD)
        return tuple(methods)

    def option_fields(self, reference: str) -> tuple[SetupField, ...]:
        """Declarative option prompts for this reference."""
        provider, model_tag = split_reference(reference)
        params = supported_params(model_tag, provider)
        fields: list[SetupField] = []
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

        Stub — Task 8 fills this in. Returns `()` when no discovery
        prober is injected or the profile has no endpoint.
        """
        if self._discovery is None or not profile.endpoint:
            return ()
        return ()  # pragma: no cover - Task 8 fills this in

    async def test(self, profile: ModelConnectionConfig) -> str:
        """Probe the profile and return a short human-readable result.

        Stub — Task 8 fills this in.
        """
        raise NotImplementedError("test() is implemented in Task 8")

    async def begin_auth(self, profile: ModelConnectionConfig) -> None:
        """Begin a device-login flow for this profile.

        Stub — Task 8 fills this in.
        """
        return None  # pragma: no cover - Task 8 fills this in

    async def finish_auth(self, profile: ModelConnectionConfig) -> None:
        """Complete a device-login flow for this profile.

        Stub — Task 8 fills this in.
        """
        return None  # pragma: no cover - Task 8 fills this in
