# Provider plugins

Third-party provider plugins are the escape hatch for LLM backends that do
**not** fit korvid's built-in providers:

- `github-copilot`
- `ollama`
- `openai-compat` and its built-in aliases: `openai`, `azure`, `vllm`,
  `github`, `anthropic`, `claude`

If your backend already speaks an OpenAI-compatible `/v1` API, prefer the
built-in `openai-compat` path instead of a plugin. Reach for a plugin only
when the wire protocol or auth flow truly differs.

> **Security warning:** provider plugins are trusted, in-process Python code
> loaded into the korvid process. Selected-only loading avoids importing
> *unused* plugins, but it is **not** a sandbox. Install only plugins you
> trust. `create()` receives only a `ProviderPluginConfig` and an optional
> `CredentialSource` — never conversation data. The provider it returns is
> then called with the same sanitized canonical `messages`/`tools` payload
> `OutboundPolicy` builds for built-in providers (see
> [`docs/threat-model.md`](threat-model.md)) — but once your provider's
> `complete()` receives that payload, it is free to mutate, retain, log,
> cache, or independently transmit it anywhere; korvid has no further
> control or visibility past the handoff. See
> [`SECURITY.md`](https://github.com/hellices/korvid/blob/main/SECURITY.md) to report a vulnerability.

## When you should not write a plugin

Use a built-in config whenever possible:

- **OpenAI, Azure OpenAI, GitHub Models, Anthropic compatibility endpoint,
  vLLM, local gateways, internal proxies:** use `provider: openai-compat` or
  a built-in alias.
- **Native Ollama `/api/chat`:** use `provider: ollama`.
- **GitHub Copilot device login:** use `provider: github-copilot`.

A plugin is warranted only when you need a genuinely different protocol or auth
scheme composition behind korvid's public `CredentialSource` boundary.

## Packaging and entry point

Install the plugin into the same Python environment as `korvid[agent]`, then
register one entry point in the `korvid.provider` group.

```toml
[project]
name = "acme-korvid-provider"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "korvid[agent]>=0.1.0",
  "httpx>=0.27",
]

[project.entry-points."korvid.provider"]
company-llm = "acme_korvid_provider.plugin:CompanyProviderPlugin"
```

The entry-point name is the operator-facing `agent.provider` value. korvid
normalizes provider names by lowercasing and collapsing runs of `-`, `_`, and
`.` into `-`, so `Company_LLM`, `company.llm`, and `company-llm` all collide.
Two installed distributions claiming the same normalized name are rejected.

## Operator configuration

Today, the `:ai` wizard exposes only the built-ins. Third-party plugins are
configured directly in `~/.config/korvid/config.yaml`:

```yaml
agent:
  provider: company-llm
  base_url: https://llm.example.internal
  model: cluster-brain
  auth: {method: api_key}
  api_key_env: COMPANY_LLM_TOKEN
  options:
    tenant: platform
    fallback_models:
      - cluster-brain
      - cluster-brain-canary
```

`api_key_env` names the environment variable; the secret itself never belongs
in the config file. Plugin auth methods are limited to `none`, `api_key`, and
`entra`.

## API 2: exact public surface

Import only from korvid's public agent boundary:

```python
from korvid.agent.credentials import CredentialSource
from korvid.agent.model_policy import (
    CapabilitySource,
    ModelCapabilities,
    ModelDescriptor,
    ModelTier,
)
from korvid.agent.provider import LLMProvider
from korvid.agent.provider_plugin import (
    PROVIDER_PLUGIN_API_VERSION,
    ProviderPlugin,
    ProviderPluginConfig,
    ProviderPluginMetadata,
)
```

The published API 2 surface is:

```python
class CredentialSource(ABC):
    async def headers(self) -> dict[str, str]: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    provider: str   # your registered plugin name
    model: str      # the model tag, e.g. "company-llm:v2"


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    context_window_tokens: int | None = None
    supports_tools: bool | None = None
    supports_parallel_tools: bool | None = None
    supports_reasoning: bool | None = None
    recommended_tier: ModelTier | None = None       # ModelTier.LOW / .HIGH
    provenance: Mapping[str, CapabilitySource] = ...


class LLMProvider(ABC):
    @property
    def descriptor(self) -> ModelDescriptor: ...

    @property
    def capabilities(self) -> ModelCapabilities: ...

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        yield ...  # async generator (async def + yield)

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class ProviderPluginMetadata:
    api_version: int
    name: str
    display_name: str
    auth_methods: tuple[str, ...]
    supports_generic_setup: bool = True


@dataclass(frozen=True)
class ProviderPluginConfig:
    base_url: str | None
    model: str | None
    auth_method: str | None
    api_key_env: str | None
    options: Mapping[str, object]


class ProviderPlugin(ABC):
    @property
    def metadata(self) -> ProviderPluginMetadata: ...

    def create(
        self,
        config: ProviderPluginConfig,
        credentials: CredentialSource | None,
    ) -> LLMProvider: ...
```

### What changed in API 2 (breaking)

`LLMProvider` no longer has a `name` property. A plugin must implement two
properties instead, and both are validated the moment korvid wraps it:

| API 1 | API 2 |
| --- | --- |
| `name -> str` (the model tag) | `descriptor -> ModelDescriptor` (provider id **and** model tag) |
| *(nothing)* | `capabilities -> ModelCapabilities` |

The reason is routing. korvid resolves a **model tier** per session — which
tool surface is armed, how many iterations a turn gets, how much history is
retained, which prompt pack is composed — and a bare model string cannot
answer that. `capabilities` is how a plugin participates in that decision
instead of being routed by the fallback.

Reporting nothing is a valid, safe answer: return
`ModelCapabilities.unknown()` and korvid falls back to the shipped catalog
and then to the `low` tier. Report only what your backend actually knows:

- `supports_tools=False` is a **hard stop** — korvid refuses to start the
  agent rather than routing a model that cannot call tools.
- `supports_parallel_tools` is honored only on the `high` tier; the `low`
  tier is always sequential.
- `recommended_tier` is a request, not a command: an explicit
  `agent.model_tier` in `config.yaml` still wins.
- `provenance` maps a fact name to a `CapabilitySource`; korvid rejects an
  unknown fact name or a non-`CapabilitySource` value.

Validation is strict, and rejection is immediate rather than at first use:

- `descriptor` must be a real `ModelDescriptor`, its `provider` must equal
  your registered plugin name (a plugin cannot claim to be another
  provider), and its `model` must be non-empty and reasonably short.
- `capabilities` must be a real `ModelCapabilities`.

Notes:

- `PROVIDER_PLUGIN_API_VERSION` is currently **exactly `2`**. A plugin whose
  `metadata.api_version` differs is rejected at load.
- `metadata.name` must match the normalized entry-point name.
- `auth_methods` must be a tuple of unique strings from `{ "none", "api_key",
  "entra" }`.
- `supports_generic_setup` is part of API 2 metadata, but current korvid
  releases do **not** auto-discover third-party plugins in the `:ai` wizard.
  Treat it as forward-compatibility metadata for now.
- `LLMProvider.complete()` must be an **async generator** (`async def` with
  `yield`), not a plain coroutine that returns an iterator.
- `LLMProvider` also defines `prepare_messages(messages)`, the built-in
  adapters' hook for dialect conversion ahead of `OutboundPolicy`. korvid
  does **not** call it on plugin providers — plugins only ever receive the
  sanitized canonical payload — so overriding it in a plugin has no effect.
  Adapt the payload inside `complete()` instead, and treat everything you
  add there as leaving korvid's inspected boundary.

## Complete minimal adapter

This is the smallest complete plugin that matches the exact source signatures
and uses korvid's auth boundary.

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from korvid.agent.credentials import CredentialSource
from korvid.agent.model_policy import (
    CapabilitySource,
    ModelCapabilities,
    ModelDescriptor,
    ModelTier,
)
from korvid.agent.provider import LLMProvider
from korvid.agent.provider_plugin import (
    PROVIDER_PLUGIN_API_VERSION,
    ProviderPlugin,
    ProviderPluginConfig,
    ProviderPluginMetadata,
)


class CompanyProvider(LLMProvider):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        credentials: CredentialSource | None,
        tenant: str | None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._credentials = credentials
        self._tenant = tenant
        self._client = httpx.AsyncClient(timeout=30.0)

    @property
    def descriptor(self) -> ModelDescriptor:
        # `provider` must equal the registered plugin name, or korvid
        # rejects the provider rather than letting it claim another id.
        return ModelDescriptor("company-llm", self._model)

    @property
    def capabilities(self) -> ModelCapabilities:
        # Report only what this backend actually knows. Everything omitted
        # stays unknown and routing falls back to korvid's catalog, then to
        # the `low` tier. `ModelCapabilities.unknown()` is a valid answer.
        return ModelCapabilities(
            context_window_tokens=128_000,
            supports_tools=True,
            supports_parallel_tools=False,
            recommended_tier=ModelTier.HIGH,
            provenance={
                "context_window_tokens": CapabilitySource.PROVIDER,
                "supports_tools": CapabilitySource.PROVIDER,
                "supports_parallel_tools": CapabilitySource.PROVIDER,
                "recommended_tier": CapabilitySource.PROVIDER,
            },
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        headers = await self._credentials.headers() if self._credentials is not None else {}
        if self._tenant:
            headers["X-Tenant"] = self._tenant

        response = await self._client.post(
            f"{self._base_url}/chat",
            json={
                "model": self._model,
                "messages": messages,
                "tools": tools,
                "stream": stream,
            },
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()

        text = payload.get("text")
        if isinstance(text, str) and text:
            yield {"type": "text_delta", "text": text}

        usage = payload.get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                yield {
                    "type": "usage",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }

        yield {"type": "done"}

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        finally:
            if self._credentials is not None:
                await self._credentials.aclose()


class CompanyProviderPlugin(ProviderPlugin):
    @property
    def metadata(self) -> ProviderPluginMetadata:
        return ProviderPluginMetadata(
            api_version=PROVIDER_PLUGIN_API_VERSION,
            name="company-llm",
            display_name="Company LLM",
            auth_methods=("api_key",),
            supports_generic_setup=True,
        )

    def create(
        self,
        config: ProviderPluginConfig,
        credentials: CredentialSource | None,
    ) -> LLMProvider:
        if not config.base_url:
            raise ValueError("company-llm requires agent.base_url")
        if not config.model:
            raise ValueError("company-llm requires agent.model")

        tenant = config.options.get("tenant")
        tenant_value = tenant if isinstance(tenant, str) else None

        return CompanyProvider(
            base_url=config.base_url,
            model=config.model,
            credentials=credentials,
            tenant=tenant_value,
        )
```

If your backend emits tool requests, yield `tool_call` events with the exact
shape below.

## Event contract and exact limits

korvid wraps every plugin provider in `ValidatedPluginProvider`, which enforces
and normalizes the event stream. Events must be mappings with one of these
shapes:

| Event | Required keys | Exact bounds |
|---|---|---|
| `text_delta` | `{"type": "text_delta", "text": str}` | `text` max **65,536 UTF-8 bytes** |
| `tool_call` | `{"type": "tool_call", "id": str, "name": str, "arguments": str}` | `id` non-empty, max **256** chars; `name` non-empty, max **256** chars; `arguments` max **65,536 UTF-8 bytes** |
| `usage` | `{"type": "usage", "input_tokens": int, "output_tokens": int}` | each token count is a non-bool int in **0..1,000,000,000** |
| `done` | `{"type": "done"}` | no payload; exactly one terminal done required |

Extra keys are discarded. Unknown event types, non-mapping payloads, missing
fields, overlong strings, or out-of-range token counts raise
`ProviderPluginContractError`.

These four are the whole contract. korvid's built-in adapters yield one
extra internal event to tell the runtime their HTTP request has actually
reached the transport — the `:ai payload` inspector uses it so a request
that was never sent is not shown as the session's last handoff. It is not
part of API 2 and a plugin that yields it is rejected like any other
unknown type. Your request is recorded when you yield your **first** event
instead, which is equally proof that it ran; a `complete()` that yields
nothing records nothing.

`tool_call.arguments` is a **string** payload, typically JSON-encoded
arguments, not a nested mapping.

`ValidatedPluginProvider.aclose()` forwards to your provider once; duplicate
closes are swallowed.

## Options contract, immutability, and secret policy

`agent.options` is the only plugin-specific config bag. API 2 accepts only
JSON-like values:

- `null`
- `bool`
- `int`
- finite `float`
- `str`
- `list`
- nested `mapping` with string keys

Exact parser limits:

- max depth: **4**
- max mapping keys across the whole structure: **64**
- max list items across the whole structure: **64**
- max string length (values and keys): **2048 UTF-8 bytes**
- max serialized JSON budget: **16384 bytes**
- option keys must be **ASCII-only** (non-ASCII keys are rejected before any
  normalization or secret detection)

Secret-looking keys are rejected before the plugin sees them. The reserved key
segments are exactly:

- `secret`
- `password`
- `token`
- `api_key` (and compact form `apikey`)
- `authorization`
- `credential`

CamelCase keys are split at word boundaries before matching, so `apiKey`,
`clientSecret`, `accessToken`, `APIKey`, and `clientAPIKey` are all rejected.

Store secrets in environment variables and pass only the variable name via
`agent.api_key_env`.

Immutability details matter:

- `ProviderPluginConfig.options` is always a read-only top-level mapping.
- Live wizard/reconnect flows build `AgentSettings`, which deep-freezes nested
  mappings and converts every `list` to an immutable `tuple`.
- Startup from `config.yaml` preserves YAML lists as lists before
  `ProviderPluginConfig` wraps the top-level mapping.

So plugin code should treat `options` as read-only and accept sequence values
as either `list` or `tuple`.

## Lifecycle and compatibility

Plugin lifecycle in current korvid builds:

1. korvid discovers all `korvid.provider` entry points across installed
   distributions.
2. It loads **only the selected** provider's entry point; unselected plugin
   modules are never imported.
3. It validates `api_version`, `metadata.name`, and `auth_methods`, then caches
   the instantiated `ProviderPlugin`.
4. korvid builds credentials first and passes only `ProviderPluginConfig` plus
   `CredentialSource | None` into `create()`. Plugins do **not** receive kube
   clients, UI handles, audit handles, or write executors.
5. Your `LLMProvider` instance is wrapped in `ValidatedPluginProvider`.
6. korvid calls `LLMProvider.aclose()` when the provider is replaced or at
   shutdown. **Your adapter owns the injected `CredentialSource`**: close it
   in `aclose()` (in a `finally` block) alongside any HTTP clients or other
   resources. Failure to close credentials leaks token-refresh HTTP sessions.

Compatibility rules:

- Built-ins are reserved and never hit the plugin registry:
  `github-copilot`, `ollama`, `openai-compat`, `openai`, `azure`, `vllm`,
  `github`, `anthropic`, `claude`.
- Unknown providers without a registry still disable the agent cleanly instead
  of crashing older code paths.
- A plugin failure during initial startup becomes a warning and leaves the app
  running with the agent disabled.
- A plugin failure during a live rebuild rejects the new provider and keeps the
  previous provider open.
- Factory/load/metadata errors are translated into bounded
  `ProviderPluginError` messages, capped to 200 characters to avoid traceback
  and secret leakage.

## Operator checklist

Before shipping a plugin:

1. Confirm the backend truly needs a plugin instead of `openai-compat`.
2. Pick a unique provider name that does not collide with built-ins or another
   installed distribution after normalization.
3. Keep secrets out of `agent.options`; use env vars and `CredentialSource`.
4. Test both startup and live reconfiguration paths.
5. Verify every emitted event matches the API 2 table above.
