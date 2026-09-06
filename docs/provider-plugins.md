# Provider plugins

korvid routes every model reference through LiteLLM's bundled catalog. Over
2,000 models from dozens of providers work out of the box — no plugin needed.
**You almost certainly do not need a plugin.** The only reasons to write one
are:

- Your backend's wire protocol is not OpenAI-compatible and LiteLLM does not
  support it.
- Your auth flow is genuinely non-standard — for example, a credential that
  refreshes through a proprietary OIDC variant.

If your backend speaks an OpenAI-compatible API at any URL, a profile with an
`endpoint` is the right answer. If the auth is non-standard but the transport
is fine, a `korvid.credential` chain (see below) covers it without a plugin.

> **Security warning:** provider plugins are trusted, in-process Python code
> loaded into the korvid process. Selected-only loading avoids importing
> *unused* plugins, but it is **not** a sandbox. Install only plugins you
> trust. `create()` receives only a `ProviderPluginConfig` and an optional
> `CredentialSource` — never conversation data. The provider it returns is
> then called with the same sanitized canonical `messages`/`tools` payload
> `OutboundPolicy` builds for built-in providers (see
> [`docs/threat-model.md`](threat-model.md)) — but once your `complete()`
> receives that payload it may mutate, retain, log, cache, or transmit it
> anywhere; korvid has no visibility past the handoff. See
> [`SECURITY.md`](https://github.com/hellices/korvid/blob/main/SECURITY.md) to report a vulnerability.

## Special flows (the current extension point)

The `korvid.provider` entry-point group now loads **`SpecialFlow`** objects,
not `ProviderPlugin` objects. A `SpecialFlow` claims a prefix and builds a
provider for profiles under that prefix. This is the extension mechanism
that third parties should use for non-standard backends.

```python
from korvid.agent.model_profiles import SpecialFlow, ModelConnectionConfig
from korvid.agent.provider import LLMProvider


class MyFlow(SpecialFlow):
    prefix = "my-backend"          # claimed prefix; must be unique

    def build_provider(
        self, profile: ModelConnectionConfig
    ) -> LLMProvider | None:
        # Return an LLMProvider, or None to fall back to the standard transport.
        ...
```

The entry point registers the flow object, not a class:

```toml
[project.entry-points."korvid.provider"]
my-backend = "my_pkg.flow:MyFlow"
```

### Prefix claiming rules

korvid normalizes prefixes by lowercasing and collapsing runs of `-`, `_`, and
`.` to `-`. Two flows whose names normalize to the same string collide. A flow
that claims a prefix blocks LiteLLM from routing that prefix — even if the flow
is later removed. `DEVICE_LOGIN_PREFIXES` in `providers/litellm_settings.py`
always claims `github-copilot` regardless of whether the Copilot flow is
installed, because LiteLLM's own copilot resolver starts an interactive device
login inside the routing call. See the
[threat model](threat-model.md#the-github-copilot-routing-hazard).

The following names are **reserved** and may never be claimed by a third party
(after normalization):

- **Served by korvid:** `openai`, `azure`, `anthropic`, `ollama`
- **Device-login trap:** `github-copilot`
- **Retired aliases:** `openai-compat`, `vllm`, `github`, `claude`

A registration that uses any of these names is rejected at startup with an
error naming the offender.

### Option claiming

A flow receives its profile's `options` mapping. Use it for flow-specific
config (tenant ids, feature flags, extra headers). The same option-key secret
policy applies: keys named `secret`, `password`, `token`, `api_key`, etc. are
rejected before the flow sees them. See the
[Options contract](#options-contract-immutability-and-secret-policy) section
for the full list of constraints.

## The `ProviderPlugin` compatibility path

`ProviderPlugin` / `ProviderPluginConfig` (the pre-flow API) **is not wired in
current builds**. korvid loads `korvid.provider` entry points as `SpecialFlow`
objects; a plugin registered as a `ProviderPlugin` class is not instantiated.
The API 2 surface described below (event contract, options limits, secret
policy, `LLMProvider`, `ModelCapabilities`) still describes what a provider
must satisfy, but the construction path is `SpecialFlow.build_provider`, not
`ProviderPlugin.create`.

If you have an existing API 2 plugin, migrate it by:

1. Implementing `SpecialFlow` with the same `prefix` as your entry point name.
2. Moving the logic from `ProviderPlugin.create` into `SpecialFlow.build_provider`.
3. Updating your entry point to point at the `SpecialFlow` class.

For reference, `korvid.providers.flow_ollama_thinking` is a shipped example.

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

The entry-point name is the operator-facing `model` prefix. korvid
normalizes provider names by lowercasing and collapsing runs of `-`, `_`, and
`.` into `-`, so `Company_LLM`, `company.llm`, and `company-llm` all collide.
Two installed distributions claiming the same normalized name are rejected.

### Credential chains for `provider-default`

A backend whose *transport* korvid already speaks but whose *credential* it
does not may declare a chain rather than a whole provider. A declaration is
data — a prefix, a display name, and a callable returning the transport call
parameters that carry a refreshing credential — published on the
`korvid.credential` group:

```toml
[project.entry-points."korvid.credential"]
company-llm = "acme_korvid_provider.credentials"
```

The module exposes `korvid_provider_default_credentials()` returning
`ProviderDefaultCredential` declarations. korvid consults one only when the
profile already says `auth.method: provider-default`, so an installed package
cannot change how a profile authenticates behind the operator's back — and
never on a name korvid ships. `resolve` raises `CredentialUnavailable`, with
an operator-facing message, when it cannot supply a credential: korvid refuses
the profile while it is being built rather than on the first message. korvid's
own Entra ID chain for `azure` is declared this way.

## Operator configuration

Today, the `:ai` wizard exposes only the built-ins. Third-party plugins are
configured directly in `~/.config/korvid/config.yaml`:

```yaml
agent:
  active: company
  profiles:
    company:
      model: company-llm/cluster-brain
      endpoint: https://llm.example.internal
      auth:
        method: environment
        key: COMPANY_LLM_TOKEN
      options:
        tenant: platform
        fallback_models:
          - cluster-brain
          - cluster-brain-canary
```

`auth.key` names the environment variable; the secret itself never belongs
in the config file.

## API 2: exact public surface

Import only from korvid's public agent boundary — `korvid.agent.credentials`,
`korvid.agent.model_policy`, `korvid.agent.provider`, and
`korvid.agent.provider_plugin` (the adapter below shows the full import list).
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

`LLMProvider` no longer has a `name` property. A plugin implements two
properties instead, both validated the moment korvid wraps it:

| API 1 | API 2 |
| --- | --- |
| `name -> str` (the model tag) | `descriptor -> ModelDescriptor` (provider id **and** model tag) |
| *(nothing)* | `capabilities -> ModelCapabilities` |

The reason is routing: korvid resolves a **model tier** per session — which
tool surface is armed, how long a turn runs, which prompt pack is composed —
and a bare model string cannot answer that. Reporting nothing is a valid, safe
answer: return `ModelCapabilities.unknown()` and korvid falls back to the
shipped catalog, then to the `low` tier. Report only what your backend knows —
`supports_tools=False` is a **hard stop** (korvid refuses to start the agent
rather than route a model that cannot call tools), `supports_parallel_tools`
is honored only on the `high` tier, `recommended_tier` loses to an explicit
`agent.model_tier`, and `provenance` must map a known fact name to a
`CapabilitySource`.

Rejection is immediate rather than at first use. `descriptor` must be a real
`ModelDescriptor` whose `provider` equals your registered plugin name — a
plugin cannot claim to be another provider — with a non-empty, short `model`;
`capabilities` must be a real `ModelCapabilities`; `metadata.api_version` must
equal `PROVIDER_PLUGIN_API_VERSION` (**exactly `2`**); `metadata.name` must
match the normalized entry-point name; and `auth_methods` must be a tuple of
unique strings from `{"none", "api_key", "entra"}`. `supports_generic_setup`
is forward-compatibility metadata: current releases do not auto-discover
third-party plugins in the `:ai` wizard.

Two shape traps: `complete()` must be an **async generator** (`async def` with
`yield`), not a coroutine returning an iterator; and `prepare_messages()`, the
built-in adapters' dialect hook, is **never** called on plugin providers, so
overriding it has no effect. Adapt inside `complete()` instead, and treat
everything you add there as leaving korvid's inspected boundary.

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
        return ModelDescriptor("company-llm", self._model)

    @property
    def capabilities(self) -> ModelCapabilities:
        # Omitted facts stay unknown; `ModelCapabilities.unknown()` is valid.
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
`ProviderPluginContractError`. `tool_call.arguments` is a **string** payload,
typically JSON-encoded arguments, not a nested mapping, and
`ValidatedPluginProvider.aclose()` forwards to your provider once — duplicate
closes are swallowed.

These four are the whole contract. korvid's built-in adapters yield one extra
internal event so the `:ai payload` inspector can tell a request that reached
the transport from one that never did; it is not part of API 2, and a plugin
that yields it is rejected like any other unknown type. Your request is
recorded when you yield your **first** event instead — a `complete()` that
yields nothing records nothing.

## Options contract, immutability, and secret policy

`agent.options` is the only plugin-specific config bag. API 2 accepts only
JSON-like values — `null`, `bool`, `int`, finite `float`, `str`, `list`, and
nested mappings with string keys — within exact parser limits:

- max depth: **4**
- max mapping keys across the whole structure: **64**
- max list items across the whole structure: **64**
- max string length (values and keys): **2048 UTF-8 bytes**
- max serialized JSON budget: **16384 bytes**
- option keys must be **ASCII-only** (non-ASCII keys are rejected before any
  normalization or secret detection)

Secret-looking keys are rejected before the plugin sees them. The reserved key
segments are exactly `secret`, `password`, `token`, `api_key` (and the compact
form `apikey`), `authorization`, and `credential`. CamelCase keys are split at
word boundaries before matching, so `apiKey`, `clientSecret`, `accessToken`,
`APIKey`, and `clientAPIKey` are all rejected. Store secrets in environment
variables and pass only the variable name via the profile's `auth.key`.

Treat `options` as read-only, and accept sequence values as either `list` or
`tuple`: the top-level mapping is always read-only, live wizard/reconnect flows
deep-freeze nested mappings and convert every `list` to a `tuple`, while
startup from `config.yaml` preserves YAML lists as lists.

## Lifecycle and compatibility

Plugin lifecycle in current korvid builds:

1. korvid discovers all `korvid.provider` entry points across installed
   distributions and reads the `SpecialFlow` each one publishes.
2. A flow owns its `prefix`. A reference under a claimed prefix is never
   routed, so a flow can never be silently bypassed.
3. The flow's `build_provider(profile)` is called with the profile the
   operator configured, and returns an `LLMProvider` or `None`. It receives
   no kube client, UI handle, audit handle, or write executor.
4. Every call into a flow is guarded: a flow that raises disables itself,
   not the profiles it has nothing to do with.
5. korvid calls `LLMProvider.aclose()` when the provider is replaced or at
   shutdown. **Your adapter owns the injected `CredentialSource`**: close it
   in `aclose()` (in a `finally` block) alongside any HTTP clients or other
   resources. Failure to close credentials leaks token-refresh HTTP sessions.

Failures stay bounded. The built-in names — `github-copilot`, `ollama`,
`openai-compat`, `openai`, `azure`, `vllm`, `github`, `anthropic`, `claude` —
are reserved and may not be claimed by a third party, and an unbuildable
profile disables the agent cleanly instead of crashing. A failure at startup
leaves korvid running with the agent off and the reason logged; a failure
during a live rebuild rejects the new provider and keeps the previous one
open. Factory, load, and
metadata errors become `ProviderPluginError` messages capped at 200 characters
so tracebacks and secrets do not leak.

## Operator checklist

Before shipping a plugin:

1. Confirm the backend truly needs a plugin instead of a profile.
2. Pick a unique provider name that does not collide with built-ins or another
   installed distribution after normalization.
3. Keep secrets out of a profile's `options`; use env vars and
   `CredentialSource`.
4. Test both startup and live reconfiguration paths.
5. Verify every emitted event matches the API 2 table above.
