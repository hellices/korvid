"""Stdlib-only constants shared across the provider layer.

Imports nothing from `korvid`, so anything in `providers/` may import it
without creating a cycle.
"""

from __future__ import annotations

#: The one extra that carries the model transport.
AGENT_EXTRA: str = "agent"

#: Sent as `api_key` for a genuinely keyless *private* endpoint, so the
#: SDK's own `OPENAI_API_KEY`/`OLLAMA_API_KEY` lookup can never smuggle an
#: unrelated ambient key onto the wire. Never used for a public vendor host.
KEYLESS_API_KEY_SENTINEL: str = "korvid-keyless"

#: Names an operator still associates with a built-in and that a
#: third-party plugin must never be able to claim, even after Task 18
#: deletes the aliases themselves.
RETIRED_PROVIDER_ALIASES: frozenset[str] = frozenset({"openai-compat", "vllm", "github", "claude"})

#: Reference prefixes that must never reach LiteLLM's routing call,
#: whatever is installed.
#:
#: Measured on 1.98.0, for both of them: resolving one of these prefixes
#: constructs the provider's `Authenticator` *inside* `get_llm_provider`,
#: which creates a credential directory under `~/.config/litellm/` and
#: then asks it for a token — printing a user code and blocking on a
#: five-second poll when none is cached. Neither honours the profile: the
#: credential the operator configured is replaced by the authenticator's,
#: and `chatgpt` replaces the endpoint too, so no argument korvid could
#: pass makes such a reference safe to resolve.
#:
#: So a prefix here has to be claimed *before* routing even when no flow
#: is installed to serve it — otherwise uninstalling the plugin turns the
#: reference back into a device-login trap. Stored in korvid's normalized
#: (hyphen) spelling; `special_flows.normalize_prefix` folds LiteLLM's
#: underscore form onto it, so both spellings hit the same claim.
#:
#: `tests/providers/test_device_login_prefixes.py` rediscovers this set
#: from the installed release, so a future one that ships a third
#: device-code provider fails there rather than in a terminal.
DEVICE_LOGIN_PREFIXES: frozenset[str] = frozenset({"github-copilot", "chatgpt"})

#: The reserved names korvid still serves itself, either through the
#: standard transport or through a flow of its own. Unlike the two sets
#: above these stay fully routable — reserving a name says who may
#: *register* it, never whether korvid will dispatch it.
#:
#: They are listed because the refusal must not be a consequence of a
#: vendor's release artefact. Measured on litellm 1.98.0, all four are
#: rows in `models_by_provider()`, which is the only thing refusing them
#: to a third party today; a release that drops a row would hand an
#: operator's `provider: openai` to whoever registered the entry point.
_SELF_SERVED_PROVIDER_NAMES: frozenset[str] = frozenset({"openai", "azure", "anthropic", "ollama"})

#: Every name a third-party plugin may not register, in korvid's
#: normalized spelling. Composed rather than restated: each part is
#: reserved for its own reason, and one list spelled twice is one list
#: that will disagree with itself.
RESERVED_PROVIDER_NAMES: frozenset[str] = (
    RETIRED_PROVIDER_ALIASES | DEVICE_LOGIN_PREFIXES | _SELF_SERVED_PROVIDER_NAMES
)
