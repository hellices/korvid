# Provider plugins

korvid routes every model reference through LiteLLM's bundled catalog, so over
2,000 models work out of the box. **You almost certainly do not need a
plugin.** Write one only when your backend's wire protocol is not
OpenAI-compatible, or when your auth is genuinely non-standard — an
OpenAI-compatible backend at any URL needs a profile `endpoint`, and unusual
auth alone needs a `korvid.credential` chain (below).

> **Security warning:** these are trusted, in-process Python loaded into the
> korvid process. Selected-only loading avoids importing *unused* plugins, but
> it is **not** a sandbox — install only what you trust. A flow is constructed
> from a profile and an optional `CredentialSource`, never from conversation
> data, and what it returns is called with the same sanitized canonical
> `messages`/`tools` payload `OutboundPolicy` builds for every other transport
> (see [`docs/threat-model.md`](threat-model.md)). Once your `complete()` has
> that payload it may mutate, retain, log or transmit it anywhere. See
> [`SECURITY.md`](https://github.com/hellices/korvid/blob/main/SECURITY.md) to report a vulnerability.

## Special flows (the current extension point)

The `korvid.provider` entry-point group loads **`SpecialFlow`** declarations.
A `SpecialFlow` is a frozen dataclass — *data*, not a base class to subclass —
claiming exactly one reference prefix (or one named boolean option on a prefix
it otherwise shares) and supplying only what the standard transport
structurally cannot.

An entry point may resolve to a `SpecialFlow` instance, or to a module exposing
a zero-argument `korvid_special_flows()` returning `SpecialFlow` objects, of
which korvid takes the first. The entry-point *name* is the claimed prefix; a
flow whose own `prefix` normalizes differently is refused.

```python
# my_pkg/flow.py
from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    EndpointRequirement,
    ModelConnectionConfig,
    SpecialFlow,
)
from korvid.agent.provider import LLMProvider


def build_provider(profile: ModelConnectionConfig) -> LLMProvider | None: ...


def korvid_special_flows() -> tuple[SpecialFlow, ...]:
    return (
        SpecialFlow(
            prefix="my-backend",
            display_name="My Backend",
            auth_methods=(AuthMethodDescriptor(id="none", display_name="No key"),),
            endpoint=EndpointRequirement.OPTIONAL,
            build_provider=build_provider,
        ),
    )
```

```toml
[project.entry-points."korvid.provider"]
my-backend = "my_pkg.flow"
```

Every field:

| Field | Meaning |
|---|---|
| `prefix` | The claimed prefix; must match `[a-z0-9][a-z0-9_-]*`. |
| `display_name` | What the wizard shows. |
| `auth_methods` | Methods offered for this prefix. A non-empty tuple **replaces** the catalog's generic list for *every* reference under the prefix, including ones this flow does not serve — korvid's Ollama flow declares `()` for that reason. |
| `option_fields` | Declarative `SetupField` prompts (`TEXT`, `SECRET_REF`, `BOOLEAN`, `INTEGER`, `CHOICE`) — data, never executable UI. |
| `endpoint` | `REQUIRED`, `OPTIONAL` (default) or `UNSUPPORTED`: whether the wizard must, may, or must not ask for one. |
| `claims_option` | A boolean option activating the flow on a prefix it shares; `None` means it owns the prefix outright. |
| `build_provider` | `(ModelConnectionConfig) -> LLMProvider \| None`. `None` for the field itself means *declaration only*: the reference is still kept from the standard transport, but nothing can be built, so the factory refuses instead of routing. |
| `begin_auth` | `async (profile) -> DeviceLoginPrompt \| None`, starting the flow's sign-in. `None` means the wizard skips the stage rather than inventing one. |
| `finish_auth` | `async (profile) -> str \| None`, returning the *credential key* the profile will name — never the secret. |

`korvid.providers.flow_ollama_thinking` ships the option form,
`korvid.providers.flow_copilot` the prefix form.

### Prefix claiming, normalization, and the exclusive names

A prefix is normalized by stripping surrounding whitespace, lowercasing and
folding `_` to `-` — and nothing else. A dot is **not** folded: `company.llm`
fails the `[a-z0-9][a-z0-9_-]*` pattern and is rejected outright, while
`Company_LLM` and `company-llm` normalize alike and collide, so the second
declaration is rejected rather than silently shadowing the first.

Three sets are enforced whatever the transport publishes, and they are not
interchangeable:

- **Never routable** — the retired aliases `openai-compat`, `vllm`, `github`,
  `claude`, plus the device-login prefixes `github-copilot` and `chatgpt`.
  These stay claimed even with no flow to serve them — a retired alias an
  operator still reads as korvid's own must stay unroutable, and a
  device-login prefix starts an interactive sign-in inside the SDK's own
  routing call (see the
  [threat model](threat-model.md#the-agent-extra-dependencies-and-lockdown)).
- **korvid's own routes** — `openai`, `azure`, `anthropic` and `ollama`. Fully
  routable (`openai/gpt-4o` stays dispatchable), but unregistrable by a third
  party. Held statically so a vendor release dropping a row from
  `models_by_provider()` cannot hand an operator's prefix to whoever registered
  the entry point. korvid's own distribution may still declare them.
- **LiteLLM's dynamic catalog** — every prefix `models_by_provider()` publishes
  at startup. A third party cannot shadow a name the SDK ships natively; korvid's
  own distribution is still exempt.

A rejected entry-point name is reported at startup, not fatal: the registry
collects the reason for the setup UI's banner and korvid starts normally.

### Selected-only loading, and what a failure costs

Construction reads entry-point **names only** and imports nothing. A name is
loaded the first time a reference resolving to it is claimed, and only that one.

A load that raises is a logged warning and a setup-banner line, never a startup
exception; the result is memoized so a repeated claim neither reloads nor
re-reports. A prefix the standard transport **also** publishes falls back to
being routed; one nothing else can serve stays claimed and refused. An
option-only flow shares its prefix, so ordinary references under it stay
routable either way.

### Option claiming

A flow receives its profile's `options`, and `claims_option` activates it only
when that key is **exactly `True`** — not merely truthy, so a quoted `"true"`
cannot silently switch transports. The secret policy below applies first.

### Credential chains for `provider-default`

A backend whose *transport* korvid already speaks but whose *credential* it
does not may declare a chain instead of a flow: a prefix, a display name, a
`resolve` callable returning transport call parameters carrying a *refreshing*
credential (never a resolved secret), and an optional `requires_extra` used
only in refusal messages.

```toml
[project.entry-points."korvid.credential"]
company-llm = "acme_korvid_provider.credentials"
```

```python
# acme_korvid_provider/credentials.py
from korvid.providers.provider_default import (
    CredentialUnavailable,
    ProviderDefaultCredential,
    ResolvedCredential,
)


def _resolve() -> ResolvedCredential:
    if not _signed_in():
        raise CredentialUnavailable("run `acme login` first")
    return ResolvedCredential(parameters={"azure_ad_token_provider": _token})


def korvid_provider_default_credentials() -> tuple[ProviderDefaultCredential, ...]:
    return (ProviderDefaultCredential("company-llm", "Company SSO", _resolve),)
```

korvid consults a chain only when the profile says `auth.method: provider-default`,
so an installed package cannot change authentication behind the operator's back —
and never on a name korvid ships. When `resolve` raises `CredentialUnavailable`,
korvid refuses the profile at build time, quoting that message. korvid's own
Entra ID chain for `azure` is declared this way.

## The `ProviderPlugin` compatibility path

`ProviderPlugin` / `ProviderPluginConfig` (the pre-flow API) **is not wired in
current builds**: korvid loads `korvid.provider` entry points as `SpecialFlow`
declarations, and a `ProviderPlugin` class registered there is never
instantiated. What the surface below describes — the event contract, the
options limits and secret policy, `LLMProvider`, `ModelCapabilities` — is still
what a provider object must satisfy, because `SpecialFlow.build_provider`
returns one. Only construction changed: publish a `SpecialFlow` whose `prefix`
is your entry-point name, move the body of `ProviderPlugin.create` into a
`build_provider` function, and point the entry point at its module.

Third-party prefixes are configured by hand; the `:ai` wizard does not discover
them:

```yaml
agent:
  active: company
  profiles:
    company:
      model: company-llm/cluster-brain
      endpoint: https://llm.example.internal
      auth: {method: environment, key: COMPANY_LLM_TOKEN}
      options: {tenant: platform}
```

## API 2: exact public surface

Declaration types come from `korvid.agent.model_profiles` (`SpecialFlow`,
`AuthMethodDescriptor`, `SetupField`, `SetupFieldKind`, `EndpointRequirement`,
`ModelConnectionConfig`, `DeviceLoginPrompt`); the provider contract from
`korvid.agent.credentials`, `korvid.agent.model_policy` and
`korvid.agent.provider`; a credential chain's from
`korvid.providers.provider_default`; and the retired, unwired plugin contract
from `korvid.agent.provider_plugin` — `ProviderPluginMetadata` (`api_version`
must equal `PROVIDER_PLUGIN_API_VERSION`, exactly `2`), `ProviderPluginConfig`
(whose `base_url` field is that API's own spelling, not a profile key) and
`ProviderPlugin`.

`LLMProvider` has no `name` property. It has `descriptor` and `capabilities`
properties — both validated the moment korvid wraps it — plus
`async complete(messages, tools, *, stream=True)` and `async aclose()`.
`ModelCapabilities` carries `context_window_tokens`, `supports_tools`,
`supports_parallel_tools`, `supports_reasoning`, `recommended_tier` and a
`provenance` mapping, each fact independently unknown; reporting nothing is
valid — `ModelCapabilities.unknown()` falls back to korvid's catalog, then to
`low`. `supports_tools=False` is a **hard stop** (korvid refuses to start the
agent rather than route a model that cannot call tools),
`supports_parallel_tools` is honored only on `high`, `recommended_tier` loses
to an explicit `agent.model_tier`, and `provenance` must map a known fact name
to a `CapabilitySource`.

Two shape traps: `complete()` must be an **async generator**, not a coroutine
returning an iterator; and `prepare_messages()`, the built-in dialect hook, is
**never** called on a provider korvid did not build. Adapt inside `complete()`,
and treat what you add there as leaving korvid's inspected boundary.

## The provider a flow returns

`build_provider` returns an `LLMProvider` built from the profile it is handed:
`profile.endpoint`, `profile.model` and `profile.options`. There is no
`base_url` key — a profile names its host in `endpoint` — and one that does not
carry what the backend needs is refused by returning `None` or by raising with
an operator-facing message.

```python
import httpx

from korvid.agent.credentials import CredentialSource
from korvid.agent.model_policy import CapabilitySource, ModelCapabilities, ModelDescriptor
from korvid.agent.provider import LLMProvider


class CompanyProvider(LLMProvider):
    def __init__(self, *, endpoint: str, model: str, creds: CredentialSource | None) -> None:
        self._endpoint, self._model, self._creds = endpoint.rstrip("/"), model, creds
        self._client = httpx.AsyncClient(timeout=30.0)

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("company-llm", self._model)

    @property
    def capabilities(self) -> ModelCapabilities:  # omitted facts stay unknown
        return ModelCapabilities(
            supports_tools=True,
            provenance={"supports_tools": CapabilitySource.PROVIDER},
        )

    async def complete(  # an async generator, never a coroutine
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
        *, stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        reply = await self._client.post(
            f"{self._endpoint}/chat",
            json={"model": self._model, "messages": messages, "tools": tools},
            headers=await self._creds.headers() if self._creds else {},
        )
        reply.raise_for_status()
        yield {"type": "text_delta", "text": reply.json()["text"]}
        yield {"type": "done"}

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        finally:
            if self._creds is not None:
                await self._creds.aclose()
```

## Event contract and exact limits

korvid wraps a provider it did not build in `ValidatedPluginProvider`, which
enforces and normalizes the event stream. Events must be mappings shaped like
one of these:

| Event | Required keys | Exact bounds |
|---|---|---|
| `text_delta` | `{"type": "text_delta", "text": str}` | `text` max **65,536 UTF-8 bytes** |
| `tool_call` | `{"type": "tool_call", "id": str, "name": str, "arguments": str}` | `id` and `name` non-empty, max **256** chars; `arguments` max **65,536 UTF-8 bytes** |
| `usage` | `{"type": "usage", "input_tokens": int, "output_tokens": int}` | each count a non-bool int in **0..1,000,000,000** |
| `done` | `{"type": "done"}` | no payload; exactly one terminal done |

Extra keys are discarded. Unknown types, non-mapping payloads, missing fields,
overlong strings or out-of-range token counts raise
`ProviderPluginContractError`. `tool_call.arguments` is a **string**, typically
JSON-encoded, not a nested mapping, and `ValidatedPluginProvider.aclose()`
forwards to your provider once — duplicate closes are swallowed.

These four are the whole contract. korvid's own transport yields one extra
internal event so the `:ai payload` inspector can tell a request that reached
the wire from one that never did; it is not part of API 2, and a third party
yielding it is rejected like any unknown type. Your request is recorded when
you yield your **first** event.

## Options contract, immutability, and secret policy

A profile's `options` is the only flow-specific config bag, and it accepts only
JSON-like values — `null`, `bool`, `int`, finite `float`, `str`, `list` and
nested mappings with ASCII string keys — within exact parser limits: max depth
**4**, max **64** mapping keys and **64** list items overall, **2048 UTF-8
bytes** per string, and a **16384-byte** serialized JSON budget.

Secret-looking keys are rejected before a flow sees them. The reserved segments
are exactly `secret`, `password`, `token`, `api_key` (and `apikey`),
`authorization` and `credential`; CamelCase keys are split at word boundaries
first, so `apiKey`, `clientSecret` and `clientAPIKey` are all rejected. Keep
secrets in environment variables and pass the name via `auth.key`.

Treat `options` as read-only and accept both `list` and `tuple` for sequences:
live wizard and reconnect flows deep-freeze nested mappings and convert lists to
tuples; startup from `config.yaml` preserves YAML lists.

## Lifecycle and compatibility

1. korvid reads the entry-point **names** at startup and imports nothing; a
   module loads the first time a reference resolves to its name.
2. A flow owns its `prefix` (or a named option on it), and a reference under a
   prefix it owns is never routed, so it cannot be silently bypassed.
3. `build_provider(profile)` returns an `LLMProvider` or `None`, and receives no
   kube client, UI handle, audit handle or write executor.
4. Every call into a flow is guarded: a failure disables that flow, not
   unrelated profiles, and the reason reaches the setup banner.
5. korvid calls `LLMProvider.aclose()` when the provider is replaced or at
   shutdown. **Your provider owns the injected `CredentialSource`**: close it
   in `aclose()` in a `finally` block, or you leak token-refresh sessions.

Failures stay bounded. An unbuildable profile disables the agent cleanly:
startup keeps korvid running with the agent off and the reason logged, and a
live rebuild keeps the previous provider. Load and construction errors become
`ProviderPluginError` messages capped at 200 characters.

## Operator checklist

Confirm the backend truly needs a flow rather than a profile `endpoint`; pick a
prefix colliding with no reserved name after normalization; keep secrets out of
`options`; test startup *and* live reconfiguration; and verify every emitted
event matches the table above.
