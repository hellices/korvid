# Provider-Neutral Model Profiles Design

## Goal

Replace korvid's single hard-coded provider configuration and CSP-oriented
setup wizard with **named model connection profiles** whose provider and model
selection is **data-driven** — resolved from a maintained model/provider
dataset and a standard multi-provider SDK — rather than from a hand-written
adapter table or a cloud-vendor picker.

korvid's own runtime is preserved without exception: `NativeAgentEngine`,
`RequestGateway`, `OutboundPolicy`, `ToolHarness`, the approval gate, the audit
log, conversation repair and the evidence/outbound-snapshot contract all stay
exactly where they are. Only the transport *below* the `LLMProvider` boundary,
and the *data* that drives selection, change.

## Problem

The current provider subsystem has multiple hand-maintained sources of truth:

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

Adding a provider therefore requires coordinated edits across core,
composition, providers, UI, docs and tests. The `:ai` wizard does not read a
configured provider collection; it offers four compiled-in choices. The config
supports only one active provider.

### The requirement this design answers

An earlier revision of this design replaced that hand-written table with a
*different* hand-written table: a `BUILTIN_ADAPTERS` mapping of adapter id →
Pydantic AI provider class → optional extra, one `provider-*` extra per vendor,
one `_build_<vendor>_model` branch per vendor, and a wizard that asked "which
adapter?" before it asked anything else. That is the same shape of coupling
with new names. Every new vendor still meant a korvid source edit, a new extra,
a new branch, a new test.

**Provider and model selection must be data-driven and standard.** korvid must
not ship, maintain, or dispatch on a compiled-in list of cloud vendors. The
list of providers and models must come from data korvid *reads*, and the
routing to a provider must be performed by a library korvid *calls* — not by a
`if provider == ...` chain korvid owns.

## Research and Library Decision

### Existing korvid runtime

korvid does not use a third-party agent framework. It owns `NativeAgentEngine`,
`RequestGateway`, `OutboundPolicy`, `ToolHarness`, `DefaultAgentSession` and the
`LLMProvider` boundary. These enforce product-specific security and UI
semantics. Replacing them with another framework's agent loop would duplicate
or bypass approval, audit, evidence, request-size and conversation-repair
contracts. They are **out of scope** for this change.

### LiteLLM — selected

LiteLLM is one OpenAI-shaped SDK covering 100+ providers. Verified locally
against an installed **litellm 1.98.0** (MIT):

| Fact | Value |
| --- | --- |
| `litellm.model_list` | the shipped model-id list (thousands of ids) |
| `litellm.provider_list` | the shipped provider enum (`LlmProviders`, a `str` enum) |
| `litellm.models_by_provider` | provider → model ids (**values are heterogeneous: mostly `set`, a handful of `list`**) |
| Catalog yield | every provider×model candidate, minus the entries whose `mode` is not `chat` |
| `litellm.model_cost` | per-model cost/capability records, keyed **both** bare (`claude-sonnet-4-5`) and provider-qualified (`ollama/codegemma`) |
| `litellm.get_model_info(model=...)` | context window, output cap, `supports_function_calling`, `supports_vision`, `mode` |
| `litellm.get_supported_openai_params(model=..., custom_llm_provider=...)` | per-provider parameter allowlist |
| `litellm.get_llm_provider(model=...)` | `(model, provider, dynamic_api_key, dynamic_api_base)` |
| `litellm.acompletion(...)` | common args `model`, `messages`, `stream`, `tools`, `tool_choice`, `api_key`, `base_url`, `api_version`, `timeout`, `stream_options`, `extra_headers`, plus `**kwargs` |
| Stream types | `CustomStreamWrapper` yielding `ModelResponseStream` → `StreamingChoices` → `Delta`; `Usage` on the final chunk |
| **Import-time network call** | `import litellm` fetches the remote model-cost map over HTTPS *unless* `LITELLM_LOCAL_MODEL_COST_MAP` is `true` in the environment **before** the import |

No exact table size is recorded here, and none is asserted in a test. The
sizes differ between the bundled data and the remote map, and they change
with every LiteLLM patch release; a number written down here is a number that
is wrong by the next upgrade. What the design depends on is the *shape* of
these tables, not their cardinality.

This is exactly the data-driven substrate the requirement asks for: the
provider list, the model list, the per-model capability facts and the routing
decision are all **library data and library calls**, not korvid source.

**The cost, stated plainly.** LiteLLM's base install pulls 55 distributions,
including `boto3`/`botocore`/`s3transfer`, `openai`, `tiktoken`, `tokenizers`,
`huggingface_hub`, `aiohttp`, `pydantic`, `pydantic-settings`, `jsonschema`,
`jinja2` and `fastuuid`. That is heavier than any dependency korvid has taken
before. It is accepted **only inside the `[agent]` optional extra**, and the
base TUI install remains unchanged and provably free of it (`tests/
test_optional_extras.py` pins the import graph). The tradeoff is documented for
operators in `docs/agent.md` and `docs/airgap.md`: one extra brings a large but
single, MIT-licensed, widely-audited stack, and in exchange korvid ships zero
per-vendor code.

LiteLLM's own `httpx` floor is `>=0.28,<1.0` — the same `httpx` family korvid
already uses for its observability connectors and Copilot device login, so no
second HTTP client flavour is introduced.

**What korvid deliberately does not use from LiteLLM:** `Router`, retries,
fallbacks, cooldowns, the proxy server, budget/spend tracking, caching,
guardrails and every observability callback integration. Those either move
prompts across trust boundaries, hide failures behind success-shaped
responses, or ship prompts to a third party. korvid uses `acompletion` and the
static catalog data. Nothing else.

Official references:

- https://docs.litellm.ai/docs/
- https://docs.litellm.ai/docs/completion/stream
- https://docs.litellm.ai/docs/set_keys
- https://github.com/BerriAI/litellm

### models.dev — selected, as bounded optional enrichment

models.dev publishes a single public JSON document
(`https://models.dev/api.json`, MIT, CORS-open, ETag-served, ~4.5 MB) keyed by
provider id, each carrying `name`, `doc`, `env` (credential env-var names),
`api` (default endpoint) and a `models` map whose entries carry `name`,
`family`, `reasoning`, `tool_call`, `attachment`, `structured_output`,
`release_date`, `last_updated`, `modalities`, `limit.context`, `limit.output`
and `cost`.

It is **metadata only**. It has no Python SDK, and korvid deliberately does not
want one: korvid fetches the document over plain HTTP, validates it against a
narrow schema, and caches it. It never carries credentials or prompts, and it
**never controls routing** — a model that models.dev knows and LiteLLM does not
is still not connectable, and a model LiteLLM can route that models.dev has
never heard of is still fully usable.

Official references:

- https://models.dev/
- https://models.dev/api.json
- https://github.com/sst/models.dev

### Pydantic AI — rejected

Pydantic AI's model layer is well typed, but its provider surface is a
**per-vendor class hierarchy** (`OpenAIProvider`, `AzureProvider`,
`AnthropicProvider`, `GoogleProvider`, `BedrockProvider`, …) published behind
**per-vendor extras**. Consuming it means korvid maintains a mapping of adapter
id → provider class → extra name → construction branch, which is precisely the
hand-written adapter table this design exists to remove. It also has no
model-level capability dataset comparable to `model_cost`/`get_model_info`, so
a catalog would still have to be hand-written.

### OpenAI Agents SDK — rejected

Its primary value is an agent runtime that would overlap korvid's engine and
tool loop. Its model layer defers to LiteLLM anyway.

### aisuite and `llm` — rejected

aisuite is a smaller OpenAI-shaped client with a much narrower provider set and
no capability dataset. `llm` is primarily a CLI/database/plugin ecosystem whose
async streaming and tool contracts are not aligned with korvid's engine.

## Architectural Principles

1. **Profiles, not CSP choices.** Users choose named connections such as
   `production`, `local`, or `company-ai`.
2. **Selection is data-driven.** The set of providers and models korvid offers
   is read from LiteLLM's shipped tables (primary) and optionally enriched from
   models.dev; korvid never compiles a vendor list into its source.
3. **Routing is delegated.** `litellm.get_llm_provider` decides which provider a
   model reference belongs to and `litellm.acompletion` performs the call.
   korvid owns no provider class table and no per-vendor construction branch.
4. **Provider details stay below the agent boundary.** UI and core never branch
   on `azure`, `aws`, `gcp`, `ollama`, or `github-copilot`.
5. **Korvid owns safety.** Model libraries never execute cluster tools, approve
   writes, write audit entries, or own conversation policy.
6. **Provider dependencies remain optional.** The base TUI imports without
   `litellm`.
7. **One runtime path.** Legacy config may be read, but it is normalized into
   the new profile domain before wiring. No parallel legacy agent runtime is
   retained.
8. **Migration ends in deletion.** Existing built-in transports and duplicate
   catalogs are removed after the LiteLLM transport covers their contracts.
9. **Extensions are the exception, and they are declarative.** A flow LiteLLM
   cannot own may register through the existing entry-point mechanism as
   bounded data. It is never a built-in provider list.

## Target Configuration

```yaml
agent:
  active: production
  profiles:
    production:
      model: anthropic/claude-sonnet-4-5
      auth:
        method: environment
        key: ANTHROPIC_API_KEY

    local:
      model: ollama/llama3
      endpoint: http://localhost:11434
      auth:
        method: none
      options:
        num_ctx: 16384
        temperature: 0

    company-ai:
      model: openai/gpt-4o
      endpoint: https://ai.company.example/v1
      auth:
        method: environment
        key: COMPANY_AI_KEY

    company-azure:
      model: azure/gpt-4o
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

`model` uses the **standard `provider/model` form** — the same string LiteLLM
accepts, the same string models.dev publishes as a model id, and the same
string every LiteLLM-compatible tool in the ecosystem prints. `anthropic/
claude-sonnet-4-5`, `openai/gpt-4o`, `ollama/llama3`, `azure/gpt-4o`,
`bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0`.

A colon-separated `provider:model` form is **not** used. Slash is what the
ecosystem writes, and a colon collides with the colons that already appear
inside model identifiers (`qwen3:8b`, `…-v1:0`).

korvid does not parse the prefix to choose an implementation. It hands the
reference to `litellm.get_llm_provider` for validation and to
`litellm.acompletion` for execution.

Two shapes are accepted beyond a plain reference:

- A reference with no `/` is passed through unchanged; LiteLLM resolves it
  against its own default-provider rules and korvid records what it resolved
  to.
- A reference whose prefix names a registered **special flow** (below) is
  claimed by that flow before LiteLLM sees it.

### Authentication

Core config treats `auth.method` and its remaining fields as bounded,
copy-owned configuration. It does not interpret provider-specific methods.

The common methods are exactly:

- `none`
- `environment` with `key`
- `keyring` with `key`
- `provider-default`
- `device-login`

These are **generic** and map onto LiteLLM's common call arguments rather than
onto vendor code:

| Method | Resolution | Handed to `acompletion` as |
| --- | --- | --- |
| `none` | no credential | `api_key=<keyless sentinel>`; **refused unless the profile names an explicit `endpoint`** (rule below) |
| `environment` | `os.environ[settings["key"]]` | `api_key=<value>` |
| `keyring` | `keyring.get_password("korvid", settings["key"])` | `api_key=<value>` |
| `provider-default` | nothing is read by korvid | **the `api_key` argument is not passed at all** — LiteLLM and the vendor SDK use their own environment/default credential chain (AWS profile chain, ADC, `AZURE_OPENAI_API_KEY`, Entra) |
| `device-login` | the registered special flow's token exchange | `api_key=<exchanged token>` plus the flow's `extra_headers` |

`provider-default` is the one method korvid deliberately does **not** implement:
delegating is the point. It is also the only method that lets an ambient
credential reach the wire, and the profile has to name it explicitly to get
that behaviour. Passing `api_key=None`, or a keyless sentinel, would *not*
delegate — the SDK would see an explicit value and stop consulting its own
chain — so the request plan carries a third state (“omit”) that is distinct
from “`None`”, and the argument is absent from the call keyword arguments
entirely. A test asserts `"api_key" not in kwargs` unconditionally.

### The `none`-auth rule, as data

"Public vendor host" is never a list of vendor names — that is precisely the
table this design removes. It is not inferred from routing either. The rule
reads one field of the operator's own profile:

> `none` is **allowed** only when `profile.endpoint` is a non-empty string. It
> is **refused** whenever `profile.endpoint is None`.

With no operator endpoint, the request goes wherever the SDK's own default
takes it, and a default the operator did not choose is by definition somebody
else's service. An operator-supplied `endpoint` means the operator chose the
host, so keyless is their call — that is the local vLLM, LM Studio and Ollama
case, and both the target configuration above and the legacy migration already
write an `endpoint` for exactly those profiles.

**Why not derive it from routing.** An earlier revision refused `none` only
when `litellm.get_llm_provider(model=reference)` returned a non-`None`
`dynamic_api_base`. Measured against litellm 1.98.0, that field is a *dynamic
override* populated for a handful of providers — not "LiteLLM knows a default
host":

| reference | `dynamic_api_base` | the old rule's verdict |
| --- | --- | --- |
| `openai/gpt-4o` | `None` | allowed — an unauthenticated POST to `api.openai.com` |
| `anthropic/claude-sonnet-4-5` | `None` | allowed |
| `azure/gpt-4o`, `gemini/*`, `bedrock/*` | `None` | allowed |
| `ollama/llama3` (no endpoint) | `http://localhost:11434` | refused — the one case the rule existed to allow |
| `groq/*`, `xai/*` | non-`None` | refused |

The old rule protected two providers and waved through every major one, while
refusing the local case. Setting `OPENAI_BASE_URL`/`OPENAI_API_BASE` does not
change the field either. That inversion is recorded here because the corrected
rule looks weaker than the one it replaces and is not: it is the one that
actually holds.

Because the rule reads only the profile, it needs no routing result. It is
evaluated **before** `litellm.get_llm_provider` is ever called, and the catalog
mirrors it **exactly** rather than approximately: `auth_methods(reference, *,
endpoint=...)` takes the endpoint the setup flow has already collected — the
endpoint stage runs before the auth-method stage — and omits `none` when it is
absent. There is no static provider table on either side and no margin at which
the two derivations can disagree, so the UI never offers a combination the
factory will refuse.

The cost, stated plainly: a keyless `ollama/llama3` profile must name
`endpoint: http://localhost:11434`. That is what the target configuration
above already writes, what the legacy migration already produces from
`base_url`, and what the setup flow already asks for first.

Secrets themselves are never stored in YAML — only references.

## Core Domain

`core/config.py` defines provider-neutral immutable dataclasses:

```python
@dataclass(frozen=True)
class AgentAuthConfig:
    method: str = "none"
    settings: Mapping[str, object] = field(default_factory=dict)
    settings_error: str | None = field(default=None, init=False, compare=False)


@dataclass(frozen=True)
class AgentProfileConfig:
    model: str
    endpoint: str | None = None
    auth: AgentAuthConfig = field(default_factory=AgentAuthConfig)
    options: Mapping[str, object] = field(default_factory=dict)
    options_error: str | None = field(default=None, init=False, compare=False)

    @property
    def config_error(self) -> str | None: ...


@dataclass(frozen=True)
class AgentProfilesConfig:
    active: str | None = None
    profiles: Mapping[str, AgentProfileConfig] = field(default_factory=dict)
    unparsed: Mapping[str, object] = field(default_factory=dict, compare=False)

    @property
    def active_profile(self) -> AgentProfileConfig | None: ...
```

Rules the implementation must hold:

- Mappings are recursively copy-owned and immutable at the public boundary.
  Freezing produces `MappingProxyType`/`tuple`; the writer must **thaw**
  recursively before serialising, or `yaml.safe_dump` raises `RepresenterError`.
- `options` and `auth.settings` go through the *same* bounded, secret-refusing
  validator that guards `agent.options` today: depth, key count, ASCII keys,
  string budget, serialized-byte budget, and a secret-looking-key refusal.
  Validation runs on the raw mapping *before* freezing.
- A rejected mapping collapses to empty and records **why** in
  `options_error`/`settings_error`; `config_error` surfaces the first reason.
  **Anything that builds a provider refuses while `config_error` is set.**
- `profiles` preserves the file's insertion order. Nothing sorts it.
- `unparsed` carries the raw YAML of entries korvid could not fully model so a
  save cannot silently delete an operator's broken-but-precious profile. It is
  never read by anything that builds, activates or lists a connection. An
  explicit delete must clear the name from **both** `profiles` and `unparsed`,
  or the writer re-emits it and the profile becomes undeletable.
- The dataclasses are `frozen=True` but hold mappings, so `__hash__ = None`.

`KorvidConfig` stores `agent_profiles: AgentProfilesConfig`. Core never imports
`litellm`, `korvid.providers`, or any provider identifier outside the isolated
legacy-migration region.

## Legacy Configuration Migration

The old shape remains readable for one compatibility cycle:

```yaml
agent:
  provider: ollama
  model: llama3
  base_url: http://localhost:11434
```

When `agent.profiles` is absent and a legacy `agent.provider` exists, parsing
creates an in-memory profile named `default` and sets it active (unless
`agent.enabled: false`, which maps to `active: null`).

Legacy provider names are translated at **one** parser boundary by a small
migration-only alias map. That map is the single place in korvid where a vendor
name is written down, it is reachable only from the legacy code path, and it is
deleted when the compatibility cycle ends:

| Legacy `agent.provider` | Migrated reference |
| --- | --- |
| `openai-compat`, `openai`, `vllm`, `github`, `anthropic`, `claude` | `openai/<model>` (bearer-token OpenAI-compatible endpoints, which is what the legacy transport actually was) |
| `azure` | `azure/<model>` |
| `ollama` | `ollama/<model>` |
| `github-copilot` | `github-copilot/<model>` (claimed by the special flow) |

The `azure` case additionally rewrites `base_url`: the legacy transport posted
to `{base_url}/chat/completions`, so a working legacy value is
deployment-scoped, while the Azure route expects the resource root and builds
`/openai/deployments/<deployment>/chat/completions` itself. The migration
strips everything from the first `/openai` segment onward, preserves any
deployment name the URL encoded as `options.azure_deployment`, and warns naming
both the old and the new value.

The writer emits only the new profile shape. The first successful `:ai` save
upgrades the file. If both shapes are present, `agent.profiles` wins and a
warning reports that legacy fields were ignored.

Compatibility accessors may temporarily derive old scalar values from the
active profile so intermediate commits remain buildable. They are read-only,
deprecated, and removed in the final cleanup. There is never duplicate mutable
state, and a profile whose adapter the *interim* transport cannot serve yields
no provider and a warning rather than a misrouted connection.

## Public Agent Boundary

`agent/model_profiles.py` defines the vocabulary the UI consumes without
importing `providers/` or `litellm`:

```python
class SetupFieldKind(Enum):
    TEXT = "text"
    SECRET_REF = "secret_ref"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    CHOICE = "choice"


@dataclass(frozen=True)
class SetupField:
    key: str
    label: str
    kind: SetupFieldKind
    required: bool = False
    default: str | None = None
    choices: tuple[str, ...] = ()
    help_text: str | None = None


@dataclass(frozen=True)
class AuthMethodDescriptor:
    id: str
    display_name: str
    fields: tuple[SetupField, ...] = ()


@dataclass(frozen=True)
class ModelEntry:
    """One connectable model, as data.

    Every capability field defaults to unknown. A fact is set only where
    a source directly asserts it, never inferred from the name.
    """

    reference: str                        # `provider/model`, written to config
    provider_id: str                      # informational; never dispatched on
    display_name: str
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    source: ModelEntrySource = ModelEntrySource.LITELLM
    credential_env_hints: tuple[str, ...] = ()
    endpoint_hint: str | None = None


class ModelCatalog(ABC):
    @abstractmethod
    def search(self, query: str, *, limit: int = 50) -> tuple[ModelEntry, ...]: ...

    @abstractmethod
    def entry(self, reference: str) -> ModelEntry | None: ...

    @abstractmethod
    def auth_methods(
        self, reference: str, *, endpoint: str | None = None
    ) -> tuple[AuthMethodDescriptor, ...]: ...

    @abstractmethod
    def option_fields(self, reference: str) -> tuple[SetupField, ...]: ...

    @abstractmethod
    def endpoint_requirement(self, reference: str) -> EndpointRequirement: ...

    @abstractmethod
    async def discover(self, profile: AgentProfileConfig) -> tuple[ModelEntry, ...]: ...

    @abstractmethod
    async def test(self, profile: AgentProfileConfig) -> str: ...

    @abstractmethod
    async def begin_auth(self, profile: AgentProfileConfig) -> DeviceLoginPrompt | None: ...

    @abstractmethod
    async def finish_auth(self, profile: AgentProfileConfig) -> str | None: ...
```

Note what is **absent**: there is no `descriptors()` returning a list of
adapters, because there is no adapter list. `auth_methods`, `option_fields` and
`endpoint_requirement` are answered *per model reference*, from data.

`auth_methods` takes the profile's `endpoint` as a keyword argument because the
`none` rule (§The `none`-auth rule, as data) is a function of that one field.
It is still static — the endpoint is a value the caller already holds, not a
lookup — and it is what lets the catalog mirror the factory's refusal exactly
instead of approximating it from a provider table.

`SetupField` is a bounded declarative field schema, not executable plugin UI.
Plugins cannot mount arbitrary Textual widgets or execute during screen
composition.

## Model Catalog Architecture

The catalog is layered. Each layer is optional except the first, and no layer
can override a routing decision.

### Layer 1 — LiteLLM offline data (primary, always present with `[agent]`)

`providers/litellm_catalog.py` builds `ModelEntry` values from
`litellm.models_by_provider` (**note the values are a mix of `set` and `list`,
so they must be normalised**), `litellm.model_list`, `litellm.model_cost` and
`litellm.get_model_info`. Capability facts are translated only where the source
directly asserts an equivalent fact (`supports_function_calling` →
`supports_tools`, `max_input_tokens` → `context_window_tokens`); anything absent
stays `None`.

`model_cost` is keyed inconsistently — some records are bare (`claude-sonnet-4-5`),
some are provider-qualified (`ollama/codegemma`), and for a measurable minority of
references **both** keys exist and carry different facts. The lookup therefore
tries `f"{provider}/{model_id}"` **first** and falls back to the bare key, so a
provider-specific record always wins over a same-named record belonging to another
provider's family entry.

**This layer is offline only if the import is made offline.** `import litellm`
fetches the remote model-cost map over HTTPS at import time unless
`LITELLM_LOCAL_MODEL_COST_MAP` is `true` in the environment *before* the import
statement runs. korvid therefore never imports `litellm` directly: a dedicated
import wrapper (§LiteLLM Transport Adapter) sets that variable, performs the
import, and silences LiteLLM's stderr logging. Without the wrapper the air-gap
story is false, wiring stalls on a connect timeout, LiteLLM writes warnings onto
the terminal a Textual app owns, and the catalog's contents become
network-dependent — the same class of failure this design cites as the reason to
avoid LiteLLM's Copilot flow.

**The `github_copilot` provider is excluded from this layer.** LiteLLM ships
dozens of already-slash-qualified `github_copilot/*` ids, and resolving that
prefix starts an interactive device login inside the routing call
(§Special-Flow Extension Registry). Emitting those ids into the catalog would put
the hazard one keystroke away in model search, so `_build_index` rewrites the
provider's entries onto korvid's own `github-copilot/` prefix — the spelling
korvid's flow claims — rather than offering LiteLLM's.

With those rules applied, the layer ships in the wheel LiteLLM already installs
and is what makes model search work on an air-gapped host.

### Layer 2 — models.dev enrichment (optional, bounded, cached)

`providers/models_dev.py` fetches `https://models.dev/api.json` and merges
richer display names, families, reasoning flags, context limits and credential
env-var hints onto entries the LiteLLM layer already produced, and may add
entries for models LiteLLM does not list *as search results only*.

Hard rules:

- **Never required at startup.** The fetch happens on demand, in a worker, when
  the operator opens model search — never during import, wiring, or agent
  construction.
- **Bounded.** A 10-second total timeout, a hard 12 MiB response-byte ceiling
  enforced while streaming (the document is ~4.5 MB today), and a refusal of
  any non-`application/json` content type.
- **Schema validated.** Every provider entry and model entry is checked against
  a narrow expected shape; unknown keys are ignored, malformed entries are
  dropped individually, and a malformed document as a whole is discarded.
- **Cached** under the standard per-user application cache directory
  (`$XDG_CACHE_HOME`/`~/.cache` on Linux, `~/Library/Caches` on macOS,
  `%LOCALAPPDATA%` on Windows) as `korvid/models-dev.json`, written atomically
  with `0o600`, revalidated with the ETag the endpoint already serves.
- **Stale-tolerant.** An expired cache is used rather than refetched-and-failed;
  a missing or corrupt cache falls back to the bundled LiteLLM data with a
  one-line notice. A models.dev failure is never an error dialog and never
  blocks a connection.
- **Credential- and prompt-free.** The request carries no `Authorization`
  header, no query parameters, no profile data and no conversation content.
  A test asserts the exact outbound request.
- **Never routing.** A `ModelEntry` sourced only from models.dev is offered in
  search and is connectable exactly to the degree LiteLLM can route it; the
  catalog marks its source so the UI can say so.
- **No remote assets.** korvid downloads the JSON document only. Provider logos
  and any other remote UI asset are out of scope; the TUI renders text.

### Layer 3 — endpoint live discovery

For a profile with an `endpoint`, `discover()` performs the existing bounded
`GET {endpoint}/models` probe (the current `providers/configurator.py` design:
`OutboundPolicy(max_request_chars=4096)`, endpoint/public client split, fixed
error messages) and returns those model ids as `ModelEntry` values with
`source=ENDPOINT`. This is what makes a private vLLM, LM Studio or Ollama host
listable without any dataset knowing about it.

### Layer 4 — manual entry

The operator can always type a model reference that no layer produced. It is
stored verbatim, validated only for shape, and marked `source=MANUAL`. A
catalog that cannot answer is never a wall.

### Precedence

`LITELLM` ∪ `MODELS_DEV` for search; `ENDPOINT` replaces both for a profile
that has an endpoint; `MANUAL` always wins for the value actually saved. No
layer can make a model *unconnectable* — the only thing that refuses a
connection is `litellm.get_llm_provider` rejecting the reference, an invalid
auth method, or `config_error`.

An entry keeps `source=LITELLM` when models.dev only restated or prettified a
fact LiteLLM already had. It is re-labelled `MODELS_DEV` **only** when
models.dev supplied a fact LiteLLM lacked — otherwise provenance would report a
source that contributed nothing, and the UI's "where did this come from" line
would be false.

### Answering per reference, without routing

`auth_methods`, `option_fields` and `endpoint_requirement` are answered from
**static data only**. None of them may call `litellm.get_llm_provider`: those
three run while rendering search results, once per visible row, and a routing
call inside a render loop is both slow and — for a claimed prefix — dangerous
(§Special-Flow Extension Registry).

`endpoint_requirement(reference)` is therefore answered from the **special-flow
registry alone**: a claiming flow's declared `endpoint` wins — that is the only
way `REQUIRED` or `UNSUPPORTED` is ever produced — and every unclaimed reference
is `OPTIONAL`.

There is deliberately **no provider→"needs an endpoint" table**. An earlier
revision specified one, derived from "the providers whose `model_cost` records
carry no vendor host and for which LiteLLM ships no built-in default base URL".
Measured against litellm 1.98.0, neither half of that derivation exists:
`model_cost` records carry no host or base-URL field of any kind (the only
adjacent keys are `supported_endpoints` and `supports_url_context`), and there
is no module-level provider→default-base-URL mapping. `litellm.get_api_base`
routes, which this section forbids, and
`ProviderConfigManager.get_provider_chat_config(...).get_complete_url(api_base=None, ...)`
is not a usable stand-in — `hosted_vllm`, `openai_like`, `groq` and `xai` all
answer `https://api.openai.com/chat/completions`, while `azure`, `anthropic` and
`gemini` raise. With no derivation available, the only way to satisfy a
`REQUIRED` assertion for a named provider would be a hardcoded vendor frozenset:
the exact table this design exists to delete, smuggled into the one module the
vendor-neutrality guard allows.

So korvid does not answer that question at all. A reference that genuinely
cannot be reached without an endpoint fails at **build** time, in the factory,
through `litellm.get_llm_provider` — which the error-handling contract already
surfaces as a warning plus a disabled agent naming the reference. `OPTIONAL`
means "korvid has no opinion", which is the truth, and an honest "no opinion"
beats a vendor list that is wrong the day a provider ships.

The one place a routing call is made is the factory, at build time, after the
special-flow registry has had its chance to claim the reference.

## Special-Flow Extension Registry

Two flows exist that LiteLLM cannot own for korvid:

1. **GitHub Copilot device login.** `litellm.get_llm_provider(model=
   "github_copilot/...")` was observed to *start an interactive device-login
   flow inside a routing call*: it prints a code to stdout, blocks on polling,
   and writes `~/.config/litellm/github_copilot/api-key.json`. A credential
   flow that runs as a side effect of asking "which provider is this?" is
   incompatible with korvid's approval and audit model — and in a TUI, stdout
   output is a corrupted screen and a blocking poll is a frozen event loop.
   korvid keeps its own device login (`providers/flow_copilot.py`, its token
   store, and its keystroke-confirmed prompt), claims the `github-copilot/`
   prefix under *its own* spelling so a reference can never fall through, and
   **never routes a profile to LiteLLM's `github_copilot` provider**.
2. **Native Ollama `thinking` parity.** Ollama's `/api/chat` route exposes a
   `thinking` field that its OpenAI-compatible `/v1/chat/completions` route does
   not, and korvid users rely on it. The flow claims the **option**
   `native_thinking` rather than the `ollama/` prefix, so parity is opt-in and
   the default path for every `ollama/*` reference is the same path every other
   model takes.

These are expressed as a **tiny declarative registry**, not a provider list:

```python
@dataclass(frozen=True)
class SpecialFlow:
    prefix: str                                   # the reference prefix it claims
    display_name: str
    auth_methods: tuple[AuthMethodDescriptor, ...]
    option_fields: tuple[SetupField, ...] = ()
    endpoint: EndpointRequirement = EndpointRequirement.OPTIONAL
    claims_option: str | None = None              # e.g. "native_thinking"
```

Rules:

- The registry is loaded through the **existing** `korvid.provider` entry-point
  group, using the existing **selected-only** loading, bounded validation and
  reserved-name rules. Entry-point *names* are read from installed distribution
  metadata; `EntryPoint.load()` is called for **exactly one** name — the one
  matching the prefix of a reference korvid is actually resolving — and only at
  the moment it is resolved. Building the catalog at composition time loads no
  third-party module at all. This is the same invariant
  `providers/plugin_registry.py` already holds ("Load only the selected entry
  point"), and this design may not weaken it: eagerly loading every declared
  entry point would execute arbitrary third-party module-level code on every
  korvid startup, and would let one broken plugin break TUI wiring. korvid's own
  two flows register through the same mechanism, from the same code path, with
  no privileged shortcut.
- A flow claims a reference by prefix, or by a named boolean option
  (`claims_option`) on a reference it otherwise shares.
- **Claiming normalizes the prefix.** The lookup lowercases the reference prefix
  and maps `_` to `-` before matching, so `github_copilot/gpt-4o` and
  `github-copilot/gpt-4o` resolve to the *same* flow. Without that
  normalization the underscore spelling — the one LiteLLM's own tables publish
  — is unclaimed, falls through to `get_llm_provider`, and starts the
  interactive device flow the registry exists to prevent. Declared prefixes are
  stored in the same normalized form, so two flows differing only in separator
  collide and the second is rejected rather than silently shadowed.
- **A claimed prefix is refused before routing.** Anything that would hand a
  reference to `litellm.get_llm_provider` first checks the normalized prefix
  against the claimed-and-denied set and refuses. The guard is behavioural, not
  a source grep: a source grep cannot see a reference that arrived from
  LiteLLM's own data.
- A flow supplies **auth and transport** for the references it claims. It never
  supplies a provider list, never appears in model search as a vendor choice,
  and never gates a reference it did not claim.
- Everything not claimed goes to LiteLLM. The registry being empty is a valid,
  fully functional configuration.
- Reserved prefixes cannot be squatted: the retired legacy aliases
  (`openai-compat`, `vllm`, `github`, `claude`) plus the shipped flow prefixes
  stay on a permanent deny-list, so a third-party plugin cannot claim a name an
  operator still associates with a built-in.

## Setup UI

The setup flow is **profile-first and model-search-first**. It never asks the
operator to pick a cloud vendor.

1. List configured profile names in file order, marking the active one.
2. Offer **activate**, **edit**, **add**, **delete** as distinct actions.
   *Activate writes only `agent.active`* — switching between two working
   profiles must not require a live probe or re-entry of field values.
3. For add/edit, the first question is **"which model?"**, answered by a search
   box over the catalog: type `claude`, `gpt`, `llama`, `qwen`; results show
   the reference, display name, context window and tool support. Typing a
   reference the catalog does not know is accepted as a manual entry.
4. Render the endpoint requirement, then the auth methods and option fields the
   catalog reports *for that reference*. The endpoint stage runs **before** the
   auth-method stage, and the collected endpoint is passed to
   `auth_methods(reference, endpoint=...)`, so `none` is offered exactly when
   the factory would accept it. Option fields are seeded from the profile being
   edited, so an edit round-trip cannot wipe `num_ctx`, `temperature` or
   `api_version`.
5. Offer live discovery when an endpoint is present.
6. Test the candidate profile.
7. Atomically rebuild the agent session.
8. Persist the new profile collection only after the runtime swap succeeds,
   carrying `unparsed` entries through untouched.

No screen branch checks a vendor, a CSP, or a built-in provider id. Device
login is offered because the catalog reported `device-login` among the
reference's auth methods, not because the UI recognised a name.

## LiteLLM Transport Adapter

One isolated module family implements korvid's existing `LLMProvider` over
LiteLLM. Exactly one module executes `import litellm`, and it is not a module
anything else in korvid imports directly.

- `providers/_litellm_import.py` — the **import wrapper**, and the *only*
  module in korvid that executes `import litellm`. It is stdlib-only above that
  import: it sets `LITELLM_LOCAL_MODEL_COST_MAP` in `os.environ` *before* the
  import statement, performs the import inside the `try:` that turns a missing
  extra into korvid's existing install hint, and detaches LiteLLM's stderr
  logging. It applies no policy of its own — the wrapper exists to make the
  import itself safe, which is a thing no code running *after* the import can
  do.
- `providers/litellm_runtime.py` — the *only* module that imports the wrapper.
  It applies the lockdown settings, fails loudly if a flag it means to set does
  not exist, and re-exports the callables korvid uses — including the SDK's
  **exception base class**, so `providers/` still names exactly one module for
  everything LiteLLM. That re-export is load-bearing: `litellm`'s own
  `exceptions.APIError` is *not* the base of its error hierarchy. Measured on
  1.98.0, `litellm.exceptions.AuthenticationError` derives from
  `openai.AuthenticationError` → `openai.APIStatusError` → `openai.APIError`,
  and 22 of the 24 exported error classes have `openai.OpenAIError` as their
  only common base. Catching `litellm.exceptions.APIError` would catch
  essentially nothing and let raw SDK exceptions escape into korvid's engine;
  the two classes it misses (`BudgetExceededError` and the guardrail errors)
  belong to features the lockdown disables. The re-export is named
  `ProviderSDKError` so that the transport reads as korvid vocabulary and no
  file outside `litellm_runtime.py` needs `import openai`.
- `providers/litellm_request.py` — pure conversion of korvid's already-sanitized
  canonical messages and tool schemas into `acompletion` arguments.
- `providers/litellm_provider.py` — `LiteLLMProvider(LLMProvider)`: drives
  `acompletion(stream=True)`, normalizes text fragments, tool-call fragments
  and usage into the existing provider event dictionaries, emits
  `REQUEST_SENT`, and closes the stream on cancellation.
- `providers/litellm_factory.py` — turns an `AgentProfileConfig` into the
  keyword arguments for `acompletion`, resolving the generic auth methods and
  refusing `config_error`.

### The import wrapper

`import litellm` is not a side-effect-free statement. In 1.98.0 it calls
`get_model_cost_map(url=...)`, which performs a blocking HTTPS `GET` of
`model_prices_and_context_window.json` and, on failure, writes a warning to
**stderr** through LiteLLM's own `StreamHandler`. Both are unacceptable here:
stderr belongs to a running Textual application, and a blocking fetch at wiring
time is a startup stall that gets worse the more firewalled the host is. Neither
can be prevented by anything the lockdown does, because the lockdown runs after
the import.

The wrapper therefore, in this order:

1. `os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")` — `setdefault`,
   not assignment, so an operator who deliberately wants the remote map keeps it.
2. `import litellm` (inside the `try:` that raises the install hint on
   `ImportError`; the import is not part of the sorted top-level import block, so
   the ordering cannot be rearranged by an import sorter).
3. Detach LiteLLM's logging from the terminal: remove every `StreamHandler` from
   `litellm.verbose_logger` and from the loggers under `LiteLLM`, attach a
   `logging.NullHandler()`, and set `propagate = False`, so nothing LiteLLM logs
   can reach the screen korvid is drawing on.

Detaching the handlers closes the *logging* channel and nothing else. LiteLLM
also writes to **stdout** with bare `print()` calls that never touch the logging
subsystem — see the lockdown note below, which is why `suppress_debug_info` is
part of the lockdown rather than hygiene.

A test patches `socket.socket.connect`/`connect_ex` to record and refuse, then
imports `korvid.providers.litellm_runtime` in a **fresh interpreter** and builds
a full catalog, asserting the recorded connection list is empty. Running it in a
subprocess is what makes it deterministic: `litellm` may already be imported by
another test in the same session, and a module-cached import proves nothing.

### Lockdown

Applied by `providers/litellm_runtime.py` on first import, and before any call:

```python
litellm.telemetry = False
litellm.turn_off_message_logging = True
litellm.success_callback = []
litellm.failure_callback = []
litellm.callbacks = []
litellm._async_success_callback = []
litellm._async_failure_callback = []
litellm.suppress_debug_info = True
```

Every one of these names is checked with `hasattr` **before** it is assigned,
and a missing name raises `ImportError` naming it. Assigning first and asserting
afterwards is not a contract test: if LiteLLM renames `_async_success_callback`,
assignment simply creates a fresh unused attribute, the read-back assertion still
passes, and the real callback sink stays open. Failing at import is the only
outcome that keeps "a rename upstream fails loudly" true, and a test drives a
stub module missing one flag and asserts the `ImportError`.

**`suppress_debug_info = True` is the one flag that protects the terminal, and
it is not hygiene.** On every mapped exception LiteLLM calls bare `print()` with
ANSI colour codes — `litellm_core_utils/exception_mapping_utils.py` and
`litellm_core_utils/get_llm_provider_logic.py` both emit a red
"Give Feedback / Get Help" / "Provider List" block straight to **stdout**,
gated on nothing but `litellm.suppress_debug_info is False`. Those calls bypass
the logging subsystem entirely, so the import wrapper's `StreamHandler` removal
does not touch them: inside a Textual app, the first 401 would repaint the
screen with escape sequences korvid did not write. The flag list is therefore
load-bearing at both ends — a maintainer trimming `suppress_debug_info` as
"noise control" reopens a TUI-corruption vector — and a test drives a provider
error under `capsys` and asserts `capsys.readouterr().out == ""`.

No `Router`, no `num_retries`, no `fallbacks`, no proxy, no cache, no
callback/observability integration of any kind. A test asserts each flag after
import and asserts that no korvid module other than the import wrapper names
`litellm` in an import statement.

### Streaming normalization

Verified against litellm 1.98.0 through an `httpx.MockTransport` mounted on a
client handed to `acompletion(client=...)`:

- `await acompletion(..., stream=True)` returns a `CustomStreamWrapper`, and it
  **raises before returning** both when the transport refuses the connection and
  when the provider answers with an error status. The two are not distinguishable
  by exception type: a refused connection and a genuine HTTP 500 both surface as
  `litellm.InternalServerError` with `status_code=500`. They *are* distinguishable
  by the exception chain — a refused connection carries an `httpx.TransportError`
  (`ConnectError`, `ReadError`, `ConnectTimeout`, `ReadTimeout`, …), while an
  answered request carries an `httpx.HTTPStatusError`.
- The exception the transport catches is the SDK base re-exported by
  `providers/litellm_runtime.py` as `ProviderSDKError` (`openai.OpenAIError`), **not**
  `litellm.exceptions.APIError` — LiteLLM's error classes do not derive from
  the latter (§LiteLLM Transport Adapter). Catching the wrong base makes every
  rule below dead code, so a test asserts that a real `AuthenticationError`
  raised by the transport is translated rather than propagated.
- `REQUEST_SENT` follows the existing contract exactly: it means *the transport
  accepted the request and response headers came back*, **before the status code
  is judged**. So it is emitted when the `await` returns a wrapper, **and also**
  when the `await` raises with an `httpx.HTTPStatusError` in its chain — 401,
  403, 404, 429, 500 and 503 all mean the provider has the payload, and the
  outbound-snapshot panel must show that payload rather than a stale one. It is
  **not** emitted when the chain carries an `httpx.TransportError`, nor when
  neither marker is present (a request LiteLLM rejected before building it was
  never sent). Tests parametrize over both families.
- Each chunk is a `ModelResponseStream`; `chunk.choices[0].delta.content` is the
  text fragment, `delta.tool_calls` is a list of `ChatCompletionDeltaToolCall`
  with `index`, `id`, `function.name`, `function.arguments`. Arguments arrive
  fragmented; korvid accumulates keyed by **`tool_call.index`** — the index of
  the tool call within the message, not `choice.index`, which is `0` for every
  chunk when `n=1` and would collapse parallel calls into one — and emits **one**
  tool call per index when the stream ends.
- Accumulated-but-unemitted tool calls are **dropped** when the stream raises
  mid-iteration, and the error is surfaced. A half-received call is not a call;
  emitting one would hand the tool harness arguments the model never finished
  writing. This is distinct from a *complete* call whose arguments do not parse,
  which is surfaced with its raw text so the harness can refuse it.
- Usage arrives on a trailing chunk. With `stream_options={"include_usage":
  True}` and a provider that emits its own usage in a `choices: []` chunk,
  LiteLLM passes the provider's exact numbers through (observed 11/7/18). When
  the provider puts usage in a chunk that *also* carries choices, LiteLLM
  substitutes its own tokenizer estimate — so korvid records usage as reported
  and never treats it as authoritative billing data.
- `CustomStreamWrapper.aclose()` exists and is awaited on cancellation.

### Outbound boundary

The existing order is unchanged:

1. provider message preparation
2. `OutboundPolicy` redaction/canonicalization/size enforcement
3. exact canonical request snapshot
4. LiteLLM argument conversion
5. network transmission

Conversion may re-encode the canonical request but may not add user-controlled
prompt text or tool definitions. Tests compare the canonical parts entering the
adapter with the snapshot.

### Capabilities

`litellm.get_model_info` facts are translated only where the source directly
asserts an equivalent fact. Unknown facts remain `None`. korvid does not infer
tool support, context windows, or reasoning support from provider or model
names.

## Dependency Layout

The base installation is unchanged and imports no `litellm` module.

```toml
agent = [
  "httpx>=0.27",
  "keyring>=25.7.0",
  "litellm>=1.98.0,<2",
]
```

There are **no per-vendor extras**. That is the point: `provider-openai`,
`provider-anthropic`, `provider-google` and `provider-bedrock` do not exist,
because korvid no longer has per-vendor code to gate. The `<2` cap protects the
streaming/normalization boundary from an unreviewed major-version contract
change; the `>=1.98.0` floor is the version whose API surface this design was
verified against.

The `entra` extra is retained: Azure's `provider-default` method still resolves
through `EntraCredentialSource`.

`uv.lock` is regenerated only by the `Relock` workflow, never locally behind
the corporate mirror.

## Security Invariants

- Agent write tools always pass the existing approval gate.
- Audit logging remains fail-closed and outside the model/provider layer.
- Model libraries never receive Kubernetes clients, `WriteOps`, approval
  callbacks, or audit handles.
- The outbound policy runs for every request before provider handoff.
- Config stores references to secrets, never secret values. `options` and
  `auth.settings` are bounded and refuse secret-looking keys.
- Plugin/special-flow descriptors are data-only, bounded, validated, and
  copy-owned. Unselected plugins are not imported: entry-point *names* come from
  distribution metadata, and `EntryPoint.load()` runs only for the single flow a
  reference resolves to.
- LiteLLM telemetry, message logging and every callback list are disabled
  before the first call; a lockdown flag that no longer exists upstream fails
  the import loudly instead of silently leaving a sink open.
- `suppress_debug_info = True` is part of that lockdown because LiteLLM prints
  ANSI-coloured help text to **stdout** with bare `print()` on every mapped
  exception, bypassing logging entirely. A test drives a provider error under
  `capsys` and asserts stdout stayed empty.
- `import litellm` happens exactly once, in `providers/_litellm_import.py`,
  with `LITELLM_LOCAL_MODEL_COST_MAP` already set — so importing korvid's
  provider layer makes **no network call**, and LiteLLM's `StreamHandler`s are
  detached so nothing it logs can corrupt the TUI. A subprocess test asserts zero
  socket connections across import and full catalog construction.
- korvid never routes to LiteLLM's `github_copilot` provider, whose provider
  *resolution* starts an interactive credential flow. That prefix — in **both**
  its underscore and hyphen spellings — is claimed by korvid's own flow, is
  excluded from or rewritten in the catalog, and is refused **before** any call
  to `get_llm_provider`.
- `REQUEST_SENT` means the provider received the payload. It is emitted for an
  answered request, including an error status, and never for a connection or
  timeout failure — a panel that under-reports a sent request is a security
  defect, and one that over-reports is a false alarm.
- No automatic retries, fallbacks or routing: a fallback that can move prompts
  across trust boundaries requires a separate explicit design.
- models.dev is metadata only: no credentials, no prompts, bounded bytes and
  time, schema validated, never required, never routing, no remote assets.
- Missing the `[agent]` extra produces an actionable install hint and does not
  fall back to another transport.
- An unknown active profile, an unroutable model reference, an invalid auth
  method, or invalid provider options **prevents agent construction**; the TUI
  starts with the agent disabled and a visible warning rather than crashing.
- Keyless (`none`) auth requires an operator-supplied `endpoint`. Without one
  korvid refuses to build the provider, so an unauthenticated request can never
  be sent to a host the operator did not name.
- Provider errors are surfaced; no success-shaped fallback is added.

## Error Handling

Errors are classified at boundaries:

- config shape errors: bounded startup warnings or migration errors following
  existing config policy
- missing `[agent]` extra: fixed install hint
- invalid descriptor/plugin: bounded `ProviderPluginError`
- `config_error` on the active profile: refuse to build, warn, agent disabled
- a reference that cannot be routed without an endpoint korvid did not ask for:
  `litellm.get_llm_provider` rejects it at build time, and the factory turns
  that into a warning naming the reference plus a disabled agent — korvid does
  not predict this case from a provider table (§Answering per reference)
- `none` auth on a profile with no `endpoint`: refuse to build, warn, agent
  disabled
- credential failure: provider construction/test failure without session swap
- stream protocol violation: existing provider-contract error path
- profile persistence failure after a successful runtime swap: existing
  applied-now/reverts-on-restart warning
- models.dev failure: silent fall back to bundled LiteLLM data plus one notice

No broad catch is added around runtime construction, but every documented
failure mode of the factory returns `None` with a warning rather than
propagating an exception into TUI startup. Best-effort model listing may still
return an empty list because typed model entry is the documented fallback.

## Delivery Strategy

This subsystem replacement is delivered as **one pull request**. Config,
catalog, setup UI, transport and legacy deletion are one architectural change;
splitting them across long-lived PRs would obscure the reason for the migration
and leave transitional compatibility seams looking permanent.

The pull request uses ordered commit groups. Each group is individually
testable and reviewable; the PR is accepted only when all groups are present
and the legacy paths are deleted.

The PR is opened as a **draft after the config-domain group**, so GitHub CI and
the complete diff stay visible throughout development. Opening it requires an
explicit instruction from the maintainer. If that instruction never arrives and
the draft is not opened mid-flight, the PR is opened at the end of the final
group instead — the work is still delivered as exactly **one** pull request
either way, and the final step must handle both states rather than assuming a PR
already exists. It is marked ready only after the final cleanup, the full
repository gate, dependency review and the review rounds succeed.

### Commit group 1: Named profile config domain

- Immutable profile/auth dataclasses; new-shape parser.
- Legacy migration into an in-memory `default` profile.
- **Standard `provider/model` slash references** (a correction commit, because
  the first two commits landed with colon references; the correction is a *new*
  commit, never an amend of a landed one).
- Writer that emits only the new shape and carries `unparsed` through.
- Derived compatibility accessors for existing wiring.
- No dependency change.

### Commit group 2: Data-driven model catalog

- Public catalog contracts on the agent boundary.
- **Add `litellm` to `[agent]` and regenerate the lock through `Relock`** —
  this lands *before* the first module that reads LiteLLM's tables, so the
  catalog commits have a real red-to-green cycle instead of a suite that
  silently skips until a later group.
- LiteLLM offline catalog, behind the offline import wrapper.
- Bounded, cached models.dev enrichment.
- Declarative special-flow registry, endpoint discovery, manual entry, and
  composition-root injection.

### Commit group 3: Profile-first, search-first setup UI

- Profile manager with distinct activate/edit/add/delete actions.
- Model search stage; descriptor-driven endpoint/auth/option stages seeded from
  the edited profile.
- Atomic runtime swap and persistence.

### Commit group 4: LiteLLM transport

- Isolated runtime shim with the lockdown settings.
- Request conversion, streaming provider, profile→call-argument factory.
- Outbound, instrumentation and cancellation contract tests.

The dependency itself was taken in group 2, so every module in this group is
written against an installed library rather than against a table of promises.

### Commit group 5: The two irreducible flows, then the deletion

- GitHub Copilot device login and native Ollama `thinking` parity expressed
  through the registry, with no UI branch naming either.
- Migrate `src/korvid/evals/__main__.py`'s provider factory off the transports
  about to be deleted, **before** deleting them — the eval CLI imports them at
  module scope and is checked by `mypy src/` and collected by pytest, so
  deleting first would leave the repository gate red with no planned remedy.
- Remove the legacy scalars, the built-in alias tables, the replaced transports
  and the configurator request path — once the replacement is proved end to end
  *and* the two flows have somewhere else to live.
- The vendor-neutrality guard lands with the deletion, so the table cannot grow
  back.

### Commit group 6: Documentation, gates and the review loop

- Update operator docs, the decision record, licensing notes, the threat model
  and the release migration notes.
- Import-graph, layer-boundary and full-gate verification across every
  supported interpreter.
- The iterative review loop, terminating on AGENTS.md's rule rather than after
  a single round.

Each group runs its focused tests and static checks. The complete branch runs
the full repository gate after every review-driven fix. The maintainer merges
the single final PR; automation never merges.

## Testing Strategy

### Config contracts

- multiple profiles round-trip, insertion order preserved
- exact active-profile selection
- immutable/recursively copy-owned nested mappings, and a **thaw** round-trip
  through `save_agent_profiles` → `load_config`
- bounded/secret-refusing `options` and `auth.settings`, with `config_error`
- invalid/missing active profile
- legacy-to-`default` normalization, including the Azure endpoint rewrite
- slash-form references for every legacy provider name
- new shape wins over legacy with warning
- writer removes legacy managed fields, preserves unrelated config, and carries
  `unparsed` — and an explicit delete removes the entry from both halves

### Catalog contracts

- LiteLLM search finds a known model; `models_by_provider` `set`/`list`
  heterogeneity is normalized
- **no test asserts a catalog size.** Table cardinality differs between the
  bundled data and the remote map and drifts with every LiteLLM patch release;
  an exact-count assertion is a scheduled false failure. Tests assert shape,
  membership and absence instead
- provider-qualified `model_cost` keys are preferred over bare ones
- capability translation only where directly asserted; unknown stays `None`
- `github_copilot` is absent from the catalog under LiteLLM's spelling, and a
  reference in **either** spelling resolves to korvid's own flow
- `auth_methods`, `option_fields` and `endpoint_requirement` never call
  `get_llm_provider` — asserted behaviourally, with the routing function
  monkeypatched to fail on call
- `endpoint_requirement` is `OPTIONAL` for every unclaimed reference and takes
  `REQUIRED`/`UNSUPPORTED` **only** from a claiming flow's declaration; a test
  asserts the catalog module holds no provider→endpoint table
- `auth_methods(reference, endpoint=None)` omits `none` and
  `auth_methods(reference, endpoint="http://…")` offers it, for the same
  reference — the exact mirror of the factory rule, asserted against the real
  catalog
- models.dev: byte ceiling, timeout, content-type refusal, schema rejection,
  atomic `0o600` cache write, ETag revalidation, stale fallback, exact
  credential-free outbound request, and *never fetched at startup*
- endpoint discovery reuses the bounded probe
- manual entry accepted and marked
- special flows: prefix claiming in both separator spellings, reserved-name
  refusal, empty registry is valid, and **an entry point whose `load()` raises
  is never loaded** while a different flow is being resolved

### UI contracts

- list, activate (write-only), add, edit, delete profiles
- model search drives the reference; no vendor question is ever asked
- option fields seeded from the edited profile survive a round-trip
- device login offered from catalog data, not from a name
- atomic apply/save/retry behavior; deleting a rejected profile sticks

### Transport contracts

- async text streaming, ordered fragments
- fragmented and parallel tool calls assembled once, keyed by `tool_call.index`
- a stream that fails mid-flight drops accumulated partial tool calls and
  surfaces the error
- usage extraction, including the provider-supplied passthrough case
- `REQUEST_SENT` timing: emitted on a successful stream **and** on an answered
  error status (401/403/404/429/500/503), *not* emitted on connection refusal or
  timeout
- `provider-default` auth omits `api_key` from the call arguments entirely —
  asserted unconditionally, not inside a conditional that can pass vacuously
- `none` auth is refused for a profile with no `endpoint` and allowed for one
  that has it — parametrized over **real** references (`openai/gpt-4o`,
  `anthropic/claude-sonnet-4-5`, `ollama/llama3`) with routing left unpatched,
  so the assertion is about LiteLLM's actual behaviour rather than a fabricated
  return value
- cancellation closes the stream
- malformed stream rejection; provider errors never become successful
  completions
- an SDK error raised by the transport is translated, not propagated — the
  catch is on the re-exported `openai.OpenAIError` base, and a test asserts a
  real `AuthenticationError` never escapes
- a provider error leaves **stdout** untouched under `capsys`, which is what
  `suppress_debug_info` buys
- canonical outbound snapshot equivalence
- lockdown flags set; a missing lockdown attribute raises at import; no callback
  receives prompts
- no korvid module outside the import wrapper names `litellm` in an import
- korvid never routes to LiteLLM's `github_copilot` provider

### Import/dependency contracts

- base TUI imports without `litellm`
- **zero socket connections** across `import korvid.providers.litellm_runtime`
  and a full catalog build, measured in a fresh subprocess
- `[agent]` imports independently; missing extra fails only when selected
- the eval CLI (`korvid.evals.__main__`) builds its live provider from the same
  profile factory as the TUI, so no module outside `providers/` imports a
  deleted transport
- Python 3.11/3.12/3.13 and Windows matrix
- dependency graph, deptry and optional-extra import tests
- lockfile policy test still rejects a mirror-scoped lock

## Non-Goals

- Replacing `NativeAgentEngine` or `ToolHarness`.
- Moving approval or audit behavior into LiteLLM.
- LiteLLM `Router`, retries, fallbacks, cooldowns, budgets, caching or the
  proxy server.
- Automatic fallback between profiles/providers.
- A hosted LiteLLM gateway bundled with korvid (a gateway is just an
  OpenAI-compatible endpoint profile).
- Storing API keys or cloud credentials in config.
- Executable third-party setup UI plugins.
- Remote UI assets (provider logos and the like).
- Inferring cloud model provider from the Kubernetes cluster CSP.
- Reusing `k8s/csp.py` detection to choose an LLM profile.
