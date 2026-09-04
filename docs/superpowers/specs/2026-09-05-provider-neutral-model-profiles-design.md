# Provider-Neutral Model Profiles Design

## Goal

Replace korvid's single, hard-coded provider configuration and CSP-oriented
setup wizard with named model connection profiles backed by a single provider
catalog and Pydantic AI's model transport layer, while preserving korvid's
fail-closed approval, audit, tool, and outbound-policy runtime.

## Problem

The current provider subsystem has multiple sources of truth:

- `core/config.py` stores one `agent_provider` plus provider-specific Ollama
  fields.
- `ui/widgets/agent_setup_screen.py` owns a fixed provider list, labels,
  aliases, defaults, and Azure/GitHub-specific transitions.
- `__main__.py` loads GitHub credentials and assembles Ollama options.
- `providers/registry.py` repeats built-in provider aliases and routes Azure
  API-key headers by provider name.
- `providers/configurator.py` repeats provider-specific model discovery.
- plugin metadata exposes `auth_methods`, but the setup wizard does not consume
  it and cannot configure installed plugins.

Consequently, adding a provider requires coordinated edits across core,
composition, providers, UI, docs, and tests. The `:ai` wizard does not read a
configured provider collection; it offers four compiled-in choices. The config
also supports only one active provider.

## Research and Library Decision

### Existing korvid runtime

korvid does not currently use a third-party agent framework. It owns:

- `NativeAgentEngine`
- `RequestGateway`
- `OutboundPolicy`
- `ToolHarness`
- `DefaultAgentSession`
- the `LLMProvider` boundary

These components enforce product-specific security and UI semantics. Replacing
them with another framework's agent loop would duplicate or bypass approval,
audit, evidence, request-size, and conversation-repair contracts.

### LiteLLM

LiteLLM provides one OpenAI-shaped SDK for 100+ providers, async streaming,
tool calls, error normalization, retries, fallbacks, and routing.

It is not selected as an embedded dependency because its SDK base currently
requires a broad stack including `openai`, `tiktoken`, `tokenizers`, `aiohttp`,
`pydantic`, `jsonschema`, and `boto3`. That conflicts with korvid's lightweight
base install and provider-specific optional-extra boundary. LiteLLM Gateway
remains supported as an ordinary OpenAI-compatible endpoint profile.

Official references:

- https://docs.litellm.ai/docs/
- https://docs.litellm.ai/docs/completion/stream
- https://docs.litellm.ai/docs/routing
- https://github.com/BerriAI/litellm

### OpenAI Agents SDK

The OpenAI Agents SDK has `Model`/`ModelProvider` abstractions and LiteLLM
integration, but its primary value is an agent runtime. Adopting it would
overlap korvid's existing engine and tool loop. It is not selected.

Official references:

- https://openai.github.io/openai-agents-python/models/
- https://github.com/openai/openai-agents-python

### Pydantic AI

Pydantic AI separates its agent runtime from a typed model/provider layer.
`pydantic-ai-slim` exposes:

- a common `Model` interface
- `request_stream()`
- typed model messages, tool definitions, stream events, usage, and profiles
- provider implementations selected by `provider:model` identifiers
- provider-specific extras for OpenAI, Anthropic, Google, Bedrock, Mistral,
  Groq, OpenRouter, and others
- custom OpenAI-compatible clients/endpoints

The slim package has a non-trivial core dependency set, but provider SDKs stay
separate. It is selected only inside the `[agent]` optional boundary and only
for the model transport layer. korvid's agent runtime remains authoritative.

Official references:

- https://pydantic.dev/docs/ai/models/overview/
- https://pydantic.dev/docs/ai/models/openai/
- https://github.com/pydantic/pydantic-ai/tree/main/pydantic_ai_slim

### aisuite and `llm`

Both offer useful multi-provider access and plugins. aisuite is a smaller
OpenAI-shaped client; `llm` is primarily a CLI/database/plugin ecosystem. Their
embedded async stream/tool contracts and provider-profile metadata are less
aligned with korvid than Pydantic AI's typed model layer. Neither is selected.

## Architectural Principles

1. **Profiles, not CSP choices.** Users choose named connections such as
   `production`, `local`, or `company-ai`.
2. **One source of truth.** Config, UI, validation, discovery, and runtime use
   the same profile and adapter descriptor contracts.
3. **Provider details stay below the agent boundary.** UI and core never branch
   on `azure`, `aws`, `gcp`, `ollama`, or `github-copilot`.
4. **Korvid owns safety.** Model libraries never execute cluster tools,
   approve writes, write audit entries, or own conversation policy.
5. **Provider dependencies remain optional.** The base TUI imports without
   Pydantic AI or provider SDKs.
6. **One runtime path.** Legacy config may be read, but it is normalized into
   the new profile domain before wiring. No parallel legacy agent runtime is
   retained.
7. **Migration ends in deletion.** Existing built-in transports and duplicate
   catalogs are removed after the standard adapter covers their contracts.

## Target Configuration

```yaml
agent:
  active: production
  profiles:
    production:
      model: anthropic:claude-sonnet-4-5
      auth:
        method: environment
        key: ANTHROPIC_API_KEY

    local:
      model: ollama:llama3
      endpoint: http://localhost:11434
      auth:
        method: none
      options:
        num_ctx: 16384
        temperature: 0

    company-ai:
      model: openai:gpt-4o
      endpoint: https://ai.company.example/v1
      auth:
        method: environment
        key: COMPANY_AI_KEY

    company-azure:
      model: azure:gpt-4o
      endpoint: https://example.openai.azure.com
      auth:
        method: provider-default
      options:
        api_version: "2025-04-01-preview"
```

### Profile names

- Profile names are operator-defined identifiers.
- Names are non-empty, at most 100 characters, and contain only ASCII letters,
  digits, `.`, `_`, or `-`.
- Names are not normalized. `prod-east` and `prod_east` are distinct config
  keys, avoiding silent selection of the wrong connection.
- `agent.active` must exactly name an entry in `agent.profiles`.

### Model references

`model` uses Pydantic AI's `provider:model` convention. The prefix selects an
installed model adapter; the suffix is passed to that adapter as the model
identifier.

The UI does not present the prefix as a cloud vendor choice. Existing profiles
are shown by profile name. The add-profile flow asks for an installed adapter
only after the operator chooses to create a connection.

### Authentication

Core config treats `auth.method` and its remaining fields as bounded,
copy-owned configuration. It does not interpret provider-specific methods.

The common methods are:

- `none`
- `environment` with `key`
- `keyring` with `key`
- `provider-default`
- `device-login`

An adapter descriptor declares which methods it supports and validates the
method-specific fields. Secrets themselves are never stored in YAML.

`provider-default` delegates to the provider SDK's standard credential chain,
such as AWS credentials, Google application-default credentials, or Azure
DefaultAzureCredential. This avoids CSP-specific credential branches in core
and UI.

## Core Domain

`core/config.py` defines provider-neutral immutable dataclasses:

```python
@dataclass(frozen=True)
class AgentAuthConfig:
    method: str
    settings: Mapping[str, object]


@dataclass(frozen=True)
class AgentProfileConfig:
    model: str
    endpoint: str | None
    auth: AgentAuthConfig
    options: Mapping[str, object]


@dataclass(frozen=True)
class AgentProfilesConfig:
    active: str | None
    profiles: Mapping[str, AgentProfileConfig]
```

Mappings are recursively copy-owned and immutable at the public boundary.
`KorvidConfig` stores `agent_profiles: AgentProfilesConfig`. Core never imports
Pydantic AI, `providers/`, or provider identifiers.

## Legacy Configuration Migration

The old shape remains readable for one compatibility cycle:

```yaml
agent:
  provider: ollama
  model: llama3
  base_url: http://localhost:11434
```

When `agent.profiles` is absent and a legacy `agent.provider` exists, parsing
creates an in-memory profile named `default` and sets it active.

Legacy provider names are translated at this one parser boundary:

- `openai-compat` and its aliases become an appropriate OpenAI-compatible model
  reference.
- `ollama` becomes `ollama:<model>`.
- `azure` becomes `azure:<model>`.
- `github-copilot` becomes the GitHub Copilot extension's model reference.

This translation is isolated in one migration function. No downstream code
branches on the legacy provider.

The writer emits only the new profile shape. The first successful `:ai` save
therefore upgrades the file. If both shapes are present, `agent.profiles` wins
and a warning reports that legacy fields were ignored.

Compatibility accessors may temporarily derive old scalar values from the
active profile so intermediate PRs remain buildable. They are read-only,
deprecated, and removed in the final cleanup. There is never duplicate mutable
state.

## Public Agent Boundary

`agent/model_profiles.py` defines types that UI can consume without importing
providers:

```python
@dataclass(frozen=True)
class AuthMethodDescriptor:
    id: str
    display_name: str
    fields: tuple[SetupField, ...]


@dataclass(frozen=True)
class ModelAdapterDescriptor:
    id: str
    display_name: str
    auth_methods: tuple[AuthMethodDescriptor, ...]
    endpoint: EndpointRequirement
    supports_model_discovery: bool


class ModelAdapterCatalog(ABC):
    @abstractmethod
    def descriptors(self) -> tuple[ModelAdapterDescriptor, ...]: ...

    @abstractmethod
    async def list_models(self, profile: AgentProfileConfig) -> list[str]: ...

    @abstractmethod
    async def test(self, profile: AgentProfileConfig) -> str: ...

    @abstractmethod
    async def begin_auth(self, profile: AgentProfileConfig) -> DeviceLoginPrompt | None: ...

    @abstractmethod
    async def finish_auth(self, profile: AgentProfileConfig) -> None: ...
```

`SetupField` is a bounded declarative field schema, not executable plugin UI.
Supported field kinds are text, secret-reference, boolean, integer, and choice.
Plugins cannot mount arbitrary Textual widgets or execute during screen
composition.

## Provider Catalog

`providers/model_catalog.py` implements `ModelAdapterCatalog`.

It combines:

- built-in descriptors backed by installed Pydantic AI provider extras
- validated `korvid.provider` entry points
- the GitHub Copilot device-login extension

Descriptor IDs are unique after the existing provider-name normalization.
Duplicate or invalid descriptors fail closed with bounded errors. Unselected
plugins remain unloaded, preserving selected-only plugin loading.

The catalog is created in `__main__.py` and injected into the app/setup screen.
UI does not import `providers/`.

## Setup UI

The setup flow becomes:

1. List configured profile names.
2. Allow activate, edit, add, or remove.
3. For add/edit, resolve the profile's model prefix through the injected
   catalog.
4. Render the adapter descriptor's endpoint, auth, and option fields.
5. Discover models when the descriptor supports it; otherwise accept typed
   model input.
6. Test the candidate profile.
7. Atomically rebuild the agent session.
8. Persist the new profile collection only after the runtime swap succeeds.

No screen branch checks a CSP or built-in provider ID. Device login is selected
because the descriptor exposes `device-login`, not because the UI sees
`github-copilot`. Reconnect auth preselection compares the saved auth method to
the descriptor's auth-method IDs.

## Pydantic AI Transport Adapter

`providers/pydantic_model.py` implements korvid's `LLMProvider` over a Pydantic
AI `Model`.

Responsibilities:

- convert korvid's already-sanitized canonical messages into Pydantic AI
  `ModelMessage` values
- convert korvid tool schemas into Pydantic AI `ToolDefinition` values
- call `Model.request_stream()`
- translate text deltas, tool-call parts, completion, and usage into the
  existing provider event dictionaries consumed by `NativeAgentEngine`
- expose `ModelDescriptor` and conservative `ModelCapabilities`
- close owned provider clients

The adapter does not run tools or use Pydantic AI's `Agent`.

### Outbound boundary

The existing order remains:

1. provider message preparation
2. `OutboundPolicy` redaction/canonicalization/size enforcement
3. exact canonical request snapshot
4. Pydantic AI model transport conversion
5. network transmission

Transport conversion may encode the canonical request into a provider-native
wire format but may not add user-controlled prompt text or tool definitions.
Tests compare the canonical parts entering the adapter with the snapshot.

The adapter emits `REQUEST_SENT` only after `request_stream()` successfully
enters the provider response context. Credential, DNS, TLS, or connection
failures before that point must leave the previous outbound snapshot unchanged,
matching the current contract.

### Instrumentation

Pydantic AI instrumentation and Logfire integration are disabled by default.
No provider callback may receive prompts, tool arguments, responses, or secrets
unless a future explicit, separately reviewed observability feature enables it.

### Capabilities

Pydantic AI model profiles are translated only where the source directly
asserts an equivalent fact. Unknown facts remain `None`. korvid does not infer
tool support, context windows, or reasoning support from provider or model
names.

## Dependency Layout

The base installation remains unchanged and imports no Pydantic AI modules.

Proposed extras:

```toml
agent = [
  "httpx>=0.27",
  "keyring>=25.7.0",
  "pydantic-ai-slim>=2.39,<3",
]
provider-openai = ["pydantic-ai-slim[openai]>=2.39,<3"]
provider-anthropic = ["pydantic-ai-slim[anthropic]>=2.39,<3"]
provider-google = ["pydantic-ai-slim[google]>=2.39,<3"]
provider-bedrock = ["pydantic-ai-slim[bedrock]>=2.39,<3"]
```

The floor is the current production/stable Pydantic AI release evaluated by
this design. The `<3` cap protects the model/stream adapter boundary from an
unreviewed major-version contract change. Compatibility is verified against
Python 3.11, 3.12, and 3.13 before the dependency commit is accepted.

Pydantic AI 2.39 uses `httpx2` for clients it owns while temporarily accepting
legacy `httpx.AsyncClient`. `httpx2` is a separate import/package and can
coexist with korvid's existing `httpx` connectors. The new model adapter uses
provider-owned `httpx2` clients rather than passing korvid's legacy client,
avoiding Pydantic AI's legacy-client deprecation path. Existing non-model
connectors migrate separately only if their own requirements justify it.

No dependency is added in the profile-domain commit group.

The existing `entra` extra is retained only until the Pydantic Azure provider
migration is complete, then removed if no remaining extension imports it.

## Security Invariants

- Agent write tools always pass the existing approval gate.
- Audit logging remains fail-closed and outside the model/provider layer.
- Model libraries never receive Kubernetes clients, `WriteOps`, approval
  callbacks, or audit handles.
- The outbound policy runs for every request before provider handoff.
- Config stores references to secrets, never secret values.
- Plugin descriptors are data-only, bounded, validated, and copy-owned.
- Unselected provider plugins are not imported.
- Missing provider extras produce an actionable install hint and do not fall
  back to another provider.
- An unknown active profile, unknown model adapter, invalid auth method, or
  invalid provider options prevents agent construction; the TUI may start with
  the agent disabled and a visible warning.
- Provider errors are surfaced; no success-shaped fallback is added.
- Automatic multi-provider fallback is not enabled. A fallback that can move
  prompts across trust boundaries requires a separate explicit design.

## Error Handling

Errors are classified at boundaries:

- config shape errors: bounded startup warnings or migration errors following
  existing config policy
- missing adapter extra: fixed install hint
- invalid descriptor/plugin: bounded `ProviderPluginError`
- credential failure: provider construction/test failure without session swap
- stream protocol violation: existing provider-contract error path
- profile persistence failure after a successful runtime swap: existing
  applied-now/reverts-on-restart warning

No broad catch is added around runtime construction. Best-effort model listing
may still return an empty list because typed model entry is the documented
fallback; it logs through the existing fixed-message path.

## Delivery Strategy

This subsystem replacement is delivered as **one pull request**. Config,
catalog, setup UI, transport, and legacy deletion are one architectural change;
splitting them across long-lived PRs would obscure the reason for the migration
and leave transitional compatibility seams looking permanent.

The pull request uses the following ordered commit groups. Each group is
individually testable and reviewable, while the final PR is accepted only when
all groups are present and the legacy paths are deleted.

The PR is opened as a draft after the config-domain group so GitHub CI and the
complete diff remain visible throughout development. It is marked ready only
after the final cleanup, full repository gate, dependency review, and review
rounds succeed.

### Commit group 1: Named profile config domain

- Add immutable profile/auth dataclasses.
- Parse the new shape.
- Migrate the legacy shape into an in-memory `default` profile.
- Write only the new shape.
- Add derived compatibility accessors for existing wiring.
- Do not add Pydantic AI.

This group changes config behavior but not provider transport or UI.

### Commit group 2: Unified descriptor catalog

- Add public agent descriptor/catalog contracts.
- Implement built-in descriptors from current behavior.
- Adapt plugin metadata into the same catalog.
- Inject the catalog from `__main__.py`.

The existing provider transports remain temporarily behind the catalog.

### Commit group 3: Profile-driven setup UI

- Replace the fixed provider choice with profile management.
- Render setup fields from descriptors.
- Remove `_DEFAULTS`, `_PROVIDER_LABELS`, UI aliases, and all Azure/GitHub
  provider-ID branches.
- Preserve atomic runtime swap and persistence behavior.

### Commit group 4: Pydantic AI model transport

- Add pinned `pydantic-ai-slim`.
- Implement and contract-test the transport adapter.
- Add provider-specific extras and missing-extra hints.
- Migrate OpenAI-compatible, Azure, Anthropic, Google, Bedrock, and Ollama
  model references through the standard model layer.

### Commit group 5: GitHub Copilot extension

- Express device login and Copilot model discovery through the catalog contract.
- Keep its token store and protocol adapter isolated in `providers/`.
- Ensure UI contains no Copilot-specific branch.

### Commit group 6: Legacy deletion and documentation

- Remove old provider scalar compatibility accessors.
- Remove duplicate built-in registries and aliases.
- Remove replaced OpenAI-compatible/Ollama transports and configurator request
  paths.
- Remove provider-specific core config fields.
- Remove obsolete extras and tests.
- Update operator docs and release migration notes.

Each commit group runs its focused tests and static checks. The complete branch
runs the full repository gate after every review-driven fix. The maintainer
merges the single final PR; automation never merges.

## Testing Strategy

### Config contracts

- multiple profiles round-trip
- exact active-profile selection
- immutable/copy-owned nested mappings
- invalid/missing active profile
- legacy-to-default normalization
- new shape wins over legacy with warning
- writer removes legacy managed fields and preserves unrelated config

### Catalog contracts

- descriptor validation and bounded fields
- selected-only plugin loading
- duplicate adapter rejection
- missing-extra hints
- auth-method/field validation

### UI contracts

- list, activate, add, edit, remove profiles
- descriptor-driven auth/endpoint fields
- reconnect preselection without provider-ID branches
- unknown/unavailable adapter messaging
- model discovery and typed fallback
- atomic apply/save/retry behavior

### Transport contracts

- async text streaming
- fragmented and parallel tool calls
- usage extraction
- request-sent timing
- cancellation closes stream/client
- malformed stream rejection
- provider errors do not become successful completion
- canonical outbound snapshot equivalence
- no instrumentation/callback leakage

### Import/dependency contracts

- base TUI imports without Pydantic AI
- each provider extra imports independently
- missing extras fail only when selected
- Python 3.11/3.12/3.13 and Windows matrix
- dependency graph and optional-extra import tests

## Non-Goals

- Replacing `NativeAgentEngine` or `ToolHarness`.
- Moving approval or audit behavior into Pydantic AI.
- Automatic fallback between profiles/providers.
- A hosted LiteLLM gateway bundled with korvid.
- Storing API keys or cloud credentials in config.
- Executable third-party setup UI plugins.
- Inferring cloud model provider from the Kubernetes cluster CSP.
- Reusing `k8s/csp.py` detection to choose an LLM profile.
