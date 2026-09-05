"""Frozen request plan: profile → LiteLLM kwargs, built once per call.

Building the call once means the outbound snapshot and the wire payload are
the same object rather than two constructions that can drift.

Parameter names are taken from ``acompletion``'s real signature in 1.98.0:
``base_url`` and ``api_version`` are named parameters; ``api_base`` is only
reachable through ``**kwargs``, so korvid uses the named ones.

``api_key`` is tri-state:

- A resolved credential string is passed verbatim.
- ``None`` (genuinely keyless private endpoint) causes ``KEYLESS_API_KEY_SENTINEL``
  to be sent so the SDK's own ``OPENAI_API_KEY`` lookup can never smuggle an
  unrelated ambient key onto the wire.
- ``OMIT_API_KEY`` means the argument is absent entirely; the vendor SDK's own
  credential chain is consulted (``provider-default`` auth).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from korvid.providers.litellm_settings import KEYLESS_API_KEY_SENTINEL

# ---------------------------------------------------------------------------
# Korvid-owned option keys — transport selectors, not model parameters.
# ---------------------------------------------------------------------------
_KORVID_OWNED_OPTIONS: frozenset[str] = frozenset(
    {"native_thinking", "ca_bundle", "num_ctx_source"}
)


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
    api_key: str | _OmitApiKey | None
    base_url: str | None
    api_version: str | None
    extra: Mapping[str, object]

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
        if stream:
            kwargs["stream_options"] = {"include_usage": True}
        kwargs.update(copy.deepcopy(dict(self.extra)))
        return kwargs


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def build_plan(
    *,
    model: str,
    api_key: str | _OmitApiKey | None,
    base_url: str | None,
    options: Mapping[str, object],
    supported: Sequence[str],
) -> RequestPlan:
    """Resolve config into a plan, dropping options the provider rejects.

    Args:
        model: LiteLLM model string (e.g. ``"openai/gpt-4o"``).
        api_key: Resolved credential, ``None`` (keyless), or ``OMIT_API_KEY``.
        base_url: Override base URL, or ``None`` to use the provider default.
        options: Raw operator options from the profile.
        supported: Parameter names the provider accepts. An *empty* sequence
            means the capability lookup failed; in that case all non-owned keys
            are forwarded rather than silently dropped.

    Returns:
        A frozen ``RequestPlan`` ready for snapshotting and wiring.
    """
    # 1. Lift api_version before filtering (it is a named acompletion param).
    api_version: str | None = None
    raw_api_version = options.get("api_version")
    if isinstance(raw_api_version, str):
        api_version = raw_api_version

    # 2. Strip korvid-owned transport selectors — they must never reach the wire.
    filtered = {
        k: v for k, v in options.items() if k not in _KORVID_OWNED_OPTIONS and k != "api_version"
    }

    # 3. Keep only what the provider accepts.  An empty `supported` means the
    #    capability lookup failed — preserve everything so the operator's
    #    explicit settings are not silently discarded (a vendor 400 with the
    #    parameter name is more actionable than a silent drop).
    if supported:
        supported_set = frozenset(supported)
        filtered = {k: v for k, v in filtered.items() if k in supported_set}

    # 4. Deep-copy so a frozen MappingProxyType in the profile can never be
    #    mutated by a downstream SDK call.
    extra: dict[str, object] = copy.deepcopy(filtered)

    return RequestPlan(
        model=model,
        api_key=api_key,
        base_url=base_url,
        api_version=api_version,
        extra=extra,
    )
