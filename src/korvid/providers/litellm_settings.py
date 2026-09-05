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
