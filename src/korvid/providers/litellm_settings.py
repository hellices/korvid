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
#: Measured on 1.98.0: resolving `github_copilot/...` starts an
#: interactive GitHub device login *inside* `get_llm_provider`, blocks
#: polling for a code and writes `~/.config/litellm/github_copilot/
#: api-key.json` before it raises. So the prefix has to be claimed
#: *before* routing even when no flow is installed to serve it —
#: otherwise uninstalling the plugin turns the reference back into a
#: device-login trap. Stored in korvid's normalized (hyphen) spelling;
#: `special_flows.normalize_prefix` folds LiteLLM's underscore form onto
#: it, so both spellings hit the same claim.
DEVICE_LOGIN_PREFIXES: frozenset[str] = frozenset({"github-copilot"})
