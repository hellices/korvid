"""The single import boundary for `litellm` (design §Lockdown).

Importing this module locks LiteLLM down before any call can be made, and
fails loudly if a flag it means to set no longer exists upstream. Nothing
else in korvid imports `korvid.providers._litellm_import`; a test walks the
source tree and asserts it.
"""

from __future__ import annotations

from typing import Any, Final

import openai as _openai

import korvid.providers._litellm_import as _litellm_mod

#: Names of every attribute the lockdown sets, so the contract test does
#: not have to restate the list and drift from it.
LOCKDOWN_FLAGS: Final[tuple[tuple[str, object], ...]] = (
    ("telemetry", False),
    ("turn_off_message_logging", True),
    ("success_callback", []),
    ("failure_callback", []),
    ("callbacks", []),
    ("_async_success_callback", []),
    ("_async_failure_callback", []),
    ("suppress_debug_info", True),
)

# Check before assigning. Assigning first and reading back afterwards is a
# tautology: `setattr` on a name LiteLLM has renamed creates a fresh, unused
# attribute, the read-back passes, and the real callback sink stays open. All
# eight names exist in 1.98.0, so this is a tripwire for a future rename.
#
# This also fixes test isolation: by raising BEFORE `_litellm = _litellm_mod.litellm`,
# a failed reload during testing does not overwrite the module-level `_litellm`
# binding with a stub, so subsequent tests still see the real module.
_missing = tuple(name for name, _ in LOCKDOWN_FLAGS if not hasattr(_litellm_mod.litellm, name))
if _missing:
    raise ImportError(
        "litellm no longer defines the lockdown attributes "
        f"{', '.join(_missing)}; korvid cannot guarantee telemetry and "
        "callbacks are disabled. Pin a supported litellm release."
    )

# Applied at import, before the first call can happen. Every one of these
# is a channel that would otherwise carry prompts, tool arguments or
# usage records to a third party or to stdout.
for _name, _value in LOCKDOWN_FLAGS:
    setattr(_litellm_mod.litellm, _name, [] if isinstance(_value, list) else _value)

# Bind after lockdown passes, so a failed reload does not corrupt this reference.
_litellm = _litellm_mod.litellm

acompletion = _litellm.acompletion
get_llm_provider = _litellm.get_llm_provider
exceptions = _litellm.exceptions

#: The base class every provider error korvid must translate inherits from.
#:
#: Measured on litellm 1.98.0: of the 24 error classes `litellm.exceptions`
#: exports, exactly one (`APIError` itself) subclasses
#: `litellm.exceptions.APIError`, while 22 share `openai.OpenAIError`.
#: `AuthenticationError` -> `openai.AuthenticationError` ->
#: `openai.APIStatusError` -> `openai.APIError` -> `openai.OpenAIError`.
#: So `except litellm.exceptions.APIError` would let a 401 escape the
#: transport unmapped; this is the base that actually catches them.
#:
#: It is re-exported here so `providers/` still names exactly one module
#: for everything that comes out of the LiteLLM stack, rather than
#: `litellm_provider.py` growing a direct `import openai`. The two classes
#: outside this base -- `BudgetExceededError` and the guardrail/PII error
#: -- belong to router and guardrail features the lockdown disables.
ProviderSDKError: Final[type[Exception]] = _openai.OpenAIError


def models_by_provider() -> dict[str, list[str]]:
    """LiteLLM's provider → model-id table, normalized.

    The shipped values are heterogeneous — most providers map to a `set`,
    a handful to a `list` — so indexing one raises `TypeError`. Sorting
    also makes search output deterministic, which `set` iteration order
    is not.
    """
    return {provider: sorted(models) for provider, models in _litellm.models_by_provider.items()}


def model_cost_entry(provider: str, model_id: str) -> dict[str, Any] | None:
    """LiteLLM's cost/capability record, qualified key first.

    `model_cost` keys are not uniform: `claude-sonnet-4-5` is bare while
    `ollama/codegemma` is qualified, and for a measurable minority of
    references **both** keys exist and carry different facts (`sora-2` vs
    `openai/sora-2`, for one). Trying the bare key first therefore reads
    another provider's record for those; the provider-qualified key is
    tried first so a provider-specific record always wins.
    """
    for key in (f"{provider}/{model_id}", model_id):
        entry = _litellm.model_cost.get(key)
        if isinstance(entry, dict):
            return entry
    return None


def supported_params(model: str, provider: str) -> tuple[str, ...]:
    """Best-effort per-provider parameter allowlist."""
    try:
        params = _litellm.get_supported_openai_params(model=model, custom_llm_provider=provider)
    except (AttributeError, KeyError, ValueError):
        return ()
    return tuple(params or ())


#: The module attribute LiteLLM reads for its own TLS trust. Named rather
#: than written inline so the tripwire below and the assignment can never
#: disagree about which attribute korvid means.
_SSL_VERIFY_ATTR: Final = "ssl_verify"


def apply_ca_bundle(path: str) -> None:
    """Point LiteLLM's own TLS trust at the operator's CA bundle.

    Two client shapes live under LiteLLM and they do not read the same
    setting (measured on 1.98.0):

    * Providers built on a vendor SDK client resolve their trust through
      `get_ssl_configuration()` called with **no arguments**, so they see
      only this module-level `litellm.ssl_verify`.
    * Providers on LiteLLM's own httpx handler fall back to this global
      when no per-call value is set.

    korvid sets only this process-global and passes nothing per request.
    A per-request `ssl_verify` was measured and rejected: the SDK-shaped
    providers ignore it for TLS and forward it into the request body,
    leaking a local filesystem path to the vendor.  The global therefore
    replaces the default trust store (certifi) for every request made
    in this process, regardless of which LiteLLM client shape is in use.

    The value must be a *path*: `get_ssl_configuration` builds its own
    `SSLContext` and silently discards a pre-built one, so assigning a
    context here would look configured and verify against certifi.
    Verification itself is untouched — the resulting context is
    `CERT_REQUIRED`, hostname-checking, TLS 1.2 minimum.

    Args:
        path: Filesystem path to a PEM bundle the caller has already
            proven loadable. LiteLLM falls back to certifi *without
            error* for a path it cannot read, so an unvalidated path
            would disable the operator's trust root silently.

    Raises:
        ValueError: if LiteLLM no longer exposes the setting. A rename
            upstream would otherwise leave korvid writing an attribute
            nothing reads, and the operator's trust quietly unapplied.
    """
    if not hasattr(_litellm, _SSL_VERIFY_ATTR):
        raise ValueError(
            "network.ca_bundle cannot be applied: this litellm release no "
            f"longer exposes `{_SSL_VERIFY_ATTR}`. Pin a supported release."
        )
    setattr(_litellm, _SSL_VERIFY_ATTR, path)


#: LiteLLM's own suffix for an environment variable naming a base URL.
#: The convention is the SDK's, not a vendor list korvid maintains: every
#: provider that needs a host publishes it as `<PROVIDER>_API_BASE`.
_API_BASE_SUFFIX: Final = "_API_BASE"


def requires_explicit_api_base(model: str, *, api_key: str | None, api_base: str | None) -> bool:
    """Does LiteLLM say this reference cannot be reached without a host?

    `validate_environment` reports which environment variables a provider
    still needs; korvid reads only the ones that name a *base URL*, which
    is the single fact it can act on — a missing credential is the
    profile's business, a missing host means the request has nowhere to
    go. Providers that ship a default host report nothing here, which is
    exactly the distinction `get_llm_provider`'s `dynamic_api_base` fails
    to make (measured: `None` for most hosted vendors).

    Args:
        model: The full model reference, as the operator wrote it.
        api_key: The resolved credential, or `None`. Passed so a missing
            key does not show up as a missing *host*.
        api_base: The endpoint the profile names, or `None`.

    Returns:
        `True` only when LiteLLM names a base-URL variable it cannot
        find. Any failure to answer returns `False`: an unanswerable
        probe must not disable a profile that would have worked.
    """
    try:
        report = _litellm.validate_environment(model=model, api_key=api_key, api_base=api_base)
    except Exception:  # the probe walks third-party provider tables
        return False
    missing = report.get("missing_keys") if isinstance(report, dict) else None
    if not isinstance(missing, list):
        return False
    return any(isinstance(key, str) and key.upper().endswith(_API_BASE_SUFFIX) for key in missing)
