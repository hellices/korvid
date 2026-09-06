"""Frozen request plan: profile → LiteLLM kwargs, built once per call.

Building the call once means the outbound snapshot and the wire payload are
the same object rather than two constructions that can drift.

Parameter names are taken from ``acompletion``'s real signature in 1.98.0:
``base_url``, ``api_version`` and ``timeout`` are named parameters; ``api_base``
is only reachable through ``**kwargs``, so korvid uses the named ones.

``api_key`` is tri-state:

- A resolved credential string is passed verbatim.
- ``None`` (genuinely keyless private endpoint) causes ``KEYLESS_API_KEY_SENTINEL``
  to be sent so the SDK's own ``OPENAI_API_KEY`` lookup can never smuggle an
  unrelated ambient key onto the wire.
- ``OMIT_API_KEY`` means the argument is absent entirely; the vendor SDK's own
  credential chain is consulted (``provider-default`` auth).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from korvid.providers.litellm_settings import KEYLESS_API_KEY_SENTINEL

# ---------------------------------------------------------------------------
# Korvid-owned option keys — transport selectors, not model parameters.
# ---------------------------------------------------------------------------
#: The trust bundle, as a profile option. A flow owns its own transport,
#: so this is how the operator's one trust decision reaches it — never a
#: model parameter, and never something the wire sees.
CA_BUNDLE_OPTION: Final[str] = "ca_bundle"

_KORVID_OWNED_OPTIONS: frozenset[str] = frozenset(
    {"native_thinking", CA_BUNDLE_OPTION, "num_ctx_source", "ssl_verify"}
)

#: Options that are named ``acompletion`` parameters rather than model
#: parameters. They are lifted onto the plan and must never be left in the
#: extras, where the per-provider allowlist would decide their fate.
_LIFTED: frozenset[str] = frozenset({"api_version", "timeout"})


# ---------------------------------------------------------------------------
# The frozen-config boundary
# ---------------------------------------------------------------------------


def _materialize(value: object) -> object:
    """One frozen config value as an independent, plain, mutable structure.

    `load_config` copy-owns every parsed value: mappings become
    `MappingProxyType` and sequences become tuples, recursively. Neither
    shape can cross this boundary as it is.

    * `copy.deepcopy` **raises** on a `mappingproxy` — it has no copier, so
      it falls through to `__reduce_ex__` and fails with `TypeError: cannot
      pickle 'mappingproxy' object`. Any profile with a nested option
      mapping (`extra_headers` is the common one) took the whole request
      down with it.
    * A tuple is not the shape the SDK expects. LiteLLM's per-provider
      transforms index and extend the sequences they are handed, and the
      outbound snapshot records plain JSON, so a tuple would either raise
      or serialize as something the operator did not write.

    Mappings become plain `dict`, `list`/`tuple` become plain `list`, and
    everything else is returned as-is: the remaining config value types are
    JSON scalars, which are immutable and cannot alias.
    """
    if isinstance(value, Mapping):
        return {str(key): _materialize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_materialize(item) for item in value]
    return value


def materialize_options(options: Mapping[str, object]) -> dict[str, object]:
    """A plain, mutable, independently-owned copy of *options*.

    The provider layer's replacement for `copy.deepcopy` at the boundary
    between frozen configuration and the SDK. See `_materialize` for why a
    deep copy is both impossible and wrong here.
    """
    return {str(key): _materialize(item) for key, item in options.items()}


def _positive_seconds(value: object) -> float | None:
    """A duration in seconds, or None when the value cannot be one.

    Deliberately strict. ``bool`` is excluded because ``True`` is an ``int``
    and a one-second timeout is never what an operator meant by it, and a
    numeric *string* is rejected rather than coerced: profile options are
    typed YAML, and quietly parsing text here would make ``"nan"`` a
    plausible input. Anything unusable falls back to the SDK default, which
    is the honest answer for a value korvid cannot act on.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


# ---------------------------------------------------------------------------
# Tri-state API-key sentinel
# ---------------------------------------------------------------------------


class _OmitApiKey:
    """Sentinel: pass no ``api_key`` argument at all.

    Distinct from ``None``, and it has to be. ``provider-default`` means "let
    the vendor SDK use its own environment/default credential chain".
    Passing ``api_key=None`` does not do that — the SDK sees an explicit
    argument and stops consulting its chain — and passing the keyless sentinel
    string would send a literal bogus credential. The only behaviour that
    delegates is the argument being *absent* from the call, so the plan needs
    a third state that ``call_kwargs`` can act on.
    """

    def __repr__(self) -> str:
        return "OMIT_API_KEY"


#: The "do not pass ``api_key``" marker. Compared with ``is``.
OMIT_API_KEY: Final = _OmitApiKey()

#: No declared credential chain — the ordinary case. Immutable, because a
#: shared mutable default would let one plan's credential leak into every
#: other plan built without one.
_NO_CREDENTIAL: Final[Mapping[str, object]] = MappingProxyType({})

#: A credential in its three resolved states: the key itself, ``None`` for
#: a genuinely keyless endpoint, or ``OMIT_API_KEY`` to pass no argument at
#: all. Named so the factory can carry a resolution result without
#: importing the private sentinel class.
ResolvedApiKey = str | _OmitApiKey | None


# ---------------------------------------------------------------------------
# RequestPlan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestPlan:
    """A fully resolved LiteLLM call, as data.

    Built once per request so the outbound snapshot and the wire payload are
    the same object rather than two constructions that can drift.
    """

    model: str
    #: Tri-state. A ``str`` is the resolved credential; ``None`` means "no
    #: credential was resolved" (a keyless endpoint, which still needs the
    #: sentinel on the wire because OpenAI-shaped clients refuse to build
    #: without one); ``OMIT_API_KEY`` means "pass nothing".
    api_key: ResolvedApiKey
    base_url: str | None
    api_version: str | None
    extra: Mapping[str, object]
    #: Seconds the SDK waits for the whole request, or ``None`` for its own
    #: default. Named rather than an extra because ``get_supported_openai_params``
    #: lists it for no provider, so the allowlist filter would drop it.
    timeout: float | None = None
    #: Transport call parameters contributed by a declared ``provider-default``
    #: credential chain. Named rather than left among the extras for the same
    #: reason ``timeout`` is: ``get_supported_openai_params`` lists none of
    #: them, so the allowlist filter would drop exactly the parameter that
    #: carries the credential. They hold a *refreshing* callable, never a
    #: resolved secret, and they are applied last so no profile option can
    #: replace one.
    credential: Mapping[str, object] = _NO_CREDENTIAL

    def call_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """Assemble the kwargs dict to pass to ``litellm.acompletion``."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
        }
        if self.api_key is not OMIT_API_KEY:
            # Absent, not None: ``provider-default`` delegates to the vendor
            # SDK's own credential chain, and an explicit argument — even None
            # — stops that chain being consulted. Every other method passes the
            # resolved key, or the keyless sentinel when the profile genuinely
            # has none, so the SDK's OPENAI_API_KEY lookup can never smuggle an
            # unrelated ambient key onto the wire.
            kwargs["api_key"] = self.api_key or KEYLESS_API_KEY_SENTINEL
        if tools:
            kwargs["tools"] = tools
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.api_version:
            kwargs["api_version"] = self.api_version
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        if stream:
            kwargs["stream_options"] = {"include_usage": True}
        kwargs.update(materialize_options(self.extra))
        # Last, and neither copied nor materialized: a declared credential
        # parameter is a live callable the transport invokes per request,
        # and an operator option must never be able to replace it.
        kwargs.update(self.credential)
        return kwargs


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def build_plan(
    *,
    model: str,
    api_key: ResolvedApiKey,
    base_url: str | None,
    options: Mapping[str, object],
    supported: Sequence[str],
    credential: Mapping[str, object] = _NO_CREDENTIAL,
) -> RequestPlan:
    """Resolve config into a plan, dropping options the provider rejects.

    Args:
        model: LiteLLM model string (e.g. ``"openai/gpt-4o"``).
        api_key: Resolved credential, ``None`` (keyless), or ``OMIT_API_KEY``.
        base_url: Override base URL, or ``None`` to use the provider default.
        options: Raw operator options from the profile. ``api_version`` and
            ``timeout`` are lifted onto the plan's named parameters; the rest
            are filtered against *supported*.
        supported: Parameter names the provider accepts. An *empty* sequence
            means the capability lookup failed; in that case all non-owned keys
            are forwarded rather than silently dropped.
        credential: Call parameters from a declared ``provider-default``
            credential chain. Never filtered against *supported*, which lists
            none of them, and never overridden by an option.

    Returns:
        A frozen ``RequestPlan`` ready for snapshotting and wiring.
    """
    # 1. Lift api_version and timeout before filtering: both are named
    #    acompletion parameters rather than model parameters. `timeout` is
    #    the sharper case — `get_supported_openai_params` lists it for no
    #    provider (measured on 1.98.0), so leaving it among the extras would
    #    hand it to the allowlist filter, which drops it.
    api_version: str | None = None
    raw_api_version = options.get("api_version")
    if isinstance(raw_api_version, str):
        api_version = raw_api_version
    timeout = _positive_seconds(options.get("timeout"))

    # 2. Strip korvid-owned transport selectors — they must never reach the wire.
    filtered = {
        k: v for k, v in options.items() if k not in _KORVID_OWNED_OPTIONS and k not in _LIFTED
    }

    # 3. Keep only what the provider accepts.  An empty `supported` means the
    #    capability lookup failed — preserve everything so the operator's
    #    explicit settings are not silently discarded (a vendor 400 with the
    #    parameter name is more actionable than a silent drop).
    if supported:
        supported_set = frozenset(supported)
        filtered = {k: v for k, v in filtered.items() if k in supported_set}

    # 4. Materialize, so the plan owns plain mutable structures a downstream
    #    SDK call can edit without reaching the frozen profile — and so a
    #    `MappingProxyType` from the profile does not take the request down
    #    on the way. The credential parameters are snapshotted rather than
    #    copied: they hold a live callable the transport invokes, so a copy
    #    would be wrong, but the plan must own a mapping the declaration
    #    that supplied it cannot rewrite later.
    extra: dict[str, object] = materialize_options(filtered)

    return RequestPlan(
        model=model,
        api_key=api_key,
        base_url=base_url,
        api_version=api_version,
        extra=extra,
        timeout=timeout,
        credential=MappingProxyType(dict(credential)),
    )
