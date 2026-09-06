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
import re
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
#: Composed into ``RESERVED_CALL_ARGUMENTS`` below rather than restated
#: there, so the rule that removes them from the extras and the rule that
#: lifts them can never name different keys.
_LIFTED: frozenset[str] = frozenset({"api_version", "timeout"})


# ---------------------------------------------------------------------------
# Reserved call arguments — the engine owns the request, the operator owns
# the model parameters, and the two sets never overlap.
# ---------------------------------------------------------------------------

#: Call arguments korvid decides and an operator option may never occupy.
#:
#: The per-provider allowlist is no protection here: measured on litellm
#: 1.98.0, ``get_supported_openai_params`` reports ``stream``,
#: ``stream_options``, ``tools`` and ``tool_choice`` as supported for
#: essentially every provider, so a profile option of that name passes the
#: filter and — before this set existed — was merged *after* the engine's
#: own value and replaced it. What that bought an operator (or anyone who
#: could write their config file) was re-routing the request to another
#: host, swapping the credential, muting the agent's tools, or turning
#: streaming off underneath a streaming reader.
#:
#: ``tool_choice`` is owned even though korvid never sets it. korvid drives
#: the tool loop; a profile that forced or disabled tool calls would change
#: the agent's behaviour while still reporting the tools as available.
#: ``functions``/``function_call`` are the deprecated spellings of exactly
#: those two arguments and are owned for the same reason.
#:
#: ``client`` is owned because a flow passes its own transport there, and
#: ``api_base``/``custom_llm_provider`` because both reach LiteLLM through
#: ``**kwargs`` and both re-route the request.
#:
#: ``_LIFTED`` is composed in rather than restated: ``api_version`` and
#: ``timeout`` *are* the operator's to set, they simply travel by the
#: plan's named parameters, and this set is what takes them out of the
#: extras on the way.
RESERVED_CALL_ARGUMENTS: Final[frozenset[str]] = _LIFTED | frozenset(
    {
        # The conversation itself.
        "model",
        "messages",
        # How the response is read.
        "stream",
        "stream_options",
        # Tool dispatch, in both the current and the deprecated spelling.
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        # Who the request authenticates as, and where it goes.
        "api_key",
        "api_base",
        "base_url",
        "custom_llm_provider",
        "client",
    }
)

#: Splits ASCII camelCase and acronym boundaries so ``apiKey``, ``APIKey``
#: and ``api_key`` tokenize the same way. The same two transitions
#: ``core/config.py`` splits on before it refuses a secret-bearing key.
_CAMEL_BOUNDARY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])"  # lowerUpper: apiKey → api_Key
    r"|(?<=[A-Z])(?=[A-Z][a-z])"  # ACRONYMWord: APIKey → API_Key
)

_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: Key segments that name a credential rather than a model parameter.
#: Deliberately the same vocabulary ``core/config.py`` refuses in a
#: profile's options, so the two boundaries cannot disagree about what a
#: credential looks like — and deliberately the *singular* ``token``:
#: ``max_tokens`` and ``max_completion_tokens`` tokenize to ``tokens`` and
#: must keep working.
_CREDENTIAL_SEGMENTS: Final[frozenset[str]] = frozenset(
    {"apikey", "authorization", "credential", "credentials", "password", "secret", "token"}
)

#: Credential names that are only credential names as a pair. ``key`` alone
#: is a real model parameter segment (``prompt_cache_key``), so it is
#: matched as a two-token window instead.
_CREDENTIAL_SEGMENT_PAIRS: Final[tuple[tuple[str, str], ...]] = (("api", "key"), ("access", "key"))


def _key_segments(key: str) -> tuple[str, ...]:
    """*key* as lowercase word segments, camelCase boundaries included."""
    lowered = _CAMEL_BOUNDARY_RE.sub("_", key).lower()
    return tuple(part for part in _SEPARATOR_RE.split(lowered) if part)


def _names_a_credential(key: str) -> bool:
    """Whether *key* names a credential or an auth selector.

    Matched by shape rather than by a list of vendor parameter names: that
    list is unbounded, it would go stale on the next SDK release, and this
    module must not branch on a vendor. Anything that matches is dropped
    rather than merely prevented from overriding — an argument the
    provider does not consume is forwarded into the *request body*
    (measured on 1.98.0), so a credential-shaped option would send
    whatever it holds to the vendor as an unknown field.
    """
    segments = _key_segments(key)
    if _CREDENTIAL_SEGMENTS.intersection(segments):
        return True
    return any(
        segments[index : index + 2] == pair
        for pair in _CREDENTIAL_SEGMENT_PAIRS
        for index in range(len(segments) - 1)
    )


def is_reserved_call_argument(key: str) -> bool:
    """Whether *key* is korvid's to decide rather than the operator's."""
    return key in RESERVED_CALL_ARGUMENTS or _names_a_credential(key)


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
        kwargs.update(self._operator_arguments())
        # Last, and neither copied nor filtered: a declared credential
        # parameter is a live callable the transport invokes per request.
        kwargs.update(self.credential)
        return kwargs

    def _operator_arguments(self) -> dict[str, Any]:
        """The profile's own parameters, in the shape the SDK is handed.

        Filtered rather than merely out-ordered, and filtered *here* rather
        than only in ``build_plan``: ``RequestPlan`` is a public dataclass a
        caller can assemble directly, and the reserved policy has to hold
        for every plan that reaches the wire. An engine argument the plan
        deliberately *omits* — no ``api_key`` under ``provider-default``, no
        ``tools`` on a toolless request, no ``stream_options`` on a blocking
        one — is exactly the case a merge order alone would not protect.
        """
        return {
            key: _materialize(value)
            for key, value in self.extra.items()
            if not is_reserved_call_argument(key) and key not in self.credential
        }


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

    # 2. Strip everything that is not the operator's to set: korvid-owned
    #    transport selectors, which must never reach the wire, and the
    #    reserved call arguments, which korvid decides. `_LIFTED` is a
    #    subset of the reserved set, so `api_version` and `timeout` leave
    #    with them rather than through a second rule.
    filtered = {
        k: v
        for k, v in options.items()
        if k not in _KORVID_OWNED_OPTIONS and not is_reserved_call_argument(k)
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
