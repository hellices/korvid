# Provider-Neutral Model Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace korvid's single hard-coded provider configuration and CSP-oriented `:ai` wizard with named model connection profiles backed by one provider catalog and a Pydantic AI model transport adapter, while preserving korvid's fail-closed approval, audit, tool, and outbound-policy runtime.

**Architecture:** `core/config.py` gains immutable, provider-neutral profile dataclasses (`AgentAuthConfig`, `AgentProfileConfig`, `AgentProfilesConfig`) that are the single source of truth; `agent/model_profiles.py` publishes the descriptor/catalog contracts the UI consumes without importing `providers/`; `providers/adapter_catalog.py` implements that catalog over built-in Pydantic AI adapters plus validated `korvid.provider` entry points; `providers/pydantic_model.py` implements korvid's existing `LLMProvider` over a Pydantic AI `Model`. korvid's `NativeAgentEngine`, `RequestGateway`, `OutboundPolicy`, `ToolHarness` and approval/audit path are untouched — only the transport below the `LLMProvider` boundary changes.

**Tech Stack:** Python 3.11+, Textual, `pydantic-ai-slim>=2.35.3,<3` (inside the `[agent]` extra only), `httpx2>=2.12` (the client type Pydantic AI 2.35.3 expects — passing a legacy `httpx.AsyncClient` raises `PydanticAIDeprecationWarning`, which this repo's `filterwarnings = ["error"]` turns into a test failure; openai 3.5.0's own floor is the looser `httpx2>=2.7.0,<3`, so `>=2.12` is korvid's deliberately tighter choice), pytest / pytest-asyncio, Ruff, mypy --strict, tach, deptry, uv.

**API baseline:** every Pydantic AI call in this plan is written against the exact interfaces of **pydantic-ai-slim 2.35.3** (with the `openai`, `anthropic`, `google` and `bedrock` extras), verified by reflection against, and execution against, an installed copy. The load-bearing facts, all of which the code below depends on:

| Fact | Exact 2.35.3 interface |
| --- | --- |
| Streaming entry point | `Model.request_stream(self, messages: list[ModelMessage], model_settings: ModelSettings \| None, model_request_parameters: ModelRequestParameters, run_context: RunContext[Any] \| None = None) -> AsyncIterator[StreamedResponse]` — an `@asynccontextmanager`. The non-streaming sibling is `Model.request(self, messages, model_settings, model_request_parameters)` |
| Request parameters | `ModelRequestParameters(...)` — a **keyword-only** dataclass whose fields are `function_tools`, `native_tools`, `tool_visibility`, `revealed_tool_names`, `deferred_capability_ids`, `output_mode`, `output_object`, `output_tools`, `prompted_output_template`, `allow_text_output`, `allow_image_output`, `instruction_parts`, `thinking`. Defaults that this plan asserts on: `function_tools == []`, `native_tools == []`, `output_tools == []`, `output_mode == "text"`, `allow_text_output is True`, and **`instruction_parts is None`** (not `[]`) |
| Model surface | `Model.model_name`, `Model.system`, `Model.base_url` and `Model.settings` are **properties**; `Model.profile` is a `cached_property`. `base_url` is therefore read, never assigned |
| Streamed response base | `StreamedResponse` is a `@dataclass` whose `__init__` takes exactly one argument, `model_request_parameters`. Its abstract members are `_get_event_iterator`, `model_name`, `provider_name`, `provider_url` and `timestamp`. `usage` is a **property**; `_usage`, `_cancelled`, `_finished`, `state`, `provider_response_id` and `finish_reason` are `field(init=False)`; `_parts_manager` is a `cached_property` |
| Parts manager | `ModelResponsePartsManager.handle_text_delta(*, vendor_part_id, content, id=None, provider_name=None, provider_details=None, thinking_tags=None, ignore_leading_whitespace=False)` and `handle_tool_call_delta(*, vendor_part_id, tool_name=None, args=None, tool_call_id=None, provider_name=None, provider_details=None)` — both keyword-only |
| Tool schema | `ToolDefinition(*, name: str, parameters_json_schema=<factory>, description: str \| None = None, …)` from `pydantic_ai.tools` — keyword-only |
| Stream events | `PartStartEvent(index, part, ...)`, `PartDeltaEvent(index, delta)`, `PartEndEvent(index, part, ...)`, `FinalResultEvent(...)` from `pydantic_ai.messages` |
| Parts / deltas | `TextPart(content=...)`, `ToolCallPart(tool_name=..., args=..., tool_call_id=...)`, `TextPartDelta(content_delta=...)`, `ToolCallPartDelta(tool_name_delta=..., args_delta=..., tool_call_id=...)` |
| Usage | `StreamedResponse.usage` is a **property** (not a method) returning `RequestUsage`, whose `input_tokens`/`output_tokens` are `int` defaulting to `0` (never `None`); `RequestUsage.has_values()` reports whether anything non-zero was set |
| Assembled response | `StreamedResponse.get() -> ModelResponse`; `ModelResponse.parts` holds the *assembled* parts (a `PartStartEvent` for a tool call carries only a **fragment** of `args` when the vendor fragments the call — confirmed by driving a fragmented `ToolCallPartDelta` sequence through the real parts manager). `ModelResponse(parts=[])` is legal, so an empty response is representable |
| Cancellation | `await StreamedResponse.cancel()` marks the stream cancelled and calls `close_stream()`; `get().state` becomes `"interrupted"` |
| Model settings | `pydantic_ai.settings.ModelSettings` is a `total=False` `TypedDict` whose keys are `extra_body`, `extra_headers`, `frequency_penalty`, `logit_bias`, `max_tokens`, `parallel_tool_calls`, `presence_penalty`, `seed`, `service_tier`, `stop_sequences`, `temperature`, `thinking`, `timeout`, `tool_choice`, `top_k`, `top_p`. `extra_body` is merged into the vendor request body verbatim (confirmed on the wire) |
| Azure | `AzureProvider(self, *, azure_endpoint=None, api_version=None, api_key=None, voice_live_endpoint=None, voice_live_api_key=None, voice_live_api_version=None, openai_client=None, http_client=None) -> None` from `pydantic_ai.providers.azure`. With `api_key` it builds an `AsyncAzureOpenAI` that sends the raw `api-key` header and **no** `Authorization`; passing `openai_client` requires `azure_endpoint`, `api_key` and `http_client` to all be None. `api_key=None` and `api_key=""` both raise `pydantic_ai.exceptions.UserError` unless `AZURE_OPENAI_API_KEY` is set |
| Azure URL construction | `AsyncAzureOpenAI(self, *, azure_endpoint=None, azure_deployment=None, api_version=None, api_key: str \| Callable[[], Awaitable[str]] \| None = None, azure_ad_token=None, azure_ad_token_provider=None, base_url=None, http_client: httpx2.AsyncClient \| None = None, …) -> None`. Given `azure_endpoint="https://x.openai.azure.com"` it posts to `…/openai/deployments/<model>/chat/completions?api-version=<v>`. A resource URL that already carries a `/openai…` path is **appended to, not replaced**, producing a broken URL — see Task 2's endpoint normalization |
| OpenAI | `OpenAIProvider(self, base_url=None, api_key=None, openai_client=None, http_client=None) -> None` (positional-or-keyword); `OpenAIChatModel(model_name, *, provider=…, profile=None, settings=None)` from `pydantic_ai.models.openai`. `api_key=None` silently falls back to `OPENAI_API_KEY` from the ambient environment |
| Ollama | `OllamaProvider(self, base_url=None, api_key=None, openai_client=None, http_client=None) -> None` from `pydantic_ai.providers.ollama` — an OpenAI-compatible provider that appends nothing to `base_url`, falls back to `OLLAMA_BASE_URL`/`OLLAMA_API_KEY`, and substitutes the literal placeholder key `"api-key-not-set"` when neither a key nor that variable is present |
| Other adapters | `AnthropicModel` / `GoogleModel` / `BedrockConverseModel` all take `(model_name, *, provider=…, profile=None, settings=None)`; `AnthropicProvider(*, api_key=None, base_url=None, anthropic_client=None, http_client=None)`, `GoogleProvider(*, api_key=None, client=None, http_client=None, base_url=None, retry_options=None)`, `BedrockProvider(*, bedrock_client=None, aws_access_key_id=None, aws_secret_access_key=None, aws_session_token=None, base_url=None, region_name=None, profile_name=None, api_key=None, aws_read_timeout=None, aws_connect_timeout=None)` |
| Client shutdown | Not uniform: `AsyncOpenAI`/`AsyncAnthropic` expose an **async** `close()` and no `aclose()`; `google.genai.Client.close()` and botocore's `close()` are **sync**. Try `aclose`, then `close`, and await only when `inspect.isawaitable(result)` |
| HTTP client flavour | Providers take `httpx2.AsyncClient`; a legacy `httpx.AsyncClient` raises `PydanticAIDeprecationWarning`. openai 3.5.0 requires `httpx2>=2.7.0,<3` (2.12.0 is what resolves here); korvid's own `httpx2>=2.12` floor in `[agent]` is korvid's choice, not the SDK's minimum |

Anything not in this table is quoted with its verified signature at the point of use. No step in this plan says "confirm after install" or "adjust if the signature differs": the signatures are already confirmed.

**How these facts were established, and how to re-establish them.** Build a throwaway venv outside the repository (see the network constraint below), then reflect with `inspect.signature` / `inspect.getsource` for signatures, and *drive real objects* for behaviour. Wire-level facts — which header carries a credential, which URL a request lands on, what JSON body a setting produces — are established by mounting an `httpx2.MockTransport` on the SDK's `http_client` and capturing the `httpx2.Request` the SDK builds. **Never** reach into private SDK internals such as `AsyncAzureOpenAI._build_request` or `._prepare_options` for this: a private helper can change shape in a patch release, and a security invariant asserted through a private hook is an invariant that silently stops being asserted. Every security test in this plan captures a real request through a public transport seam.

**Two of those reproductions have already been executed end to end**, against a scratch venv holding `pydantic-ai-slim 2.35.3`, `openai 3.5.0`, `httpx2 2.12.0`, `anthropic 1.2.0`, `boto3 1.43.82` and `google-genai 2.20.0`, so the two facts this plan leans on hardest are observed rather than reasoned:

- **The Azure URL table** (Task 2 Step 3) was reproduced row for row through an `httpx2.MockTransport` mounted on a real `AzureOpenAI` client. All three rows — a bare resource URL, a `/openai/v1` URL and a deployment-scoped URL — produced exactly the paths the table states, each carrying `api-version=2024-10-21`. This is what makes `_migrate_azure_endpoint`'s "strip back to the resource URL" safe: the client rebuilds the path it stripped.
- **The Task 13 streaming contract** was driven with a scripted `StreamedResponse` double through the real `complete()` loop. Six checks passed: a tool call whose JSON arguments arrive split across chunks is assembled once from the final parts (not re-emitted per delta), text deltas arrive in order, the usage record carries request and response tokens, `REQUEST_SENT` is *not* emitted when the transport refuses the connection, cancellation propagates as `CancelledError` without swallowing it, and tool definitions reach `ModelRequestParameters`.

Neither reproduction lives in the repository, so neither is a substitute for the tests this plan writes. They are the reason those tests are written the way they are — and Task 11 Step 9, Task 13 Step 4 and Task 19 Step 5 each require the repository's own versions to *run* rather than skip.

## Global Constraints

- **One pull request.** Config, catalog, setup UI, transport, Copilot extension and legacy deletion land on the single branch `agents/provider-neutral-profiles`. Each task below is an independently testable commit group step; none of them is its own PR. Opening that PR — draft or otherwise — requires an explicit instruction from the human driving the work; Task 4 pushes the branch unconditionally and opens the PR only under that instruction.
- **The maintainer merges. You never do.** No `gh pr merge`, no auto-merge, no merge automation of any kind, including for the relock helper PR (which is *closed and deleted*, never merged).
- **The review loop runs to AGENTS.md's termination rule, not to one round.** Task 20 iterates: read every comment including the suppressed low-confidence findings inside the review body's `<details>` block, fix credible findings with TDD, reply per comment, resolve each thread, re-request review, poll, repeat. It terminates after two consecutive rounds that contain only suppressed low-confidence findings and no unresolved blocking findings; any new credible blocking finding resets that counter.
- **Never `git commit --no-verify`**, never edit a gate file to make a failure pass.
- Profile names: non-empty, at most 100 characters, only ASCII letters, digits, `.`, `_`, `-`. Names are **not** normalized (`prod-east` != `prod_east`). `agent.active` must exactly name an entry in `agent.profiles`.
- **Profile insertion order is the file's order.** `AgentProfilesConfig.profiles` preserves the order the profiles appear in `config.yaml`; nothing sorts them. The wizard's list, the `:model` picker and every test assert that same order.
- Model references use `provider:model`. The prefix selects an installed model adapter; the suffix is the adapter's model identifier.
- Common auth methods, exactly these five ids: `none`, `environment` (with `key`), `keyring` (with `key`), `provider-default`, `device-login`.
- Secrets are never stored in YAML — only references (env var name, keyring key name). Both `profile.options` and `auth.settings` are parsed through the *same* bounded, secret-refusing validator before they are frozen, so a reference mapping can never grow unbounded and can never carry a key that looks like an inline secret.
- **A save never deletes what the parser could not model.** A profile korvid dropped (invalid name, no `model:`) and an `options`/`auth` block korvid rejected are carried on `AgentProfilesConfig.unparsed` as raw mappings and written back verbatim, so `:ai` saving one profile cannot silently erase another one the operator still has to fix. `unparsed` is *never* read by anything that builds, activates or lists a connection: it is write-back state only, and a test pins that. The one exception is an *explicit* delete, and it has to clear both halves: a profile that parsed into a `config_error` is a member of `profiles` **and** of `unparsed`, so removing it from one collection alone leaves the writer to re-emit it from the other and the deleted profile comes back on the next load.
- Core never imports Pydantic AI, `korvid.providers`, or any provider identifier. UI never imports `korvid.providers` and never branches on `azure`, `aws`, `gcp`, `ollama`, or `github-copilot`.
- **The vendor-leak guard is scoped to model-adapter identity, not to the word.** It scans a hand-written list of the five modules that used to *choose* an adapter (`core/config.py`, `__main__.py`, `agent/model_profiles.py`, `ui/agent_ui_controller.py`, `ui/widgets/agent_setup_screen.py`), matches only the adapter ids korvid dispatches on, and exempts `core/config.py`'s legacy migration **region** — computed from the module's AST, not per line, because a migration function's body and a legacy translation table's contents name vendors on lines that do not themselves contain the word "legacy". `src/korvid/k8s/csp.py` is **explicitly out of scope and asserted to be so**: mapping node labels to `azure`/`aws`/`gcp` is that module's entire job, and a guard that fired there would be widened until it meant nothing. `agent/model_catalog.py` is out of scope for the same class of reason: it is a capability catalog *keyed by adapter id* (`ModelCatalogEntry(provider="ollama", model="qwen3:8b", …)`), so its vendor strings are data lookups, not dispatch. Every exclusion is listed with its reason, and a test asserts the guarded set and the exclusion set are disjoint so an exclusion can never silence a guarded module.
- Model libraries never receive Kubernetes clients, `WriteOps`, approval callbacks, or audit handles.
- The outbound order is fixed: provider message preparation → `OutboundPolicy` redaction/canonicalization/size enforcement → canonical request snapshot → Pydantic AI transport conversion → network transmission. Transport conversion may re-encode but may never add user-controlled prompt text or tool definitions.
- `REQUEST_SENT` is emitted only after `request_stream()` successfully enters the provider response context.
- Pydantic AI instrumentation and Logfire are disabled by default; no provider callback may receive prompts, tool arguments, responses, or secrets.
- Capabilities are translated only where the source directly asserts an equivalent fact; unknown facts stay `None`. Never infer tool support, context window, or reasoning from provider/model names.
- Dependency floors are exact: `pydantic-ai-slim>=2.35.3,<3`; extras `provider-openai`, `provider-anthropic`, `provider-google`, `provider-bedrock` each pin `pydantic-ai-slim[<sdk>]>=2.35.3,<3`. The `[agent]` extra also declares `httpx2>=2.12` because korvid constructs the clients it hands to Pydantic AI providers.
- **Azure keeps its own adapter.** Legacy `provider: azure` migrates to `azure:<model>`, not to `openai:<model>`. Azure authenticates with the raw `api-key` header (or an Entra token provider) — never a generic `Authorization: Bearer`. The `azure` adapter is built with `pydantic_ai.providers.azure.AzureProvider`, and the `entra` extra and `EntraCredentialSource` survive the legacy deletion because Azure's `provider-default` method needs them.
- **`auth.method: none` is not universal.** An adapter whose SDK cannot construct a keyless client rejects `none` at build time with a named error. Azure is such an adapter: `AzureProvider(api_key=None)` and `AzureProvider(api_key="")` both raise `UserError`, and `api_key=None` additionally falls back to `AZURE_OPENAI_API_KEY` from the ambient environment. korvid refuses the combination itself rather than letting the SDK decide.
- **No ambient-environment credential fallback.** Every SDK client is constructed with an explicit `api_key` argument. Where a profile genuinely carries no credential — a keyless custom OpenAI-compatible endpoint such as a local Ollama or vLLM server — korvid passes the explicit sentinel `KEYLESS_API_KEY_SENTINEL` so the SDK's own `OPENAI_API_KEY`/`OLLAMA_API_KEY` lookup can never smuggle an unrelated key onto the wire. The sentinel is only ever used for a profile whose `endpoint` is set to a non-vendor host; a profile pointing at `api.openai.com` with `auth.method: none` is refused, never sentinel-authenticated.
- **Security invariants are asserted on the wire, through public seams.** Which header carries a credential, which URL a request reaches, and what body a setting produces are proven by capturing the `httpx2.Request` an SDK builds through an `httpx2.MockTransport` mounted on the client korvid supplies. No test in this plan calls a private SDK helper (`AsyncAzureOpenAI._build_request`, `._prepare_options`, `BaseClient.auth_headers`, `._api_version`) to establish a security fact.
- **A migration never silently changes an endpoint's meaning.** Where a legacy value cannot be carried across as-is (the Azure resource URL is the only such case), the migration rewrites it to the shape the new adapter requires, preserves any information the old shape encoded (the deployment name), and records a warning naming both the old and new value.
- The base install imports no Pydantic AI module. Missing provider extras produce an actionable install hint and never fall back to another provider.
- An unknown active profile, unknown model adapter, invalid auth method, or invalid provider options prevents agent construction; the TUI may start with the agent disabled and a visible warning.
- **A half-migrated adapter disables the agent visibly; it never misroutes.** Between Task 2 and Task 14 the running transport is still the legacy one, which knows only the legacy provider ids. Any profile whose adapter that transport cannot serve (`anthropic`, `google`, `bedrock`) yields *no* provider and a warning naming the adapter and the task that will enable it — never a silently substituted OpenAI connection. Task 14 removes the restriction by replacing the transport, and the guard test for it is deleted in the same commit.
- **Every constant shared by two layers lives in a leaf module.** `providers/adapter_extras.py` is stdlib-only and imports nothing from `korvid`; both `providers/adapter_catalog.py` and `providers/pydantic_factory.py` import from it, and `providers/registry.py` and `providers/plugin_registry.py` take `BUILTIN_ADAPTERS` from it too. No shared table is defined in a module that also imports one of its consumers.
- Automatic multi-provider fallback is **not** enabled.
- **This network cannot resolve public PyPI directly, but its proxy does.** `uv.lock` is produced only by the `Relock` workflow (Task 11). Never run `uv lock` locally; never commit a lock resolved through a mirror. Verifying a library's API locally is a *separate* activity, and it has already been done for every signature in the API baseline table: create a throwaway venv outside the repo through the global uv proxy (`uv venv <scratch> && uv pip install --python <scratch>/bin/python 'pydantic-ai-slim[openai,anthropic,google,bedrock]==2.35.3'`) and reflect on it. That install has been performed and succeeded — `pydantic-ai-slim 2.35.3` plus all four provider extras resolved — so the baseline table is observed fact, not an intention. That venv never touches `pyproject.toml`, `uv.lock`, or `.venv`, which is exactly why it is permitted where `uv lock` is not: it leaves no mirror-scoped URL behind.
- Run `uv run tach check` whenever imports cross packages. Every test contains at least one `assert` or `pytest.raises(..., match=...)`. No bare `except:`; no bare `# type: ignore`.

## File Structure

| Path | Responsibility | Task |
| --- | --- | --- |
| `src/korvid/core/config.py` | Immutable `AgentAuthConfig`/`AgentProfileConfig`/`AgentProfilesConfig`, new-shape parser, legacy migration, `save_agent_profiles` writer, derived legacy scalars (deleted in Task 17) | 1, 2, 3, 17 |
| `src/korvid/agent/model_profiles.py` (new) | Public, provider-free descriptor/catalog vocabulary: `SetupField`, `SetupFieldKind`, `EndpointRequirement`, `AuthMethodDescriptor`, `ModelAdapterDescriptor`, `ModelAdapterCatalog`, `DeviceLoginPrompt`, reference helpers, re-export of the three config dataclasses | 5 |
| `src/korvid/providers/adapter_catalog.py` (new) | `ProviderModelCatalog`: built-in descriptors + entry-point plugin descriptors (selected-only loading), model listing, connection test, device auth | 6, 16 |
| `src/korvid/providers/plugin_registry.py` | Adds `entry_point_names()` and `metadata()`; loses built-in alias sets in Task 17 | 6, 17 |
| `src/korvid/providers/adapter_extras.py` (new) | Single stdlib-only leaf module: the immutable adapter id → extra name + import-probe table, `BUILTIN_ADAPTERS`, `KEYLESS_API_KEY_SENTINEL` and `install_hint()`. Imports nothing from `korvid`, so the catalog, the factory, `registry.py` and `plugin_registry.py` can all share it without a cycle | 6, 14, 17 |
| `src/korvid/providers/pydantic_messages.py` (new) | Canonical message/tool → Pydantic AI `ModelMessage`/`ToolDefinition` conversion, pure | 12 |
| `src/korvid/providers/pydantic_model.py` (new) | `PydanticModelProvider(LLMProvider)`: `request_stream()` driving, event translation, usage, `REQUEST_SENT`, cancellation-safe close | 13 |
| `src/korvid/providers/pydantic_factory.py` (new) | Per-adapter `Model` construction, `AdapterExtraMissing`, credential resolution | 14 |
| `src/korvid/providers/registry.py` | `create_profile_provider()` replaces `create_provider()`; keeps `EntraCredentialSource` for Azure `provider-default` | 14, 17 |
| `src/korvid/providers/github_copilot.py` | Device login + `CopilotAuth` httpx2 auth hook + Copilot model listing | 16 |
| `src/korvid/ui/widgets/agent_setup_screen.py` | Profile manager + descriptor-driven stages; no provider ids | 8, 9, 10 |
| `src/korvid/ui/widgets/agent_setup_fields.py` (new) | Pure declarative field prompt/coercion helpers | 9 |
| `src/korvid/ui/agent_ui_controller.py` | Holds `AgentProfilesConfig`, `apply_profiles`, `:model` on the active profile | 10 |
| `src/korvid/__main__.py` | Builds and injects the catalog, builds providers from profiles, persists profiles | 3, 7, 14, 17 |
| `src/korvid/providers/entra.py` | **Kept**, not deleted: gains `access_token()` so Azure's `provider-default` can hand `AsyncAzureOpenAI` a token provider | 14 |
| `pyproject.toml` | `pydantic-ai-slim` + `httpx2` in `[agent]`, four `provider-*` extras, deptry module-name map (`entra` extra is **kept** — Azure's `provider-default` needs it) | 11 |
| `docs/agent.md`, `docs/provider-plugins.md`, `docs/airgap.md`, `docs/threat-model.md`, `docs/release-notes/unreleased.md` | Operator documentation and migration notes | 18 |

Test files created: `tests/core/test_config_profiles.py`, `tests/agent/test_model_profiles.py`, `tests/providers/test_adapter_catalog.py`, `tests/providers/test_adapter_extras.py`, `tests/providers/test_pydantic_messages.py`, `tests/providers/test_pydantic_model.py`, `tests/providers/test_pydantic_factory.py`, `tests/providers/test_pydantic_contracts.py`, `tests/providers/test_copilot_catalog.py`, `tests/ui/test_agent_setup_profiles.py`.

Test files deleted (Task 17): `tests/agent/test_setup.py`, `tests/providers/test_configurator.py`, `tests/providers/test_openai_compat.py`. `tests/providers/test_entra.py` is **kept**, and so is `tests/providers/test_ollama.py` — the Ollama tuning knobs it covers must keep passing until Task 17 proves parity through the new transport. `tests/ui/test_agent_setup_screen.py` is **migrated in Task 10, not deleted**: a file that Task 10 rewrites case-by-case cannot also be removed wholesale in Task 17.

---

## Commit group 1 — Named profile config domain (Tasks 1–4)

### Task 1: Immutable profile dataclasses and the new config shape

**Files:**
- Modify: `src/korvid/core/config.py`
- Create: `tests/core/test_config_profiles.py`

**Interfaces:**
- Produces:
  - `AgentAuthConfig(method: str, settings: Mapping[str, object] = {})` with the non-`__init__` field `settings_error: str | None`
  - `AgentProfileConfig(model: str, endpoint: str | None = None, auth: AgentAuthConfig = AgentAuthConfig("none"), options: Mapping[str, object] = {})` with the non-`__init__` field `options_error: str | None` and the property `config_error: str | None`
  - `AgentProfilesConfig(active: str | None = None, profiles: Mapping[str, AgentProfileConfig] = {}, unparsed: Mapping[str, object] = {})` with property `active_profile: AgentProfileConfig | None`; `profiles` preserves file order, `unparsed` carries the raw YAML value of every entry the parser could not fully model (write-back only, see Task 3)
  - `_parse_bounded_options(value: Any, *, root: str) -> tuple[dict[str, object], str | None]`
  - `AGENT_PROFILE_NAME_MAX_LENGTH: int = 100`
  - `is_valid_profile_name(name: str) -> bool`
  - `KorvidConfig.agent_profiles: AgentProfilesConfig`
- Consumes: existing `load_config`, `_opt_str`, `_parse_agent_options` and its private helpers (`_AgentOptionCounters`, `_AgentOptionsError`, `_parse_agent_option_mapping`, `_parse_agent_option_value`, `_raise_if_secret_key_segment`), and the `warnings` list convention in `core/config.py`.

- [ ] **Step 1: Write the failing parser tests**

Create `tests/core/test_config_profiles.py`:

```python
"""Provider-neutral agent profile parsing (spec: provider-neutral model profiles)."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.core.config import (
    AGENT_PROFILE_NAME_MAX_LENGTH,
    AgentAuthConfig,
    AgentProfileConfig,
    AgentProfilesConfig,
    is_valid_profile_name,
    load_config,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_multiple_profiles_round_trip_into_the_domain(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
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
""",
    )
    cfg = load_config(path)
    assert list(cfg.agent_profiles.profiles) == ["production", "local"]
    assert cfg.agent_profiles.active == "production"
    local = cfg.agent_profiles.profiles["local"]
    assert local.model == "ollama:llama3"
    assert local.endpoint == "http://localhost:11434"
    assert local.auth == AgentAuthConfig(method="none", settings={})
    assert local.options["num_ctx"] == 16384
    assert local.config_error is None


def test_active_profile_selects_the_exact_named_entry(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: prod_east
  profiles:
    prod-east:
      model: openai:gpt-4o
    prod_east:
      model: openai:gpt-4o-mini
""",
    )
    cfg = load_config(path)
    active = cfg.agent_profiles.active_profile
    assert active is not None
    assert active.model == "openai:gpt-4o-mini"


def test_unknown_active_profile_disables_the_agent_with_a_warning(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: missing
  profiles:
    local:
      model: ollama:llama3
""",
    )
    cfg = load_config(path)
    assert cfg.agent_profiles.active is None
    assert cfg.agent_profiles.active_profile is None
    assert any("agent.active" in warning for warning in cfg.warnings)


def test_nested_option_mappings_are_copy_owned_and_immutable(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
      options:
        nested:
          depth: 1
        items: [1, 2]
""",
    )
    cfg = load_config(path)
    options = cfg.agent_profiles.profiles["local"].options
    with pytest.raises(TypeError, match="does not support item assignment"):
        options["nested"]["depth"] = 2  # type: ignore[index]  # proving immutability
    assert options["items"] == (1, 2)


def test_profile_order_follows_the_file_not_an_alphabetical_sort(tmp_path: Path) -> None:
    """Insertion order is the contract the wizard and `:model` list against."""
    path = _write(
        tmp_path,
        """
agent:
  active: zulu
  profiles:
    zulu:
      model: openai:gpt-4o
    alpha:
      model: openai:gpt-4o-mini
    mike:
      model: ollama:llama3
""",
    )
    cfg = load_config(path)
    assert list(cfg.agent_profiles.profiles) == ["zulu", "alpha", "mike"]


def test_oversized_options_are_rejected_and_recorded_on_the_profile(tmp_path: Path) -> None:
    """`options` goes through the same bounded validator as `agent.options`."""
    blob = "x" * 4096
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
      options:
        blob: "{blob}"
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.profiles["local"]
    assert profile.options == {}
    assert profile.options_error is not None
    assert profile.config_error == profile.options_error
    assert any("agent.profiles[local].options" in warning for warning in cfg.warnings)


def test_an_inline_secret_in_auth_settings_is_refused(tmp_path: Path) -> None:
    """A profile stores references; a key that looks like a secret is a bug."""
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: openai:gpt-4o
      auth:
        method: environment
        api_key: sk-inline-not-a-reference
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.profiles["local"]
    assert profile.auth.settings == {}
    assert profile.auth.settings_error is not None
    assert profile.config_error == profile.auth.settings_error
    assert any("agent.profiles[local].auth" in warning for warning in cfg.warnings)
    assert not any("sk-inline-not-a-reference" in warning for warning in cfg.warnings)


def test_an_environment_reference_key_is_not_treated_as_a_secret(tmp_path: Path) -> None:
    """`key: OPENAI_API_KEY` names a variable; only the *key name* is bounded."""
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: openai:gpt-4o
      auth:
        method: environment
        key: OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.profiles["local"]
    assert profile.auth.settings == {"key": "OPENAI_API_KEY"}
    assert profile.config_error is None


def test_revalidating_an_already_frozen_profile_is_idempotent() -> None:
    """Rebuilding a profile from a frozen one must not fail on its tuples."""
    first = AgentProfileConfig(model="ollama:llama3", options={"stop": ["a", "b"]})
    assert first.options["stop"] == ("a", "b")
    second = AgentProfileConfig(model=first.model, options=first.options)
    assert second.options == first.options
    assert second.options_error is None


@pytest.mark.parametrize(
    "instance",
    [
        AgentAuthConfig(method="none"),
        AgentProfileConfig(model="openai:gpt-4o"),
        AgentProfilesConfig(),
    ],
)
def test_frozen_profile_dataclasses_are_unhashable(instance: object) -> None:
    """They hold mutable-by-identity proxies; hashing one would be a lie."""
    with pytest.raises(TypeError, match="unhashable type"):
        hash(instance)


def test_invalid_profile_names_are_dropped_with_a_warning(tmp_path: Path) -> None:
    long_name = "x" * (AGENT_PROFILE_NAME_MAX_LENGTH + 1)
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
    "bad name":
      model: openai:gpt-4o
    {long_name}:
      model: openai:gpt-4o
""",
    )
    cfg = load_config(path)
    assert set(cfg.agent_profiles.profiles) == {"local"}
    assert any("invalid profile name" in warning for warning in cfg.warnings)


def test_profile_without_a_model_reference_is_dropped(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
    broken:
      endpoint: http://example.invalid
""",
    )
    cfg = load_config(path)
    assert set(cfg.agent_profiles.profiles) == {"local"}
    assert any("broken" in warning and "model" in warning for warning in cfg.warnings)


def test_an_unmodellable_profile_is_kept_as_a_raw_mapping_but_never_reaches_the_runtime(
    tmp_path: Path,
) -> None:
    """Dropping a profile from the domain must not amount to deleting it."""
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
    "bad name":
      model: openai:gpt-4o
    broken:
      endpoint: http://example.invalid
""",
    )
    cfg = load_config(path)
    profiles = cfg.agent_profiles
    assert set(profiles.profiles) == {"local"}
    assert set(profiles.unparsed) == {"bad name", "broken"}
    assert profiles.unparsed["broken"] == {"endpoint": "http://example.invalid"}
    assert profiles.active_profile is profiles.profiles["local"]


def test_a_rejected_options_block_is_retained_verbatim_for_write_back(
    tmp_path: Path,
) -> None:
    """The operator has to be able to fix the block korvid refused to load."""
    blob = "x" * 4096
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
      options:
        blob: "{blob}"
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.profiles["local"]
    assert profile.options == {}
    assert profile.options_error is not None
    assert cfg.agent_profiles.unparsed["local"] == {
        "model": "ollama:llama3",
        "options": {"blob": blob},
    }


@pytest.mark.parametrize("name", ["prod", "prod-east", "prod_east", "a.b", "x" * 100])
def test_valid_profile_names(name: str) -> None:
    assert is_valid_profile_name(name)


@pytest.mark.parametrize("name", ["", " prod", "prod east", "prod/east", "x" * 101, "naïve"])
def test_invalid_profile_names(name: str) -> None:
    assert not is_valid_profile_name(name)


def test_profiles_config_defaults_are_empty() -> None:
    empty = AgentProfilesConfig()
    assert empty.active is None
    assert empty.active_profile is None
    assert empty.profiles == {}
    assert empty.unparsed == {}
    assert AgentProfileConfig(model="ollama:llama3").auth.method == "none"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/core/test_config_profiles.py -q`
Expected: FAIL — `ImportError: cannot import name 'AGENT_PROFILE_NAME_MAX_LENGTH' from 'korvid.core.config'`.

- [ ] **Step 3: Make the bounded option validator reusable**

`core/config.py` already validates `agent.options` through `_parse_agent_options`, which
enforces every bound a profile needs: max depth 4, at most 64 keys per mapping, at most
64 items per list, 2048 bytes per string, 16 KiB serialized total, 120 characters of
path, and a refusal of any key segment in `_SECRET_OPTION_KEY_SEGMENTS`
(`secret`, `password`, `token`, `api_key`, `apikey`, `authorization`, `credential`).
Profiles must not get a second, weaker copy of those rules, so extract the body and
keep the existing entry point as a thin wrapper:

```python
def _parse_bounded_options(value: Any, *, root: str) -> tuple[dict[str, object], str | None]:
    """Validate *value* as a bounded, secret-free option mapping.

    *root* is the configuration path the messages name, so the same rules
    guard `agent.options`, a profile's `options` and a profile's `auth`
    settings without any of them inventing its own limits.

    Returns:
        The accepted mapping and `None`, or `{}` and a reason. The reason
        names the offending *path*, never the offending value.
    """
    if not isinstance(value, Mapping):
        return {}, f"{root} must be a mapping with string keys"
    counters = _AgentOptionCounters(root=root)
    try:
        parsed = _parse_agent_option_mapping(value, path=root, depth=1, counters=counters)
        serialized = json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except _AgentOptionsError as exc:
        return {}, str(exc)
    except (TypeError, ValueError) as exc:
        return {}, f"{root} could not be serialized safely: {type(exc).__name__}"
    if len(serialized) > _MAX_AGENT_OPTIONS_SERIALIZED_BYTES:
        return (
            {},
            f"{root} exceeds max serialized budget {_MAX_AGENT_OPTIONS_SERIALIZED_BYTES} bytes",
        )
    return parsed, None


def _parse_agent_options(value: Any) -> tuple[dict[str, object], str | None]:
    """`agent.options`, validated. Thin wrapper over `_parse_bounded_options`."""
    return _parse_bounded_options(value, root="agent.options")
```

Three adjustments make the extracted validator correct for its new callers:

1. `_AgentOptionCounters` gains `root: str = "agent.options"`. **Three** messages inside
   `_parse_agent_option_mapping` and `_parse_agent_option_value` hardcode the string
   `"agent.options"` instead of deriving it from the path; all three take `counters`,
   so all three become `f"{counters.root} …"`:
   - `config.py:1023` — `_AgentOptionsError("agent.options must use string keys")`
   - `config.py:1037` — `f"agent.options exceeds max {_MAX_AGENT_OPTIONS_KEYS} mapping keys"`
   - `config.py:1062` — `f"agent.options exceeds max {_MAX_AGENT_OPTIONS_LIST_ITEMS} list items"`

   Every other message in those two functions already interpolates
   `_agent_options_path(path)`, and `_parse_bounded_options` seeds `path=root`, so those
   follow the caller automatically. Without this fix a profile's rejection message would
   name a configuration key the operator never wrote — `agent.options` for a fault in
   `agent.profiles[local].options`, which sends them editing the wrong block. Grep
   afterwards to prove none is left: `rg -n '"agent\.options' src/korvid/core/config.py`
   must return only `_parse_agent_options`'s own `root=` default.
2. `_parse_agent_option_value` currently accepts `Mapping` and `list`; a frozen profile
   holds `tuple`s, so re-validating one raises `_AgentOptionsError(... got tuple)`.
   Widen the sequence branch from `isinstance(value, list)` to
   `isinstance(value, list | tuple)`. Re-validation happens on every rebuild
   (`AgentProfileConfig(model=old.model, options=old.options)`), so without this the
   wizard's edit path fails on any profile that has a list option.
3. `_raise_if_secret_key_segment` keeps its existing segment list unchanged. `key` is
   deliberately **not** a secret segment: `auth.settings.key` names an environment
   variable or a keyring entry, which is a reference, not a secret. `api_key`, however,
   *is* in the list, so an operator who pastes a literal key under `auth.api_key` is
   refused — which is exactly the leak this guard exists to stop.

Add the matching unit tests to `tests/core/test_config.py`:

```python
def test_bounded_options_accept_a_previously_frozen_tuple_value() -> None:
    from korvid.core.config import _parse_bounded_options

    parsed, error = _parse_bounded_options({"stop": ("a", "b")}, root="options")
    assert error is None
    assert parsed == {"stop": ["a", "b"]}


def test_bounded_options_refuse_an_inline_secret_key() -> None:
    from korvid.core.config import _parse_bounded_options

    parsed, error = _parse_bounded_options({"api_key": "sk-inline"}, root="auth")
    assert parsed == {}
    assert error is not None
    assert "sk-inline" not in error


def test_bounded_options_name_the_caller_s_root_in_limit_messages() -> None:
    from korvid.core.config import _parse_bounded_options

    _parsed, error = _parse_bounded_options(
        {str(index): index for index in range(65)}, root="agent.profiles[local].options"
    )
    assert error is not None
    assert error.startswith("agent.profiles[local].options")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({1: "x"}, id="non-string-key"),
        pytest.param({str(index): index for index in range(65)}, id="mapping-keys"),
        pytest.param({"stop": list(range(65))}, id="list-items"),
    ],
)
def test_no_bounded_options_message_hardcodes_the_agent_options_root(
    value: dict[object, object],
) -> None:
    """The three messages that used to spell `agent.options` by hand.

    Each of them is reachable only through a different branch, so one
    parametrized case per branch is what proves the constant is gone
    rather than moved.
    """
    from korvid.core.config import _parse_bounded_options

    _parsed, error = _parse_bounded_options(value, root="agent.profiles[local].auth")
    assert error is not None
    assert error.startswith("agent.profiles[local].auth")
    assert "agent.options" not in error
```

- [ ] **Step 4: Add the dataclasses and name validation**

In `src/korvid/core/config.py`, add `import re` to the existing imports if absent, and insert above `class ConfigMigrationError`:

```python
#: Profile names are operator-defined identifiers, never normalized:
#: `prod-east` and `prod_east` are distinct keys so a mistyped selector can
#: never silently activate a different connection.
AGENT_PROFILE_NAME_MAX_LENGTH: int = 100
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def is_valid_profile_name(name: str) -> bool:
    """Whether *name* is a usable `agent.profiles` key."""
    return (
        type(name) is str
        and 0 < len(name) <= AGENT_PROFILE_NAME_MAX_LENGTH
        and _PROFILE_NAME_RE.match(name) is not None
    )


def _freeze_config_value(value: object) -> object:
    """Recursively copy-own a parsed value: mappings become read-only proxies,
    sequences become tuples, scalars pass through."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_config_value(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_config_value(item) for item in value)
    return value


def _freeze_config_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return cast("Mapping[str, object]", _freeze_config_value(dict(value)))


def _validated_config_mapping(
    value: Mapping[str, object], *, root: str
) -> tuple[Mapping[str, object], str | None]:
    """Bound-check *value*, then freeze it.

    Validation runs on the *raw* mapping, before freezing, so the size,
    depth and secret-key rules see the values a human wrote rather than
    the proxies and tuples the freeze produces. On rejection the mapping
    collapses to empty and the reason travels with it — a profile that
    silently kept half its options would be worse than one that visibly
    has none.

    *root* is the short path name the message uses (`options` or `auth`);
    the parser prefixes it with the profile name when it warns.

    Returns:
        The frozen mapping and `None`, or an empty mapping and the
        rejection reason. The reason never quotes a value.
    """
    sanitized, error = _parse_bounded_options(value, root=root)
    if error is not None:
        return MappingProxyType({}), error
    return _freeze_config_mapping(sanitized), None


@dataclass(frozen=True)
class AgentAuthConfig:
    """How a profile authenticates, as bounded copy-owned configuration.

    Core does not interpret provider-specific methods: `method` is one of
    the five common ids (`none`, `environment`, `keyring`,
    `provider-default`, `device-login`) and `settings` carries the
    method-specific *references* (never secret values) an adapter
    descriptor validates.
    """

    method: str = "none"
    settings: Mapping[str, object] = field(default_factory=dict)
    #: Why `settings` was emptied, or None. Not an `__init__` argument and
    #: not compared: two configs that differ only in *why* a rejected
    #: mapping is empty are the same configuration.
    settings_error: str | None = field(default=None, init=False, compare=False)

    # A frozen dataclass would otherwise be hashable, but `settings` is a
    # `MappingProxyType` over a dict — hashing this would raise from deep
    # inside `hash(tuple(...))` at some unrelated call site instead of here.
    __hash__ = None  # type: ignore[assignment]  # frozen but genuinely unhashable

    def __post_init__(self) -> None:
        settings, error = _validated_config_mapping(self.settings, root="auth")
        object.__setattr__(self, "settings", settings)
        object.__setattr__(self, "settings_error", error)


@dataclass(frozen=True)
class AgentProfileConfig:
    """One named model connection."""

    model: str
    endpoint: str | None = None
    auth: AgentAuthConfig = field(default_factory=AgentAuthConfig)
    options: Mapping[str, object] = field(default_factory=dict)
    #: Why `options` was emptied, or None. See `AgentAuthConfig.settings_error`.
    options_error: str | None = field(default=None, init=False, compare=False)

    __hash__ = None  # type: ignore[assignment]  # frozen but genuinely unhashable

    def __post_init__(self) -> None:
        options, error = _validated_config_mapping(self.options, root="options")
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "options_error", error)

    @property
    def config_error(self) -> str | None:
        """The first reason this profile cannot be trusted, or None.

        Anything that builds a provider from a profile checks this and
        refuses rather than connecting with silently discarded settings.
        """
        return self.options_error or self.auth.settings_error


@dataclass(frozen=True)
class AgentProfilesConfig:
    """The configured connection collection and which one is active.

    `profiles` preserves the order the entries appeared in the file. That
    order is the operator's, and it is what the wizard's profile list and
    the `:model` picker render.

    `unparsed` is the escape hatch that keeps a save honest: it maps the
    file key of every entry korvid could **not** fully model — an invalid
    name, a non-mapping, a missing `model:`, or a profile whose `options`
    or `auth` block was rejected — to that entry's raw YAML value. Nothing
    in the runtime reads it: it is not consulted by `active_profile`, by
    the wizard's list, by `:model`, or by any provider construction. Its
    only consumer is `save_agent_profiles` (Task 3), which writes those
    values back verbatim so saving one profile cannot delete another the
    operator still has to repair. The values are the objects `yaml.safe_load`
    already built for this same file, held opaquely and never interpreted,
    so retaining them costs nothing the loader had not already allocated.
    """

    active: str | None = None
    profiles: Mapping[str, AgentProfileConfig] = field(default_factory=dict)
    #: Raw, unmodelled `agent.profiles` entries keyed by file key. Opaque;
    #: never read by the runtime. Not compared: two configurations that
    #: differ only in text korvid refused to interpret are the same
    #: configuration as far as the agent is concerned.
    unparsed: Mapping[str, object] = field(default_factory=dict, compare=False)

    __hash__ = None  # type: ignore[assignment]  # frozen but genuinely unhashable

    def __post_init__(self) -> None:
        object.__setattr__(self, "profiles", MappingProxyType(dict(self.profiles)))
        object.__setattr__(self, "unparsed", MappingProxyType(dict(self.unparsed)))

    @property
    def active_profile(self) -> AgentProfileConfig | None:
        """The active profile, or None when unset or unknown.

        Only `profiles` is consulted — an `unparsed` entry can never
        become the active connection.
        """
        if self.active is None:
            return None
        return self.profiles.get(self.active)
```

`__hash__ = None` inside a `@dataclass(frozen=True)` body is honoured: the decorator
detects an explicitly set `__hash__` in the class namespace and leaves it alone rather
than synthesising one. mypy --strict reports `Incompatible types in assignment
(expression has type "None", base class "object" defined the type as "Callable[[],
int]")`, whose code is `assignment` — hence the narrow ignore with its reason. Both
behaviours were confirmed against CPython 3.11 and mypy --strict before this plan was
written; do not replace the ignore with `eq=False` (that would break `==`, which the
tests rely on) or with `unsafe_hash` (which would synthesise the hash this forbids).

Ensure `from collections.abc import Mapping`, `from types import MappingProxyType` and `from typing import cast` are imported at the top of the module (add whichever is missing).

- [ ] **Step 5: Parse the new shape**

Add below the dataclasses in `src/korvid/core/config.py`:

```python
def _parse_profile_entry(
    name: str, raw: object, warnings: list[str]
) -> AgentProfileConfig | None:
    """One `agent.profiles.<name>` entry, or None when unusable."""
    if not isinstance(raw, dict):
        warnings.append(f"agent.profiles[{name}] is not a mapping; the profile was ignored")
        return None
    model = _opt_str(raw.get("model"))
    if model is None:
        warnings.append(f"agent.profiles[{name}] has no model reference; the profile was ignored")
        return None
    auth_raw = raw.get("auth")
    auth_map: dict[str, Any] = auth_raw if isinstance(auth_raw, dict) else {}
    method = _opt_str(auth_map.get("method")) or "none"
    settings = {key: value for key, value in auth_map.items() if key != "method"}
    options_raw = raw.get("options")
    options: Mapping[str, object] = options_raw if isinstance(options_raw, dict) else {}
    profile = AgentProfileConfig(
        model=model,
        endpoint=_opt_str(raw.get("endpoint")),
        auth=AgentAuthConfig(method=method, settings=settings),
        options=options,
    )
    # The dataclasses validated and (on rejection) emptied these mappings;
    # the parser is the layer that knows the profile's name, so it is the
    # layer that turns the reason into an operator-facing warning. The
    # profile is *kept* — with an empty mapping and a recorded reason — so
    # `:ai` can show it and let the operator fix it, but anything that
    # builds a provider refuses while `config_error` is set.
    if profile.options_error is not None:
        warnings.append(
            f"agent.profiles[{name}].options was rejected: {profile.options_error}"
        )
    if profile.auth.settings_error is not None:
        warnings.append(
            f"agent.profiles[{name}].auth was rejected: {profile.auth.settings_error}"
        )
    return profile


def _parse_agent_profiles(agent_raw: dict[str, Any], warnings: list[str]) -> AgentProfilesConfig:
    """Parse the `agent.active`/`agent.profiles` shape."""
    raw_profiles = agent_raw.get("profiles")
    if not isinstance(raw_profiles, dict):
        warnings.append("agent.profiles is not a mapping; no agent profile was loaded")
        return AgentProfilesConfig()
    profiles: dict[str, AgentProfileConfig] = {}
    unparsed: dict[str, object] = {}
    reported_invalid_name = False
    for raw_name, raw_entry in raw_profiles.items():
        name = raw_name if type(raw_name) is str else ""
        if not is_valid_profile_name(name):
            if not reported_invalid_name:
                warnings.append(
                    "agent.profiles contains an invalid profile name; the entry was ignored"
                )
                reported_invalid_name = True
            unparsed[str(raw_name)] = raw_entry
            continue
        parsed = _parse_profile_entry(name, raw_entry, warnings)
        if parsed is None:
            # korvid could not model it; keep the text so a later save
            # rewrites it untouched instead of deleting the operator's work.
            unparsed[name] = raw_entry
            continue
        if parsed.config_error is not None:
            # Kept, but with an emptied block. The rejected block is the
            # one thing the operator has to edit, so it must survive a save.
            unparsed[name] = raw_entry
        profiles[name] = parsed
    active = _opt_str(agent_raw.get("active"))
    if active is not None and active not in profiles:
        warnings.append(
            f"agent.active names an unknown profile {active!r}; the agent is disabled"
        )
        active = None
    return AgentProfilesConfig(active=active, profiles=profiles, unparsed=unparsed)
```

An `unparsed` key can collide with a `profiles` key only in the kept-but-rejected case,
and there both refer to the same entry — Task 3's writer resolves that deterministically
by letting the parsed values win field by field and falling back to the raw text only for
the block that was rejected. An invalid *name* is stringified with `str(raw_name)` because
a YAML key can be an int or a bool; `is_valid_profile_name` has already refused it, so the
stringified form is only ever a dictionary key on the way back out to the file.

- [ ] **Step 6: Store the parsed profiles on `KorvidConfig`**

In `src/korvid/core/config.py`, add the field to `KorvidConfig` immediately above `agent_enabled`:

```python
    #: Named model connection profiles (`agent.active` / `agent.profiles`).
    #: The single source of truth for provider configuration; the legacy
    #: scalars below are derived from `agent_profiles.active_profile`
    #: during the compatibility cycle and are removed with it.
    agent_profiles: AgentProfilesConfig = field(default_factory=AgentProfilesConfig)
```

In `load_config`, immediately after `agent_raw` is computed, add:

```python
    agent_profiles = (
        _parse_agent_profiles(agent_raw, warnings) if "profiles" in agent_raw else AgentProfilesConfig()
    )
```

and pass `agent_profiles=agent_profiles,` in the `KorvidConfig(...)` construction. If `warnings` is initialized after this point in the current function body, move the `warnings: list[str] = []` initialization above it.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/core/test_config_profiles.py tests/core/test_config.py -q`
Expected: PASS (all tests in both files).

- [ ] **Step 8: Lint, typecheck and commit**

```bash
uv run ruff check --fix src/korvid/core/config.py tests/core/test_config_profiles.py
uv run ruff format src/korvid/core/config.py tests/core/test_config_profiles.py
uv run mypy src/korvid/core/config.py
git add src/korvid/core/config.py tests/core/test_config_profiles.py
git commit -m "feat: parse named agent model profiles" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Legacy agent configuration migration

**Files:**
- Modify: `src/korvid/core/config.py`
- Modify: `tests/core/test_config_profiles.py`

**Interfaces:**
- Consumes: `AgentProfileConfig`, `AgentAuthConfig`, `AgentProfilesConfig`, `_parse_agent_profiles` (Task 1).
- Produces:
  - `LEGACY_PROFILE_NAME: str = "default"`
  - `_legacy_model_reference(provider: str, model: str) -> str`
  - `_legacy_options(agent_raw: dict[str, Any], provider: str, warnings: list[str]) -> dict[str, object]`
  - `_legacy_ollama_options(ollama_raw: dict[str, Any], warnings: list[str]) -> dict[str, object]`
  - `_legacy_ollama_number(key: str, value: object, cast_to: type[int] | type[float], warnings: list[str]) -> int | float | None`
  - `_migrate_azure_endpoint(base_url: str) -> tuple[str, str | None, str | None]`
  - `_migrate_legacy_agent(agent_raw: dict[str, Any], warnings: list[str]) -> AgentProfilesConfig`

- [ ] **Step 1: Write the failing migration tests**

Append to `tests/core/test_config_profiles.py`:

```python
def test_legacy_ollama_config_becomes_the_default_profile(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  provider: ollama
  model: llama3
  base_url: http://localhost:11434
  ollama:
    num_ctx: 8192
    think: true
""",
    )
    cfg = load_config(path)
    assert cfg.agent_profiles.active == "default"
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.model == "ollama:llama3"
    assert profile.endpoint == "http://localhost:11434"
    assert profile.auth.method == "none"
    assert profile.options["num_ctx"] == 8192
    assert profile.options["think"] is True
    # The legacy transport was the native `/api/chat` route, and migration
    # must not silently switch an existing install to `/v1`.
    assert profile.options["native_api"] is True


def test_a_new_ollama_profile_defaults_to_the_shared_route(tmp_path: Path) -> None:
    """`native_api` is a migration artefact, not a default for new profiles.

    Only `_legacy_options` sets it. A profile written in the new shape is
    parsed verbatim, so an operator who never ran the old config gets the
    common adapter.
    """
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
      endpoint: http://localhost:11434
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert "native_api" not in profile.options


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("num_ctx: \"8192\"", ("num_ctx", 8192), id="numeric-string-int"),
        pytest.param("num_ctx: 8192.0", ("num_ctx", 8192), id="float-to-int"),
        pytest.param("temperature: \"0.2\"", ("temperature", 0.2), id="numeric-string-float"),
        pytest.param("seed: 0", ("seed", 0), id="zero-seed-is-not-absent"),
        pytest.param("num_predict: 192", ("num_predict", 192), id="strict-int-kept"),
        pytest.param("keep_alive: 5m", ("keep_alive", "5m"), id="non-numeric-verbatim"),
    ],
)
def test_legacy_ollama_numbers_keep_the_old_parser_s_coercion(
    tmp_path: Path, raw: str, expected: tuple[str, object]
) -> None:
    """The pre-profile parser coerced these; Task 17 deletes it.

    `OllamaOptions` is a plain dataclass, so a surviving `"8192"` would be
    sent as a JSON string and would reach `context_window_tokens` as a
    `str`. Migration is the last place that can still fix it.
    """
    path = _write(tmp_path, f"agent:\n  provider: ollama\n  model: llama3\n  ollama:\n    {raw}\n")
    profile = load_config(path).agent_profiles.active_profile
    assert profile is not None
    key, value = expected
    assert profile.options[key] == value
    assert type(profile.options[key]) is type(value)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("num_ctx: nope", id="not-a-number"),
        pytest.param("num_ctx: true", id="bool-is-not-a-number"),
        pytest.param("temperature: .inf", id="non-finite"),
        pytest.param("num_ctx: [1]", id="wrong-shape"),
        pytest.param('num_predict: "192"', id="strict-int-refuses-a-numeric-string"),
        pytest.param("num_predict: 1.9", id="strict-int-refuses-a-fraction"),
        pytest.param("num_predict: 0", id="strict-int-refuses-non-positive"),
    ],
)
def test_an_uncoercible_legacy_ollama_value_is_dropped_with_a_warning(
    tmp_path: Path, raw: str
) -> None:
    """Dropped, not defaulted, and never fatal to the whole profile.

    The old parser substituted its own fallback here. That fallback is now
    the field default on `OllamaOptions`, which a migrated profile still
    reaches through `native_api: True`, so dropping the key restores the
    same effective value *and* names the line to fix. Non-finite floats
    matter especially: the bounded validator refuses them, so carrying one
    through would reject the entire migrated profile over one knob.

    `num_predict` is the odd one out: `num_ctx` accepts `"8192"` because
    its old parser did, while `num_predict`'s old parser refused a numeric
    string, a fraction, a `bool` and a non-positive value. Migration keeps
    each contract as it was rather than unifying them, because unifying
    them would change what an existing config means.
    """
    key = raw.split(":")[0]
    path = _write(tmp_path, f"agent:\n  provider: ollama\n  model: llama3\n  ollama:\n    {raw}\n")
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.config_error is None
    assert key not in profile.options
    assert any(f"agent.ollama.{key}" in warning for warning in cfg.warnings)


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai-compat", "gpt-4o-mini", "openai:gpt-4o-mini"),
        ("openai", "gpt-4o", "openai:gpt-4o"),
        ("vllm", "qwen", "openai:qwen"),
        ("azure", "gpt-4o", "azure:gpt-4o"),
        ("ollama", "llama3", "ollama:llama3"),
        ("github-copilot", "gpt-4o", "github-copilot:gpt-4o"),
        ("company-llm", "v2", "company-llm:v2"),
    ],
)
def test_legacy_provider_names_translate_to_model_references(
    tmp_path: Path, provider: str, model: str, expected: str
) -> None:
    path = _write(
        tmp_path,
        f"""
agent:
  provider: {provider}
  model: {model}
  base_url: https://example.invalid/v1
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.model == expected


def test_legacy_api_key_env_becomes_environment_auth(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  provider: openai-compat
  model: gpt-4o
  base_url: https://api.openai.com/v1
  api_key_env: OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.auth == AgentAuthConfig(method="environment", settings={"key": "OPENAI_API_KEY"})


def test_legacy_entra_auth_becomes_provider_default(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com
  auth:
    method: entra
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.auth.method == "provider-default"
    assert profile.model == "azure:gpt-4o"
    assert profile.endpoint == "https://example.openai.azure.com"
    assert any("azure" in warning.lower() for warning in cfg.warnings)


def test_legacy_azure_api_key_keeps_the_azure_adapter(tmp_path: Path) -> None:
    """Azure is not an OpenAI-compatible endpoint: it must not become `openai:`.

    The `openai:` adapter would send `Authorization: Bearer <key>`; Azure
    OpenAI authenticates an API key with the raw `api-key` header, so a
    migration onto `openai:` would silently break every key-based Azure
    install.
    """
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com
  api_key_env: AZURE_OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.model == "azure:gpt-4o"
    assert profile.auth == AgentAuthConfig(
        method="environment", settings={"key": "AZURE_OPENAI_API_KEY"}
    )


def test_legacy_azure_deployment_url_is_reduced_to_the_resource_url(tmp_path: Path) -> None:
    """The legacy client posted to `<base_url>/chat/completions`.

    So a working legacy `base_url` was deployment-scoped. `AzureProvider`
    wants the *resource* URL and appends `/openai/deployments/<model>`
    itself; handing it the old value produces
    `.../openai/deployments/my-dep/openai/chat/completions`, a 404. The
    migration therefore truncates at `/openai` and keeps the deployment
    name as an option instead of throwing it away.
    """
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com/openai/deployments/my-dep
  api_key_env: AZURE_OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.endpoint == "https://example.openai.azure.com"
    assert profile.options["azure_deployment"] == "my-dep"
    assert any(
        "https://example.openai.azure.com/openai/deployments/my-dep" in warning
        and "was rewritten" in warning
        for warning in cfg.warnings
    )


def test_legacy_azure_v1_url_is_reduced_without_inventing_a_deployment(
    tmp_path: Path,
) -> None:
    """`.../openai/v1` is Azure's v1 surface: no deployment is encoded in it."""
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com/openai/v1
  api_key_env: AZURE_OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.endpoint == "https://example.openai.azure.com"
    assert "azure_deployment" not in profile.options
    assert any("openai/v1" in warning for warning in cfg.warnings)


def test_a_bare_azure_resource_url_migrates_unchanged_and_silently(
    tmp_path: Path,
) -> None:
    """Nothing to rewrite means nothing to warn about."""
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com/
  api_key_env: AZURE_OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.endpoint == "https://example.openai.azure.com"
    assert not any("was rewritten" in warning for warning in cfg.warnings)


def test_a_non_azure_endpoint_is_never_rewritten(tmp_path: Path) -> None:
    """Only the `azure` adapter's endpoint changes meaning; leave the rest alone."""
    path = _write(
        tmp_path,
        """
agent:
  provider: openai-compat
  model: gpt-4o
  base_url: https://gateway.corp.invalid/openai/v1
  api_key_env: CORP_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.endpoint == "https://gateway.corp.invalid/openai/v1"


def test_legacy_copilot_auth_stays_device_login(tmp_path: Path) -> None:
    path = _write(tmp_path, "agent:\n  provider: github-copilot\n  model: gpt-4o\n")
    cfg = load_config(path)
    profile = cfg.agent_profiles.active_profile
    assert profile is not None
    assert profile.auth.method == "device-login"


def test_explicitly_disabled_legacy_agent_produces_no_active_profile(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "agent:\n  enabled: false\n  provider: ollama\n  model: llama3\n  base_url: http://x:11434\n",
    )
    cfg = load_config(path)
    assert cfg.agent_profiles.active is None
    assert "default" in cfg.agent_profiles.profiles


def test_new_shape_wins_over_legacy_with_a_warning(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  provider: ollama
  model: llama3
  base_url: http://localhost:11434
  active: production
  profiles:
    production:
      model: openai:gpt-4o
""",
    )
    cfg = load_config(path)
    assert set(cfg.agent_profiles.profiles) == {"production"}
    assert any("legacy" in warning for warning in cfg.warnings)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/core/test_config_profiles.py -q -k "legacy or new_shape or azure"`
Expected: FAIL — `assert cfg.agent_profiles.active == "default"` fails with `None` (no migration exists yet).

The `-k` expression must be quoted. Unquoted, the shell splits `legacy or new_shape`
into three words, `pytest` takes `legacy` as the expression and then treats `or` and
`new_shape` as *file paths*, exiting with `ERROR: file or directory not found: or`
before running a single test — a fake RED that proves nothing.

- [ ] **Step 3: Implement the migration**

Add to `src/korvid/core/config.py` below `_parse_agent_profiles`:

```python
#: The in-memory profile name a legacy `agent.provider` config migrates into.
LEGACY_PROFILE_NAME: str = "default"

#: Legacy provider names that meant "an OpenAI-compatible endpoint".
#: `azure` is deliberately absent: Azure OpenAI authenticates with the raw
#: `api-key` header (or an Entra token) rather than a bearer token, so it
#: keeps its own `azure:` adapter instead of collapsing into `openai:`.
_LEGACY_OPENAI_COMPAT_NAMES: frozenset[str] = frozenset(
    {"openai-compat", "openai", "vllm", "github", "anthropic", "claude"}
)

#: Legacy provider names whose credential handling changed with the
#: migration and therefore warrant a one-line warning on load.
_LEGACY_REVIEW_NAMES: frozenset[str] = frozenset({"azure"})

#: Legacy `agent.ollama.*` keys carried into the migrated profile's options
#: so the writer's new shape preserves the operator's tuning.
_LEGACY_OLLAMA_KEYS: tuple[str, ...] = (
    "num_ctx",
    "temperature",
    "seed",
    "think",
    "keep_alive",
    "num_predict",
)

#: The legacy `agent.ollama.*` knobs whose pre-profile parser coerced a
#: numeric *string* to a number, mapped to the type it produced.
#: `OllamaOptions` is a plain dataclass with no validation, so a `"8192"`
#: that survived migration would be sent as a JSON string and would land
#: in `context_window_tokens` as a `str`.
_LEGACY_OLLAMA_NUMERIC_KEYS: Mapping[str, type[int] | type[float]] = MappingProxyType(
    {"num_ctx": int, "seed": int, "temperature": float}
)

#: `num_predict` is deliberately absent from the coercion table above.
#: Its pre-profile parser was the *strict* one: it refused a numeric
#: string, a fractional float, a `bool` and a non-positive value outright
#: instead of coercing them (`tests/core/test_config.py` pins all four).
#: Migration keeps that contract by dropping the key, which lands on
#: `OllamaOptions.num_predict = None` — the same effective value the old
#: fallback produced.
_LEGACY_OLLAMA_STRICT_INT_KEYS: frozenset[str] = frozenset({"num_predict"})

#: Legacy auth methods → the five common method ids.
_LEGACY_AUTH_METHODS: Mapping[str, str] = MappingProxyType(
    {
        "api_key": "environment",
        "entra": "provider-default",
        "device-login": "device-login",
        "none": "none",
    }
)


def _legacy_model_reference(provider: str, model: str) -> str:
    """`provider:model` for a legacy provider name.

    Translated at this one parser boundary: nothing downstream branches on
    a legacy provider name again.
    """
    if provider in _LEGACY_OPENAI_COMPAT_NAMES:
        return f"openai:{model}"
    return f"{provider}:{model}"


def _legacy_auth(agent_raw: dict[str, Any], provider: str) -> AgentAuthConfig:
    auth_value = agent_raw.get("auth")
    auth_map: dict[str, Any] = auth_value if isinstance(auth_value, dict) else {}
    legacy_method = _opt_str(auth_map.get("method"))
    api_key_env = _opt_str(agent_raw.get("api_key_env"))
    if legacy_method is None:
        if provider == "github-copilot":
            legacy_method = "device-login"
        elif api_key_env:
            legacy_method = "api_key"
        else:
            legacy_method = "none"
    method = _LEGACY_AUTH_METHODS.get(legacy_method, legacy_method)
    settings: dict[str, object] = {}
    if method == "environment" and api_key_env:
        settings["key"] = api_key_env
    return AgentAuthConfig(method=method, settings=settings)


def _legacy_options(
    agent_raw: dict[str, Any], provider: str, warnings: list[str]
) -> dict[str, object]:
    """Options carried into the migrated profile.

    Only `provider: ollama` had a legacy tuning block, so only `ollama`
    reads `agent.ollama.*`. Copying those keys into, say, an `openai`
    profile would invent settings the operator never wrote and that the
    adapter would then have to ignore.

    Migrated `ollama` profiles also get `native_api: True`. The legacy
    transport was `OllamaProvider`'s `/api/chat` route, which returns
    per-tool-call reasoning the OpenAI dialect cannot carry (Task 17). A
    *new* `ollama:` profile defaults to the shared route; an *existing*
    install keeps the transport it was already running, because a
    migration that silently changes the wire protocol is not "read
    without changes".

    Values are copied verbatim with one exception: the numeric knobs are
    coerced (`num_ctx`, `seed`, `temperature`) or strictly validated
    (`num_predict`), because the pre-profile parser did that and Task 17
    deletes it along with the scalars. Anything that will not coerce is
    **dropped with a warning** rather than replaced by an invented
    default — the default the old parser substituted is `OllamaOptions`'
    own field default, which a migrated profile still reaches through
    `native_api: True`, so dropping restores exactly the old effective
    value while also telling the operator which line to fix.
    """
    options: dict[str, object] = {}
    if provider == "ollama":
        ollama_value = agent_raw.get("ollama")
        ollama_raw: dict[str, Any] = ollama_value if isinstance(ollama_value, dict) else {}
        options.update(_legacy_ollama_options(ollama_raw, warnings))
        options["native_api"] = True
    extra = agent_raw.get("options")
    if isinstance(extra, dict):
        options.update(extra)
    return options


def _legacy_ollama_options(
    ollama_raw: dict[str, Any], warnings: list[str]
) -> dict[str, object]:
    """The `agent.ollama.*` block as profile options. See `_legacy_options`."""
    options: dict[str, object] = {}
    for key in _LEGACY_OLLAMA_KEYS:
        if key not in ollama_raw:
            continue
        value = ollama_raw[key]
        if key in _LEGACY_OLLAMA_STRICT_INT_KEYS:
            # `bool` is an `int` subclass, so YAML `true` must not pass here.
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                warnings.append(
                    f"agent.ollama.{key}: must be a positive integer — the value was dropped"
                )
                continue
            options[key] = value
            continue
        cast_to = _LEGACY_OLLAMA_NUMERIC_KEYS.get(key)
        if cast_to is None:
            options[key] = value
            continue
        coerced = _legacy_ollama_number(key, value, cast_to, warnings)
        if coerced is not None:
            options[key] = coerced
    return options


def _legacy_ollama_number(
    key: str, value: object, cast_to: type[int] | type[float], warnings: list[str]
) -> int | float | None:
    """One permissive numeric knob, coerced the way the old parser was.

    Returns `None` for "drop it" — the caller tests `is not None` rather
    than truthiness, because `seed: 0` and `temperature: 0.0` are both
    valid values that a truthiness test would silently discard.
    """
    # `bool` is an `int` subclass, so YAML `true` would coerce to 1.
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        warnings.append(f"agent.ollama.{key}: must be a number — the value was dropped")
        return None
    try:
        coerced = cast_to(value)
    except (TypeError, ValueError, OverflowError):
        # `.inf` reaches `int()` as OverflowError, `.nan` as ValueError.
        warnings.append(f"agent.ollama.{key}: must be a number — the value was dropped")
        return None
    if isinstance(coerced, float) and not isfinite(coerced):
        # `.inf`/`.nan` survive `float()`, and the bounded validator
        # refuses them — which would reject the whole migrated profile
        # over one tuning knob the old parser quietly replaced.
        warnings.append(f"agent.ollama.{key}: must be finite — the value was dropped")
        return None
    return coerced


def _migrate_azure_endpoint(base_url: str) -> tuple[str, str | None, str | None]:
    """Reduce a legacy Azure `base_url` to the resource URL it was built from.

    The legacy transport posted to `f"{base_url}/chat/completions"`, so a
    working legacy value was already deployment- or version-scoped —
    `https://x.openai.azure.com/openai/deployments/<name>` or
    `https://x.openai.azure.com/openai/v1`. `AzureProvider` takes the
    *resource* URL and builds the `/openai/...` path itself; given the old
    value it appends rather than replaces, producing
    `.../openai/deployments/<name>/openai/chat/completions` or
    `.../openai/v1/openai/deployments/<model>/chat/completions`. Both 404.

    Everything from the first `/openai` segment onward is therefore
    dropped, and any deployment name it encoded is returned so the caller
    can preserve it rather than lose it.

    Returns:
        The resource URL, the deployment name the old URL encoded (or
        None), and a warning naming both the old and the new value (or
        None when nothing was rewritten).
    """
    split = urlsplit(base_url)
    segments = [segment for segment in split.path.split("/") if segment]
    resource = urlunsplit((split.scheme, split.netloc, "", "", ""))
    if "openai" not in segments:
        if not segments and not split.query and not split.fragment:
            return resource, None, None
        # A path korvid does not recognise: leave the value alone rather
        # than guess. The adapter will surface the failure with the real
        # URL in it, which is more useful than a silent rewrite.
        return base_url, None, None
    tail = segments[segments.index("openai") + 1 :]
    deployment = tail[1] if len(tail) >= 2 and tail[0] == "deployments" else None
    warning = (
        f"agent.base_url {base_url!r} was rewritten to {resource!r} for the azure "
        "adapter, which builds the /openai/deployments path itself"
    )
    if deployment is not None:
        warning += f"; the deployment name {deployment!r} was kept as options.azure_deployment"
    return resource, deployment, warning


def _migrate_legacy_agent(
    agent_raw: dict[str, Any], warnings: list[str]
) -> AgentProfilesConfig:
    """Normalize a legacy `agent.provider` config into one `default` profile."""
    provider_raw = agent_raw.get("provider")
    if not isinstance(provider_raw, str) or not provider_raw.strip():
        return AgentProfilesConfig()
    provider = _canonicalize_provider_name(provider_raw)
    model = _opt_str(agent_raw.get("model"))
    if model is None:
        warnings.append("agent.provider is set but agent.model is missing; the agent is disabled")
        return AgentProfilesConfig()
    endpoint = _opt_str(agent_raw.get("base_url"))
    options = _legacy_options(agent_raw, provider, warnings)
    if provider == "azure" and endpoint is not None:
        endpoint, deployment, endpoint_warning = _migrate_azure_endpoint(endpoint)
        if deployment is not None:
            options.setdefault("azure_deployment", deployment)
        if endpoint_warning is not None:
            warnings.append(endpoint_warning)
    profile = AgentProfileConfig(
        model=_legacy_model_reference(provider, model),
        endpoint=endpoint,
        auth=_legacy_auth(agent_raw, provider),
        options=options,
    )
    if provider in _LEGACY_REVIEW_NAMES:
        # The credential *reference* survives, but where Entra was implicit
        # the method is now spelled out. Saying so beats a silent 401.
        warnings.append(
            f"agent.provider {provider!r} migrated to an {provider} profile; "
            "check auth.method (provider-default for Entra ID) in :ai"
        )
    enabled = agent_raw.get("enabled", True) is not False
    return AgentProfilesConfig(
        active=LEGACY_PROFILE_NAME if enabled else None,
        profiles={LEGACY_PROFILE_NAME: profile},
    )
```

Add `from urllib.parse import urlsplit, urlunsplit` to the module's imports.

The two broken URLs `_migrate_azure_endpoint` exists to prevent were reproduced against
openai 3.5.0 by capturing the request an `AsyncAzureOpenAI` builds through an
`httpx2.MockTransport`:

| `azure_endpoint` handed to the SDK | URL the SDK actually requests |
| --- | --- |
| `https://x.openai.azure.com` | `https://x.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=…` ✔ |
| `https://x.openai.azure.com/openai/deployments/my-dep` | `https://x.openai.azure.com/openai/deployments/my-dep/openai/chat/completions?api-version=…` ✘ |
| `https://x.openai.azure.com/openai/v1` | `https://x.openai.azure.com/openai/v1/openai/deployments/gpt-4o/chat/completions?api-version=…` ✘ |

That table is the *reason* for the migration, so it is checked in rather than trusted.
Add the reproduction to `tests/core/test_config_profiles.py` so a future openai/httpx2
bump that changes the URL shape fails here instead of in production:

```python
@pytest.mark.parametrize(
    ("azure_endpoint", "expected_path"),
    [
        (
            "https://x.openai.azure.com",
            "/openai/deployments/gpt-4o/chat/completions",
        ),
        (
            "https://x.openai.azure.com/openai/deployments/my-dep",
            "/openai/deployments/my-dep/openai/chat/completions",
        ),
        (
            "https://x.openai.azure.com/openai/v1",
            "/openai/v1/openai/deployments/gpt-4o/chat/completions",
        ),
    ],
)
def test_the_azure_sdk_builds_the_url_from_the_resource_root(
    azure_endpoint: str, expected_path: str
) -> None:
    """Why `_migrate_azure_endpoint` strips the deployment segment.

    Only the first row is a working URL. The other two are what an
    operator's pre-migration `base_url` produces once the request is built
    by the SDK instead of by korvid's own string concatenation: the SDK
    treats `azure_endpoint` as the *resource root*, appends `/openai`, and
    inserts `/deployments/<model>` only when the resulting path does not
    already contain a deployment segment.
    """
    openai = pytest.importorskip("openai")
    httpx2 = pytest.importorskip("httpx2")
    seen: list[str] = []

    def _capture(request: Any) -> Any:
        seen.append(str(request.url))
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    client = openai.AzureOpenAI(
        azure_endpoint=azure_endpoint,
        api_key="not-a-real-key",
        api_version="2024-10-21",
        http_client=httpx2.Client(transport=httpx2.MockTransport(_capture)),
    )
    client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    assert len(seen) == 1
    assert seen[0].startswith(f"https://x.openai.azure.com{expected_path}?")
    assert "api-version=2024-10-21" in seen[0]
```

Three deliberate choices here. `pytest.importorskip` is called *inside* the test rather
than at module scope, because `openai` and `httpx2` arrive with the `[agent]` extra and
`tests/core/` must still pass for a contributor who installed korvid without it — a
module-scope skip would take the whole parser suite with it. `Any` is used for the
transport callback's annotation because the modules arrive as `Any` from `importorskip`
and mypy --strict rejects an `Any`-typed name used as a type. And the *synchronous*
`AzureOpenAI` is used, not `AsyncAzureOpenAI`, so the test needs no event loop; the URL
assembly is shared between the two clients. Add `from typing import Any` to the test
module's imports.

**This test skips on the branch until Task 11 installs the extra, and a skip here is
not a pass.** It is the only executable evidence in the plan that the Azure URL that
`_migrate_azure_endpoint` strips is the one `AzureProvider` rebuilds, so it must be
*run*, not merely written. Two gates enforce that: Task 11 Step 9 re-runs this file with
`-rs` and requires zero skipped, and Task 19 Step 5 repeats the check
tree-wide. Until then, record the skip deliberately:

```bash
uv run pytest -p no:tach tests/core/test_config_profiles.py -q -rs \
  -k "azure_url or deployment_scoped"
```

Expected at this point in the branch: `2 skipped` with the reason
`could not import 'openai'`. Expected from Task 11 onward: `2 passed`, `0 skipped`.
If it still skips after Task 11 the extra did not install, and the URL table is
unverified no matter how green the suite looks.

The same rule applies to every `pytest.importorskip("pydantic_ai")` module this plan
adds — `tests/providers/test_pydantic_messages.py`, `test_pydantic_model.py`,
`test_pydantic_factory.py`, `test_pydantic_contracts.py` and
`test_github_copilot_factory.py`. The scripted-stream reproduction in
`tests/providers/test_pydantic_model.py` (Task 13) is the one that matters most: it is
the only place the streaming contract — fragmented tool-call assembly, text deltas, the
usage record, no `request_sent` when the transport refuses, and cancellation — is
exercised without a network. A silent skip there would let Task 13 be accepted on an
unexecuted test.

- [ ] **Step 4: Give the new shape precedence in `load_config`**

Replace the `agent_profiles = ...` assignment added in Task 1 Step 5 with:

```python
    if "profiles" in agent_raw:
        if "provider" in agent_raw:
            warnings.append(
                "agent.profiles is present; the legacy agent.provider fields were ignored"
            )
        agent_profiles = _parse_agent_profiles(agent_raw, warnings)
    else:
        agent_profiles = _migrate_legacy_agent(agent_raw, warnings)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/core/test_config_profiles.py tests/core/test_config.py -q`
Expected: PASS.

- [ ] **Step 6: Lint, typecheck and commit**

```bash
uv run ruff check --fix src/korvid/core/config.py tests/core/test_config_profiles.py
uv run ruff format src/korvid/core/config.py tests/core/test_config_profiles.py
uv run mypy src/korvid/core/config.py
git add src/korvid/core/config.py tests/core/test_config_profiles.py
git commit -m "feat: migrate legacy agent config into a default profile" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: Profile writer and derived legacy scalars

**Files:**
- Modify: `src/korvid/core/config.py`
- Modify: `src/korvid/__main__.py:1086-1096` (`_persist_agent_settings`)
- Modify: `tests/core/test_config_profiles.py`
- Modify: `tests/core/test_config.py` (Step 6 — the legacy scalars become a projection)

**Interfaces:**
- Consumes: `AgentProfilesConfig`, `AgentProfileConfig`, `AgentAuthConfig`, `LEGACY_PROFILE_NAME`, `_atomic_write_text`.
- Produces:
  - `save_agent_profiles(path: Path, profiles: AgentProfilesConfig, *, model_tier: str | None = None) -> None`
  - `LEGACY_AGENT_KEYS: tuple[str, ...]` — the legacy keys the writer removes
  - `_derive_legacy_scalars(profiles: AgentProfilesConfig, warnings: list[str]) -> _LegacyAgentScalars` — internal, deleted in Task 17

- [ ] **Step 1: Write the failing writer and derivation tests**

Extend the import block at the *top* of `tests/core/test_config_profiles.py` — ruff's
`E402`/`I` rules reject a module-level import further down the file — so that it reads:

```python
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from korvid.core.config import (
    AGENT_PROFILE_NAME_MAX_LENGTH,
    AgentAuthConfig,
    AgentProfileConfig,
    AgentProfilesConfig,
    is_valid_profile_name,
    load_config,
    save_agent_profiles,
)
```

Then append the tests:

```python
def test_writer_emits_only_the_new_shape_and_preserves_unrelated_config(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
namespace: kube-system
agent:
  provider: ollama
  model: llama3
  base_url: http://localhost:11434
  api_key_env: OLD_KEY
  enabled: true
  ollama:
    num_ctx: 4096
  rules:
    - "Never include node names in an answer."
""",
    )
    save_agent_profiles(
        path,
        AgentProfilesConfig(
            active="production",
            profiles={
                "production": AgentProfileConfig(
                    model="openai:gpt-4o",
                    endpoint="https://api.openai.com/v1",
                    auth=AgentAuthConfig(method="environment", settings={"key": "OPENAI_API_KEY"}),
                    options={"temperature": 0},
                )
            },
        ),
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["namespace"] == "kube-system"
    assert raw["agent"]["rules"] == ["Never include node names in an answer."]
    assert raw["agent"]["active"] == "production"
    assert raw["agent"]["profiles"]["production"] == {
        "model": "openai:gpt-4o",
        "endpoint": "https://api.openai.com/v1",
        "auth": {"method": "environment", "key": "OPENAI_API_KEY"},
        "options": {"temperature": 0},
    }
    for legacy_key in ("provider", "base_url", "model", "api_key_env", "enabled", "ollama"):
        assert legacy_key not in raw["agent"]


def test_writer_round_trips_through_the_parser(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    profiles = AgentProfilesConfig(
        active="local",
        profiles={
            "local": AgentProfileConfig(
                model="ollama:llama3",
                endpoint="http://localhost:11434",
                auth=AgentAuthConfig(method="none"),
                options={"num_ctx": 16384},
            )
        },
    )
    save_agent_profiles(path, profiles)
    assert load_config(path).agent_profiles == profiles


def test_writer_round_trips_a_nested_option_mapping(tmp_path: Path) -> None:
    """The frozen `options` are proxies at *every* depth.

    `yaml.safe_dump` raises `RepresenterError` on a `MappingProxyType`, so
    a writer that only copied the top level would dump fine for flat
    options and explode the first time an operator nested one.
    """
    path = tmp_path / "config.yaml"
    profiles = AgentProfilesConfig(
        active="local",
        profiles={
            "local": AgentProfileConfig(
                model="ollama:llama3",
                auth=AgentAuthConfig(
                    method="environment",
                    settings={"key": "OPENAI_API_KEY", "headers": {"x-team": "sre"}},
                ),
                options={"retry": {"attempts": 3, "backoff": [1, 2, 4]}, "num_ctx": 8192},
            )
        },
    )
    save_agent_profiles(path, profiles)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["agent"]["profiles"]["local"]["options"] == {
        "retry": {"attempts": 3, "backoff": [1, 2, 4]},
        "num_ctx": 8192,
    }
    assert raw["agent"]["profiles"]["local"]["auth"] == {
        "method": "environment",
        "key": "OPENAI_API_KEY",
        "headers": {"x-team": "sre"},
    }
    assert load_config(path).agent_profiles == profiles


def test_saving_one_profile_does_not_delete_an_unparsable_one(tmp_path: Path) -> None:
    """A profile korvid could not model is still the operator's work."""
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
    broken:
      endpoint: http://example.invalid
""",
    )
    profiles = load_config(path).agent_profiles
    save_agent_profiles(path, profiles)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["agent"]["profiles"]["broken"] == {"endpoint": "http://example.invalid"}
    assert raw["agent"]["profiles"]["local"]["model"] == "ollama:llama3"
    assert load_config(path).agent_profiles.active_profile is not None


def test_saving_writes_a_rejected_options_block_back_untouched(tmp_path: Path) -> None:
    blob = "x" * 4096
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
      options:
        blob: "{blob}"
""",
    )
    profiles = load_config(path).agent_profiles
    save_agent_profiles(path, profiles)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["agent"]["profiles"]["local"]["options"] == {"blob": blob}


def test_a_profile_the_operator_removed_is_not_resurrected(tmp_path: Path) -> None:
    """Preservation applies to what korvid dropped, never to a real delete."""
    path = _write(
        tmp_path,
        """
agent:
  active: keep
  profiles:
    keep:
      model: ollama:llama3
    remove-me:
      model: openai:gpt-4o
""",
    )
    loaded = load_config(path).agent_profiles
    save_agent_profiles(
        path,
        dataclasses.replace(
            loaded, active="keep", profiles={"keep": loaded.profiles["keep"]}
        ),
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(raw["agent"]["profiles"]) == {"keep"}


def test_deleting_a_rejected_profile_removes_it_from_both_collections(
    tmp_path: Path,
) -> None:
    """The delete path has to clear `unparsed` too, or the save undoes it.

    A profile korvid *kept* but whose `options` block it rejected lives in
    `profiles` **and** `unparsed` at once. The writer re-emits every
    `unparsed` entry that has no parsed counterpart, so a caller that
    drops the profile from `profiles` alone hands the writer an orphaned
    raw entry and the deleted profile reappears in the file.
    """
    blob = "x" * 4096
    path = _write(
        tmp_path,
        f"""
agent:
  active: keep
  profiles:
    keep:
      model: ollama:llama3
    broken:
      model: openai:gpt-4o
      options:
        blob: "{blob}"
""",
    )
    loaded = load_config(path).agent_profiles
    assert loaded.profiles["broken"].config_error is not None
    assert "broken" in loaded.unparsed

    # The bug this test exists for: dropping only the parsed half.
    half_deleted = dataclasses.replace(
        loaded, profiles={"keep": loaded.profiles["keep"]}
    )
    save_agent_profiles(path, half_deleted)
    assert "broken" in yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]["profiles"]

    save_agent_profiles(
        path,
        dataclasses.replace(
            loaded,
            active="keep",
            profiles={"keep": loaded.profiles["keep"]},
            unparsed={},
        ),
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(raw["agent"]["profiles"]) == {"keep"}
    assert set(load_config(path).agent_profiles.profiles) == {"keep"}
    assert load_config(path).agent_profiles.unparsed == {}


def test_writer_persists_and_clears_the_model_tier(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    profiles = AgentProfilesConfig(
        active="local", profiles={"local": AgentProfileConfig(model="ollama:llama3")}
    )
    save_agent_profiles(path, profiles, model_tier="low")
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]["model_tier"] == "low"
    save_agent_profiles(path, profiles, model_tier=None)
    assert "model_tier" not in yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]


def test_legacy_scalars_are_derived_from_the_active_profile(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
      endpoint: http://localhost:11434
      auth:
        method: none
      options:
        num_ctx: 8192
""",
    )
    cfg = load_config(path)
    assert cfg.agent_enabled is True
    assert cfg.agent_provider == "ollama"
    assert cfg.agent_model == "llama3"
    assert cfg.agent_base_url == "http://localhost:11434"
    assert cfg.agent_auth_method == "none"
    assert cfg.agent_ollama_num_ctx == 8192


def test_environment_auth_derives_the_legacy_api_key_env(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: cloud
  profiles:
    cloud:
      model: openai:gpt-4o
      endpoint: https://api.openai.com/v1
      auth:
        method: environment
        key: OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    assert cfg.agent_auth_method == "api_key"
    assert cfg.agent_api_key_env == "OPENAI_API_KEY"


def test_keyring_auth_is_not_usable_during_the_compatibility_cycle(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: cloud
  profiles:
    cloud:
      model: openai:gpt-4o
      endpoint: https://api.openai.com/v1
      auth:
        method: keyring
        key: korvid-openai
""",
    )
    cfg = load_config(path)
    assert cfg.agent_auth_method == "keyring"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/core/test_config_profiles.py -q -k "writer or saving or resurrected or legacy_scalars or environment_auth or keyring_auth"`
Expected: FAIL — `ImportError: cannot import name 'save_agent_profiles' from 'korvid.core.config'`. The
import sits at the top of the module, so every test in the file errors on collection; that is the
expected RED state and Step 3 clears it.

- [ ] **Step 3: Implement the writer**

First add the thaw helpers to `src/korvid/core/config.py` immediately below
`_freeze_config_mapping` (Task 1 Step 4), so the freeze and its inverse sit
together and a future change to one is visibly a change to the other:

```python
def _thaw_config_value(value: object) -> object:
    """Recursively undo `_freeze_config_value` for serialization.

    `yaml.safe_dump` has no representer for `MappingProxyType` and raises
    `yaml.representer.RepresenterError` on the first one it meets, so a
    frozen mapping must become a plain `dict` before it reaches the dumper.
    This is the exact inverse of the freeze, applied at every depth — a
    nested `options` block is the ordinary case, not the exotic one
    (`options: {retry: {attempts: 3}}`), and freezing that produced a proxy
    *inside* a proxy, which a single-level `dict(...)` copy would not undo.

    Tuples become lists. `yaml.safe_dump` can already represent a tuple as
    a plain sequence, so this is not a correctness fix; it makes the emitted
    YAML identical whether a value came from a freshly parsed file or from
    a frozen domain object.
    """
    if isinstance(value, Mapping):
        return {str(key): _thaw_config_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw_config_value(item) for item in value]
    return value


def _thaw_config_mapping(value: object) -> dict[str, Any]:
    """*value* as a plain nested dict, or `{}` when it is not a mapping."""
    thawed = _thaw_config_value(value)
    return thawed if isinstance(thawed, dict) else {}
```

Then add the writer itself next to `save_agent_config`:

```python
#: Legacy `agent.*` keys the profile writer removes on every save. The
#: first successful `:ai` save therefore upgrades the file exactly once;
#: unrelated keys (`rules`, `follow`, `disable_in_protected`, …) survive
#: the read-modify-write untouched.
LEGACY_AGENT_KEYS: tuple[str, ...] = (
    "provider",
    "base_url",
    "model",
    "api_key_env",
    "auth",
    "enabled",
    "ollama",
    "options",
)


def _profile_to_raw(profile: AgentProfileConfig, previous: Mapping[str, object]) -> dict[str, Any]:
    """One profile in file shape.

    Everything korvid parsed is rewritten from the domain object, so the
    file and the runtime can never disagree. A block korvid *rejected* is
    the one exception: `previous` is the raw entry this profile was read
    from (empty for a profile the wizard just built), and a rejected
    `options`/`auth` block is copied out of it verbatim. Without that, a
    `:ai` save of an unrelated field would delete the very text the
    operator has to edit to fix the rejection.

    Every value passes through `_thaw_config_value` because the domain
    mappings are frozen and `yaml.safe_dump` cannot represent a
    `MappingProxyType` at any depth.
    """
    raw: dict[str, Any] = {"model": profile.model}
    if profile.endpoint:
        raw["endpoint"] = profile.endpoint
    if profile.auth.settings_error is None:
        auth: dict[str, Any] = {"method": profile.auth.method}
        auth.update(_thaw_config_mapping(profile.auth.settings))
        raw["auth"] = auth
    else:
        preserved_auth = _thaw_config_mapping(previous.get("auth"))
        raw["auth"] = preserved_auth or {"method": profile.auth.method}
    if profile.options_error is not None:
        preserved_options = previous.get("options")
        if preserved_options is not None:
            raw["options"] = _thaw_config_value(preserved_options)
    elif profile.options:
        raw["options"] = _thaw_config_mapping(profile.options)
    return raw


def save_agent_profiles(
    path: Path, profiles: AgentProfilesConfig, *, model_tier: str | None = None
) -> None:
    """Persist the profile collection, preserving unrelated keys.

    Only the new shape is written: the legacy managed keys are removed so a
    saved file has exactly one source of truth for provider configuration.

    Entries korvid could not model (`profiles.unparsed`) are written back
    unchanged. They are re-emitted from that field rather than from the
    file on disk, so an entry the operator genuinely *deleted* in `:ai`
    stays deleted while an entry korvid merely failed to parse survives.
    """
    raw: dict[str, Any] = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text())
        raw = loaded if isinstance(loaded, dict) else {}
    existing = raw.get("agent")
    agent: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    for legacy_key in LEGACY_AGENT_KEYS:
        agent.pop(legacy_key, None)
    if model_tier is not None:
        agent["model_tier"] = model_tier
    else:
        agent.pop("model_tier", None)
    agent["active"] = profiles.active
    written: dict[str, Any] = {
        name: _profile_to_raw(profile, _thaw_config_mapping(profiles.unparsed.get(name)))
        for name, profile in profiles.profiles.items()
    }
    for name, entry in profiles.unparsed.items():
        if name not in written:
            written[name] = _thaw_config_value(entry)
    agent["profiles"] = written
    raw["agent"] = agent
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(raw, sort_keys=False))
```

Two properties of this writer are load-bearing and are pinned by tests in Step 1:

1. **Nothing frozen reaches the dumper.** `yaml.safe_dump({"a": MappingProxyType({"b": 1})})`
   raises `yaml.representer.RepresenterError: cannot represent an object`, and a
   nested `options` mapping is frozen *recursively*, so a one-level `dict(...)`
   copy would still smuggle a proxy into the second level. `_thaw_config_value`
   mirrors `_freeze_config_value` exactly, which is why the two are defined next
   to each other.
2. **Rewriting is not deleting.** Profiles korvid parsed are rebuilt from the domain
   object; profiles it could not parse, and rejected blocks inside profiles it could,
   are copied from `unparsed`. Callers that construct an `AgentProfilesConfig` from an
   existing one (the wizard in Task 8, the controller in Task 10) must use
   `dataclasses.replace(current, active=…, profiles=…)` rather than a bare
   `AgentProfilesConfig(active=…, profiles=…)` so `unparsed` travels with it; the plan
   spells that out at each such call site.
3. **A delete has to clear both halves.** The one case where a key is in `profiles`
   *and* `unparsed` is the kept-but-rejected profile. Because the writer re-emits every
   `unparsed` key that has no parsed counterpart, a caller that removes such a profile
   from `profiles` alone leaves an orphaned raw entry behind and the writer restores the
   profile it was just told to delete. Every delete path therefore drops the key from
   both mappings in the same `dataclasses.replace` — Task 10's `_delete_profile` is the
   only one, and `test_deleting_a_rejected_profile_removes_it_from_both_collections`
   above pins the writer half of the contract.

- [ ] **Step 4: Derive the legacy scalars from the active profile**

Add to `src/korvid/core/config.py` below `_migrate_legacy_agent`:

```python
@dataclass(frozen=True)
class _LegacyAgentScalars:
    """Read-only projection of the active profile onto the pre-profile
    fields the composition root, registry and UI still read.

    Deprecated: this exists only for the compatibility cycle inside this
    migration and is deleted together with its consumers in Task 17.
    There is no second mutable source of truth — every value here is
    computed from `AgentProfilesConfig` during parsing.
    """

    enabled: bool
    provider: str | None
    base_url: str | None
    model: str | None
    api_key_env: str | None
    auth_method: str | None
    options: dict[str, object]


#: The five common auth ids projected back onto the legacy method names
#: `providers/registry.build_credentials` still understands.
_LEGACY_AUTH_BACK: Mapping[str, str] = MappingProxyType(
    {
        "none": "none",
        "environment": "api_key",
        "provider-default": "entra",
        "device-login": "device-login",
    }
)

#: Adapters the *interim* legacy transport cannot serve correctly. It
#: routes `anthropic` through its OpenAI-compatible client (which would
#: send a bearer token to an endpoint that wants `x-api-key`) and has no
#: route at all for `google` or `bedrock`. Until Task 14 replaces the
#: transport, a profile naming one of these disables the agent with a
#: warning rather than connecting to the wrong thing in the wrong way.
#: Deleted in Task 17 together with `_LegacyAgentScalars`.
_ADAPTERS_WITHOUT_LEGACY_TRANSPORT: frozenset[str] = frozenset(
    {"anthropic", "google", "bedrock"}
)


def _legacy_azure_base_url(endpoint: str, model_tag: str, options: Mapping[str, object]) -> str:
    """Re-attach the deployment path Task 2's migration stripped.

    The interim transport posts to `f"{base_url}/chat/completions"`, so it
    needs the deployment-scoped URL that `AzureProvider` will build for
    itself from Task 14 onward. Deleted in Task 17 with the rest of the
    legacy projection.
    """
    declared = options.get("azure_deployment")
    deployment = declared if isinstance(declared, str) and declared else model_tag
    return f"{endpoint.rstrip('/')}/openai/deployments/{deployment}"


def _derive_legacy_scalars(
    profiles: AgentProfilesConfig, warnings: list[str]
) -> _LegacyAgentScalars:
    disabled = _LegacyAgentScalars(False, None, None, None, None, None, {})
    profile = profiles.active_profile
    if profile is None:
        return disabled
    if profile.config_error is not None:
        warnings.append(
            f"the active profile was rejected: {profile.config_error}; the agent is disabled"
        )
        return disabled
    adapter, _, tag = profile.model.partition(":")
    if adapter in _ADAPTERS_WITHOUT_LEGACY_TRANSPORT:
        warnings.append(
            f"the {adapter!r} adapter is not connected yet; the agent is disabled "
            "until the Pydantic AI transport lands"
        )
        return disabled
    base_url = profile.endpoint
    if adapter == "azure" and base_url:
        base_url = _legacy_azure_base_url(base_url, tag, profile.options)
    key = profile.auth.settings.get("key")
    return _LegacyAgentScalars(
        enabled=True,
        provider=adapter or None,
        base_url=base_url,
        model=tag or None,
        api_key_env=key if isinstance(key, str) else None,
        # An unmapped method (`keyring`) is passed through verbatim so the
        # legacy credential builder refuses it instead of silently
        # authenticating with something the operator did not choose.
        auth_method=_LEGACY_AUTH_BACK.get(profile.auth.method, profile.auth.method),
        options=dict(profile.options),
    )
```

`_ADAPTERS_WITHOUT_LEGACY_TRANSPORT` is the only place in this plan where a half-built
feature is visible to a user, and it is deliberately loud: disabling the agent with a
named reason is recoverable, while a bearer token sent to Anthropic's endpoint is a
failed request that looks like a credential problem. Task 14 deletes the constant, the
branch that reads it, and the guard test below, in the same commit that makes those
three adapters real.

Add the guard tests to `tests/core/test_config_profiles.py`:

```python
@pytest.mark.parametrize("adapter", ["anthropic", "google", "bedrock"])
def test_an_adapter_without_a_transport_disables_the_agent(
    tmp_path: Path, adapter: str
) -> None:
    """Deleted in Task 14, when these adapters gain a real transport."""
    path = _write(
        tmp_path,
        f"""
agent:
  active: cloud
  profiles:
    cloud:
      model: {adapter}:some-model
""",
    )
    cfg = load_config(path)
    assert cfg.agent_enabled is False
    assert cfg.agent_provider is None
    assert any(adapter in warning for warning in cfg.warnings)


def test_a_rejected_profile_disables_the_agent(tmp_path: Path) -> None:
    blob = "x" * 4096
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
      endpoint: http://localhost:11434
      options:
        blob: "{blob}"
""",
    )
    cfg = load_config(path)
    assert cfg.agent_enabled is False
    assert any("the agent is disabled" in warning for warning in cfg.warnings)


def test_azure_keeps_a_deployment_scoped_url_during_the_compatibility_cycle(
    tmp_path: Path,
) -> None:
    """Also deleted in Task 14: the legacy transport needs the old URL shape."""
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com/openai/deployments/my-dep
  api_key_env: AZURE_OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    assert cfg.agent_profiles.active_profile is not None
    assert cfg.agent_profiles.active_profile.endpoint == "https://example.openai.azure.com"
    assert cfg.agent_base_url == "https://example.openai.azure.com/openai/deployments/my-dep"
```

In `load_config`, after `agent_profiles` is computed, replace the existing `provider`,
`enabled`, `api_key_env`, `auth_method` and `agent_options` derivations with the two
lines below — in this order, because the `KorvidConfig(...)` call further down reads
both names:

```python
    legacy = _derive_legacy_scalars(agent_profiles, warnings)
    active_profile = agent_profiles.active_profile
    agent_options_error = active_profile.config_error if active_profile is not None else None
```

Delete `provider_raw`, `auth_value`/`auth_raw` and `ollama_value`/`ollama_raw` from
`load_config` in the same edit. Every read of them moves into `_migrate_legacy_agent`
(Task 2) or `_derive_legacy_scalars`, so leaving them behind is both dead code and a
ruff `F841` failure in this task's own lint step — and `ollama_value = agent_raw.get("ollama")`
would additionally survive as a vendor-guard failure in Task 17 that nothing there is
scheduled to fix.

`agent_options_error` keeps being fed, but from the profile rather than from a second
parse. Task 1's dataclasses already ran the bounded validator, so re-parsing here would
create a second source of truth for the same rejection.

Then pass the derived values into `KorvidConfig(...)`:

```python
        agent_enabled=legacy.enabled,
        agent_provider=legacy.provider,
        agent_base_url=legacy.base_url,
        agent_model=legacy.model,
        agent_api_key_env=legacy.api_key_env,
        agent_auth_method=legacy.auth_method,
        agent_options=legacy.options,
        agent_options_error=agent_options_error,
```

Keep the Ollama scalars working by reading the same options mapping:

```python
        agent_ollama_num_ctx=_parse_num_ctx(legacy.options.get("num_ctx")),
        agent_ollama_temperature=_parse_temperature(legacy.options.get("temperature")),
        agent_ollama_seed=_parse_seed(legacy.options.get("seed")),
        agent_ollama_think=legacy.options.get("think") is True,
        agent_ollama_keep_alive=_parse_keep_alive(legacy.options.get("keep_alive")),
        agent_ollama_num_predict=_parse_num_predict(legacy.options.get("num_predict"), warnings),
```

- [ ] **Step 5: Persist profiles from the composition root**

In `src/korvid/__main__.py`, replace `_persist_agent_settings` with a profile-shaped writer and keep the `AgentSettings` overload only until Task 10 removes it:

```python
def _persist_agent_profiles(profiles: AgentProfilesConfig, model_tier: str | None) -> None:
    """Write the profile collection the `:ai` wizard produced back to config.yaml."""
    save_agent_profiles(DEFAULT_CONFIG_PATH, profiles, model_tier=model_tier)
```

Update the import line `from korvid.core.config import (... save_agent_config, ...)` to import `AgentProfilesConfig` and `save_agent_profiles` as well. Leave `_persist_agent_settings` in place (it is still the `ProviderConfigurator`'s persist hook) but re-implement it over the new writer:

```python
def _persist_agent_settings(settings: AgentSettings) -> None:
    """Compatibility shim: the wizard's legacy settings shape, written as a profile."""
    auth_method = _LEGACY_AUTH_METHODS.get(settings.auth_method, settings.auth_method)
    auth_settings: dict[str, object] = (
        {"key": settings.api_key_env} if auth_method == "environment" and settings.api_key_env else {}
    )
    profiles = AgentProfilesConfig(
        active=LEGACY_PROFILE_NAME,
        profiles={
            LEGACY_PROFILE_NAME: AgentProfileConfig(
                # The wizard still speaks legacy provider ids, so the same
                # translator the file migration uses must run here too.
                # `f"{settings.provider}:{settings.model}"` would write
                # `openai-compat:gpt-4o` or `vllm:qwen` — model references
                # that name adapters korvid does not have, so the very next
                # load would disable the agent.
                model=_legacy_model_reference(
                    _canonicalize_provider_name(settings.provider), settings.model
                ),
                endpoint=settings.base_url,
                auth=AgentAuthConfig(method=auth_method, settings=auth_settings),
                options=dict(settings.options),
            )
        },
    )
    _persist_agent_profiles(profiles, settings.model_tier)
```

Import `AgentAuthConfig`, `AgentProfileConfig`, `LEGACY_PROFILE_NAME`, `_LEGACY_AUTH_METHODS`,
`_canonicalize_provider_name` and `_legacy_model_reference` from `korvid.core.config` in the
same import block. Reusing `_LEGACY_AUTH_METHODS` rather than a second inline
`{"api_key": …, "entra": …}` literal keeps one translation table: a method added to one
place and not the other is the class of bug this shim exists to avoid.

Add the shim's own regression test to `tests/test_main_wiring.py`:

```python
def test_the_legacy_persist_shim_writes_a_resolvable_model_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`openai-compat` is not an adapter id; the shim must translate it."""
    import korvid.__main__ as main_module

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(main_module, "DEFAULT_CONFIG_PATH", path)
    main_module._persist_agent_settings(
        AgentSettings(
            provider="openai-compat",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            auth_method="api_key",
        )
    )
    profile = load_config(path).agent_profiles.active_profile
    assert profile is not None
    assert profile.model == "openai:gpt-4o"
    assert profile.auth.method == "environment"
```

(Construct `AgentSettings` with whatever required fields that dataclass declares at this
point in the branch; the assertions above are what matter.)

- [ ] **Step 6: Re-point `tests/core/test_config.py` at the projection**

This is the step that would otherwise be discovered as a red suite in Step 7. Every
legacy scalar in `tests/core/test_config.py` now arrives through
`_migrate_legacy_agent` → `AgentProfileConfig` → `_derive_legacy_scalars`, and that
path has entry conditions the old direct parse did not. Three rules cover every
failure; apply them mechanically rather than case by case.

**Rule L1 — a legacy fixture needs a `model:`.** `_migrate_legacy_agent` refuses
`agent.provider` without `agent.model` (it warns and returns an empty
`AgentProfilesConfig`), so with no model there is no active profile and *every*
projected scalar is its disabled default. Add a `model:` line to each fixture that
sets `agent.provider` and asserts on a projected scalar:

| Test | Fixture today | Add |
|---|---|---|
| `test_load_from_yaml` | `provider: anthropic` | `model: llama3` (and Rule L2) |
| `test_explicit_agent_off_wins` | `provider: anthropic`, `enabled: false` | `model: llama3` (and Rule L2) |
| `test_auth_method_parsed` | `provider: github-copilot` | `model: gpt-4o` |
| `test_auth_method_backcompat_api_key` | `provider: openai-compat` | `model: gpt-4o` |
| `test_auth_method_backcompat_none` | `provider: ollama` | `model: llama3` |
| all 17 `test_ollama_*` cases | `provider: ollama` | `model: llama3` |

`test_explicit_agent_off_wins` passes today *by accident* once the model is missing —
it asserts `agent_enabled is False`, which is also what a refused migration produces.
Fixing the fixture is what makes it test the explicit off switch again rather than the
absence of a model.

**Rule L2 — a legacy fixture needs a connected adapter.** `anthropic`, `google` and
`bedrock` are in `_ADAPTERS_WITHOUT_LEGACY_TRANSPORT`, so a profile naming one of them
projects to the disabled scalars until Task 14. The two `anthropic` fixtures above are
not testing Anthropic — they are testing "a provider is present" and "an explicit off
switch wins" — so change them to `openai-compat` and assert
`cfg.agent_provider == "openai-compat"`. Do **not** delete
`test_an_adapter_without_a_transport_disables_the_agent` from
`tests/core/test_config_profiles.py` to make room; that test is the one that owns this
behaviour, and Task 14 deletes it.

**Rule L3 — `agent.options` only reaches `agent_options` through a profile.**
`_load_agent_options_config` (`tests/core/test_config.py:17`) writes an `agent:` block
with nothing but `options:`, so after this task it produces no profile and every one of
the ~25 `test_agent_options_*` cases sees `agent_options == {}` and
`agent_options_error is None`. Give the helper a provider and a model:

```python
def _load_agent_options_config(tmp_path: Path, options: object) -> KorvidConfig:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {"agent": {"provider": "openai-compat", "model": "gpt-4o", "options": options}},
            sort_keys=False,
        )
    )
    return load_config(path)
```

`openai-compat`, not `ollama`: `_legacy_options` adds `native_api: True` to a migrated
Ollama profile, and these tests compare the *exact* option mapping. Apply the same two
keys to the three `test_agent_options_*` cases that write their YAML inline instead of
through the helper.

Two follow-on details inside Rule L3:

- **Sequences come back as tuples.** `AgentProfileConfig` freezes `options` recursively
  (`_freeze_config_value`: mappings → `MappingProxyType`, sequences → `tuple`) and
  `_derive_legacy_scalars` copies the mapping shallowly. A `MappingProxyType` compares
  equal to a `dict`, but a `tuple` does not compare equal to a `list`, so in
  `test_agent_options_parses_valid_nested_data` the expected `"fallbacks"` value becomes
  `("gpt-4o-mini", {"label": "backup", "weight": 1})`. This is a real behaviour change
  and it is the intended one — the frozen mapping is what makes `agent_options`
  un-mutable by a consumer, and every downstream reader serialises it to JSON, where a
  tuple and a list are the same array.
- **The rejection messages keep their wording but change their root.** The bounded
  validator's messages are built from `counters.root`, which is `"agent.options"` when
  `load_config` seeds it and `"options"` when `AgentProfileConfig.__post_init__` does.
  Every assertion in these tests is a *substring* check (`"64 mapping keys" in ...`,
  `"string keys" in ...`, `"finite float" in ...`), so they keep passing unchanged. Do
  not "fix" them to assert the full message: `agent_options_error` is now
  `profile.config_error`, and pinning the whole string here would duplicate
  `tests/core/test_config_profiles.py`'s ownership of that wording.
- **A rejected mapping still reports `agent_options == {}`, but by a different route.**
  `_derive_legacy_scalars` returns the *disabled* projection as soon as
  `profile.config_error is not None`, so `legacy.options` is `{}` — while
  `agent_options_error` is read straight off `active_profile.config_error` and is
  therefore still populated. Both halves of every `…_rejects_…` case hold. The disabled
  branch also appends a `"the active profile was rejected: …"` warning that
  `load_config` did not emit before; no test in this file asserts on `cfg.warnings` in
  that range, so nothing breaks, but do not add one here — `tests/core/test_config_profiles.py`
  owns that warning.

Nothing else in the file moves. `test_defaults_when_no_file` still holds (the disabled
projection returns exactly `KorvidConfig`'s field defaults), `test_agent_follow_*` reads
a key the projection does not touch, the `save_agent_config_*` file-safety tests still
exercise the old writer (Task 17 retargets them), and every fixture that already writes
both `provider:` and `model:` — `test_agent_provider_settings_parsed`,
`test_scalar_auth_value_does_not_crash`,
`test_backcompat_github_copilot_defaults_to_device_login`,
`test_provider_name_canonicalized_at_load`,
`test_provider_name_case_variant_canonicalized` — passes untouched, which is the point
of Task 2 canonicalising the provider name inside the migration.

Verify the auth round-trip while you are here: `_LEGACY_AUTH_METHODS` maps
`api_key → environment`, `entra → provider-default`, and `_LEGACY_AUTH_BACK` maps them
straight back, so all four of the `test_auth_method_*` cases assert the same string
they do today once Rule L1 is applied.

Run just this file first, because it is the one changing:

```bash
uv run pytest -p no:tach tests/core/test_config.py -q
```

Expected: PASS. A failure here that is *not* explained by one of the three rules above
is a genuine regression in `_derive_legacy_scalars` — fix the derivation, not the test.

- [ ] **Step 7: Run the affected suites**

Run: `uv run pytest -p no:tach tests/core/test_config_profiles.py tests/core/test_config.py tests/test_main_wiring.py -q`
Expected: PASS. If a `tests/core/test_config.py` case asserts a legacy scalar that no longer round-trips and Step 6's three rules do not explain it, update that test to assert the derived value from the profile — do not reintroduce a second parse path.

- [ ] **Step 8: Lint, typecheck, layer check and commit**

```bash
uv run ruff check --fix src/korvid/core/config.py src/korvid/__main__.py tests/core/test_config_profiles.py tests/core/test_config.py
uv run ruff format src/korvid/core/config.py src/korvid/__main__.py tests/core/test_config_profiles.py tests/core/test_config.py
uv run mypy src/korvid/core/config.py src/korvid/__main__.py
uv run tach check
git add src/korvid/core/config.py src/korvid/__main__.py tests/core/test_config_profiles.py tests/core/test_config.py
git commit -m "feat: write agent config as named profiles" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Push the branch, and open the draft pull request only if instructed

**Files:** none (repository/PR operation only).

**Interfaces:**
- Consumes: the three commits from Tasks 1–3 on `agents/provider-neutral-profiles`.
- Produces: the branch on `origin`, and — **only under an explicit human instruction to open a PR** — one draft PR whose number later tasks reuse for CI and the review loop.

> **Authorisation gate.** AGENTS.md: "Do NOT open a pull request without explicit human
> instruction." That applies to draft PRs too. Step 3 and Step 4 run **only** when the
> human driving this plan has said, in the conversation that is executing it, to open the
> PR. Absent that instruction, stop after Step 2, report that the branch is pushed and
> that the PR is waiting on authorisation, and continue with Task 5 — every later task
> works on the branch, not on the PR. Task 20 is likewise skipped if no PR was ever
> opened, and it says so in its own gate.

- [ ] **Step 1: Verify the branch state**

```bash
git status --porcelain
git log --oneline -4
```
Expected: a clean tree and the three profile-domain commits on top of `docs: design provider-neutral model profiles`.

- [ ] **Step 2: Push the branch**

```bash
git push -u origin agents/provider-neutral-profiles
```
Expected: the branch is created on `origin`. This step is unconditional: pushing a branch is not opening a PR.

- [ ] **Step 3: Create the draft PR — only if explicitly instructed**

Skip this step entirely unless the human running the plan has asked for the PR.

```bash
gh pr create --draft --base main --head agents/provider-neutral-profiles \
  --title "feat: provider-neutral model connection profiles" \
  --body "Implements docs/superpowers/specs/2026-09-05-provider-neutral-model-profiles-design.md.

Replaces korvid's single hard-coded provider configuration and CSP-oriented \`:ai\` wizard with named model connection profiles backed by one provider catalog and a Pydantic AI model transport adapter. korvid's agent runtime, approval gate, audit log and outbound policy are unchanged.

Delivered as one PR in ordered commit groups:

1. Named profile config domain (parse, migrate, write).
2. Unified descriptor catalog.
3. Profile-driven setup UI.
4. Pydantic AI model transport.
5. GitHub Copilot extension.
6. Legacy deletion and documentation.

Draft until the final cleanup, the full repository gate and the review round are complete."
```
Expected: `gh` prints the new PR URL. Record the number as `$PR` for later tasks.

- [ ] **Step 4: Confirm CI started — only if Step 3 ran**

```bash
gh pr view --json number,isDraft,statusCheckRollup
```
Expected: `"isDraft": true` and a populated `statusCheckRollup`. Do **not** mark ready, and never enable auto-merge.

---

## Commit group 2 — Unified descriptor catalog (Tasks 5–7)

### Task 5: Public agent descriptor and catalog contracts

**Files:**
- Create: `src/korvid/agent/model_profiles.py`
- Create: `tests/agent/test_model_profiles.py`
- Modify: `src/korvid/agent/setup.py` (import `DeviceLoginPrompt` from the new module)

**Interfaces:**
- Consumes: `AgentAuthConfig`, `AgentProfileConfig`, `AgentProfilesConfig` from `korvid.core.config` (the agent layer may import core).
- Produces (all importable from `korvid.agent.model_profiles`):
  - `AUTH_METHOD_IDS: frozenset[str]`
  - `class SetupFieldKind(Enum)`: `TEXT`, `SECRET_REF`, `BOOLEAN`, `INTEGER`, `CHOICE`
  - `SetupField(id: str, label: str, kind: SetupFieldKind, required: bool = False, choices: tuple[str, ...] = (), default: str | None = None, placeholder: str = "")`
  - `class EndpointRequirement(Enum)`: `REQUIRED`, `OPTIONAL`, `UNSUPPORTED`
  - `AuthMethodDescriptor(id: str, display_name: str, fields: tuple[SetupField, ...] = ())`
  - `ModelAdapterDescriptor(id, display_name, auth_methods, endpoint, supports_model_discovery, option_fields=(), available=True, install_hint=None)`
  - `DeviceLoginPrompt(user_code: str, verification_uri: str)`
  - `class ModelAdapterCatalog(ABC)`: `descriptors()`, `descriptor(adapter_id)`, `list_models(profile)`, `test(profile)`, `begin_auth(profile)`, `finish_auth(profile)`
  - `adapter_id(model_reference: str) -> str`, `model_tag(model_reference: str) -> str`, `with_model_tag(model_reference: str, tag: str) -> str`
  - Re-exports `AgentAuthConfig`, `AgentProfileConfig`, `AgentProfilesConfig` so `korvid.providers` (which may import `korvid.agent` but not `korvid.core`) shares one vocabulary without widening the layer graph.

- [ ] **Step 1: Write the failing contract tests**

Create `tests/agent/test_model_profiles.py`:

```python
"""Public model-adapter descriptor contracts consumed by the setup UI."""

from __future__ import annotations

import pytest

from korvid.agent.model_profiles import (
    AUTH_METHOD_IDS,
    AgentProfileConfig,
    AuthMethodDescriptor,
    DeviceLoginPrompt,
    EndpointRequirement,
    ModelAdapterCatalog,
    ModelAdapterDescriptor,
    SetupField,
    SetupFieldKind,
    adapter_id,
    model_tag,
    with_model_tag,
)


class _StubCatalog(ModelAdapterCatalog):
    def __init__(self, descriptors: tuple[ModelAdapterDescriptor, ...]) -> None:
        self._descriptors = descriptors

    def descriptors(self) -> tuple[ModelAdapterDescriptor, ...]:
        return self._descriptors

    async def list_models(self, profile: AgentProfileConfig) -> list[str]:
        return []

    async def test(self, profile: AgentProfileConfig) -> str:
        return "ok"

    async def begin_auth(self, profile: AgentProfileConfig) -> DeviceLoginPrompt | None:
        return None

    async def finish_auth(self, profile: AgentProfileConfig) -> None:
        return None


_OPENAI = ModelAdapterDescriptor(
    id="openai",
    display_name="OpenAI-compatible endpoint",
    auth_methods=(
        AuthMethodDescriptor(
            id="environment",
            display_name="Environment variable",
            fields=(
                SetupField(
                    id="key",
                    label="Environment variable holding the API key",
                    kind=SetupFieldKind.SECRET_REF,
                    required=True,
                ),
            ),
        ),
    ),
    endpoint=EndpointRequirement.OPTIONAL,
    supports_model_discovery=True,
)


def test_the_five_common_auth_method_ids_are_published() -> None:
    assert frozenset(
        {"none", "environment", "keyring", "provider-default", "device-login"}
    ) == AUTH_METHOD_IDS


def test_descriptor_lookup_is_derived_from_descriptors() -> None:
    catalog = _StubCatalog((_OPENAI,))
    assert catalog.descriptor("openai") is _OPENAI
    assert catalog.descriptor("nope") is None


def test_descriptors_are_immutable_value_objects() -> None:
    with pytest.raises(AttributeError, match="cannot assign to field"):
        _OPENAI.display_name = "changed"  # type: ignore[misc]  # proving frozen


def test_setup_field_choices_are_a_tuple() -> None:
    field = SetupField(
        id="api_version",
        label="API version",
        kind=SetupFieldKind.CHOICE,
        choices=("2025-04-01-preview",),
    )
    assert field.choices == ("2025-04-01-preview",)


@pytest.mark.parametrize(
    ("reference", "expected_adapter", "expected_tag"),
    [
        ("openai:gpt-4o", "openai", "gpt-4o"),
        ("ollama:qwen3:8b", "ollama", "qwen3:8b"),
        ("github-copilot:gpt-4o", "github-copilot", "gpt-4o"),
        ("gpt-4o", "", "gpt-4o"),
    ],
)
def test_model_reference_helpers(reference: str, expected_adapter: str, expected_tag: str) -> None:
    assert adapter_id(reference) == expected_adapter
    assert model_tag(reference) == expected_tag


def test_with_model_tag_keeps_the_adapter_prefix() -> None:
    assert with_model_tag("openai:gpt-4o", "gpt-4o-mini") == "openai:gpt-4o-mini"
    assert with_model_tag("openai:gpt-4o", "ollama:llama3") == "ollama:llama3"
    assert with_model_tag("gpt-4o", "gpt-4o-mini") == "gpt-4o-mini"


def test_unavailable_descriptor_carries_an_install_hint() -> None:
    descriptor = ModelAdapterDescriptor(
        id="bedrock",
        display_name="Amazon Bedrock",
        auth_methods=(AuthMethodDescriptor(id="provider-default", display_name="SDK default"),),
        endpoint=EndpointRequirement.UNSUPPORTED,
        supports_model_discovery=False,
        available=False,
        install_hint="install korvid[provider-bedrock]",
    )
    assert descriptor.available is False
    assert descriptor.install_hint == "install korvid[provider-bedrock]"


def test_option_fields_default_to_empty_and_round_trip() -> None:
    """Task 8's option stage renders exactly these; an empty tuple skips the stage."""
    assert _OPENAI.option_fields == ()
    tuned = ModelAdapterDescriptor(
        id="ollama",
        display_name="Ollama",
        auth_methods=(AuthMethodDescriptor(id="none", display_name="No authentication"),),
        endpoint=EndpointRequirement.REQUIRED,
        supports_model_discovery=True,
        option_fields=(
            SetupField(id="num_ctx", label="Context window", kind=SetupFieldKind.INTEGER),
        ),
    )
    assert tuple(field.id for field in tuned.option_fields) == ("num_ctx",)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/agent/test_model_profiles.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'korvid.agent.model_profiles'`.

- [ ] **Step 3: Write the module**

Create `src/korvid/agent/model_profiles.py`:

```python
"""Provider-neutral model adapter contracts.

The setup UI consumes only this module: it never imports `korvid.providers`
and never learns a cloud vendor's name. A descriptor is *data* — a bounded,
declarative field schema — so a third-party adapter can be configured
without mounting widgets or executing code during screen composition.

The three configuration dataclasses are re-exported here so
`korvid.providers` (which may import `korvid.agent`, not `korvid.core`)
shares exactly one vocabulary with core and the UI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Final

from korvid.core.config import AgentAuthConfig, AgentProfileConfig, AgentProfilesConfig

__all__ = [
    "AUTH_METHOD_IDS",
    "AgentAuthConfig",
    "AgentProfileConfig",
    "AgentProfilesConfig",
    "AuthMethodDescriptor",
    "DeviceLoginPrompt",
    "EndpointRequirement",
    "ModelAdapterCatalog",
    "ModelAdapterDescriptor",
    "SetupField",
    "SetupFieldKind",
    "adapter_id",
    "model_tag",
    "with_model_tag",
]

#: The common auth methods korvid understands. An adapter descriptor
#: declares which of these it supports and which fields each one needs;
#: core stores the choice without interpreting it.
AUTH_METHOD_IDS: Final[frozenset[str]] = frozenset(
    {"none", "environment", "keyring", "provider-default", "device-login"}
)


class SetupFieldKind(Enum):
    """The bounded field kinds a descriptor may ask the wizard to render."""

    TEXT = "text"
    #: The *name* of an environment variable or keyring entry — never a value.
    SECRET_REF = "secret-ref"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    CHOICE = "choice"


@dataclass(frozen=True, slots=True)
class SetupField:
    """One declarative input the wizard renders for an adapter."""

    id: str
    label: str
    kind: SetupFieldKind
    required: bool = False
    choices: tuple[str, ...] = ()
    default: str | None = None
    placeholder: str = ""


class EndpointRequirement(Enum):
    """Whether a profile for this adapter may or must carry an endpoint."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class AuthMethodDescriptor:
    """One auth method an adapter supports, plus the fields it needs."""

    id: str
    display_name: str
    fields: tuple[SetupField, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelAdapterDescriptor:
    """Everything the UI needs to configure one installed model adapter."""

    id: str
    display_name: str
    auth_methods: tuple[AuthMethodDescriptor, ...]
    endpoint: EndpointRequirement
    supports_model_discovery: bool
    #: Adapter-specific tuning rendered by the wizard's option stage and
    #: stored verbatim under `profiles.<name>.options`. An empty tuple
    #: means the stage is skipped for this adapter.
    option_fields: tuple[SetupField, ...] = ()
    #: False when the adapter's optional extra is not installed. The UI
    #: shows `install_hint` instead of offering the adapter; it never
    #: silently substitutes another one.
    available: bool = True
    install_hint: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceLoginPrompt:
    """A device-login code and the URL the operator must visit."""

    user_code: str
    verification_uri: str


def adapter_id(model_reference: str) -> str:
    """The adapter prefix of a `provider:model` reference (`""` when absent)."""
    prefix, separator, _ = model_reference.partition(":")
    return prefix if separator else ""


def model_tag(model_reference: str) -> str:
    """The model identifier a `provider:model` reference passes to its adapter."""
    prefix, separator, suffix = model_reference.partition(":")
    return suffix if separator else prefix


def with_model_tag(model_reference: str, tag: str) -> str:
    """Replace the model tag, keeping the adapter prefix.

    A *tag* that already carries its own prefix replaces the whole
    reference, so a `:model <adapter>:<tag>` argument moves adapters
    deliberately instead of nesting one prefix inside another.
    """
    if adapter_id(tag):
        return tag
    prefix = adapter_id(model_reference)
    return f"{prefix}:{tag}" if prefix else tag


class ModelAdapterCatalog(ABC):
    """The single source of truth for installed model adapters.

    Implemented in `korvid.providers.adapter_catalog` and injected at the
    composition root. Nothing here exposes a provider SDK type.
    """

    @abstractmethod
    def descriptors(self) -> tuple[ModelAdapterDescriptor, ...]:
        """Every adapter that can be offered, without loading plugin code."""

    def descriptor(self, adapter_id_value: str) -> ModelAdapterDescriptor | None:
        """The descriptor for one adapter id, or None when unknown.

        The default resolves against `descriptors()`; an implementation
        that must load a selected plugin to answer fully overrides it.
        """
        for descriptor in self.descriptors():
            if descriptor.id == adapter_id_value:
                return descriptor
        return None

    @abstractmethod
    async def list_models(self, profile: AgentProfileConfig) -> list[str]:
        """Models available for *profile*; `[]` when unknown (typed entry follows)."""

    @abstractmethod
    async def test(self, profile: AgentProfileConfig) -> str:
        """Run one bounded live probe and return the model's reply text."""

    @abstractmethod
    async def begin_auth(self, profile: AgentProfileConfig) -> DeviceLoginPrompt | None:
        """Start an interactive login, or return None when none is needed."""

    @abstractmethod
    async def finish_auth(self, profile: AgentProfileConfig) -> None:
        """Complete a login started by `begin_auth`."""
```

- [ ] **Step 4: Point the legacy setup contract at the shared prompt type**

In `src/korvid/agent/setup.py`, delete the local `DeviceLoginPrompt` dataclass and import it instead, so the compatibility cycle has exactly one prompt type:

```python
from korvid.agent.model_profiles import DeviceLoginPrompt

__all__ = ["MODEL_TIERS", "AgentConfigurator", "AgentSettings", "DeviceLoginPrompt"]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/agent/test_model_profiles.py tests/agent/test_setup.py tests/ui/test_agent_setup_screen.py -q`
Expected: PASS.

- [ ] **Step 6: Lint, typecheck, layer check and commit**

```bash
uv run ruff check --fix src/korvid/agent/model_profiles.py src/korvid/agent/setup.py tests/agent/test_model_profiles.py
uv run ruff format src/korvid/agent/model_profiles.py src/korvid/agent/setup.py tests/agent/test_model_profiles.py
uv run mypy src/korvid/agent/model_profiles.py src/korvid/agent/setup.py
uv run tach check
git add src/korvid/agent/model_profiles.py src/korvid/agent/setup.py tests/agent/test_model_profiles.py
git commit -m "feat: publish provider-neutral model adapter contracts" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: Provider catalog over built-ins and plugins

**Files:**
- Create: `src/korvid/providers/adapter_catalog.py`
- Modify: `src/korvid/providers/plugin_registry.py`
- Create: `tests/providers/test_adapter_catalog.py`

**Interfaces:**
- Consumes: `ModelAdapterCatalog`, `ModelAdapterDescriptor`, `AuthMethodDescriptor`, `SetupField`, `SetupFieldKind`, `EndpointRequirement`, `DeviceLoginPrompt`, `AgentProfileConfig`, `adapter_id`, `model_tag` from `korvid.agent.model_profiles`; `ProviderPluginRegistry`, `ProviderPluginMetadata`, `normalize_provider_name`; `TokenStore`; `GitHubDeviceFlow`; `OutboundPolicy`.
- Produces:
  - `ProviderModelCatalog(ModelAdapterCatalog)` with `__init__(self, *, token_store: TokenStore, plugin_registry: ProviderPluginRegistry | None = None, ca_bundle: str | None = None, http_client_factory: Callable[[], httpx.AsyncClient] | None = None, flow_factory: Callable[[], GitHubDeviceFlow] = GitHubDeviceFlow) -> None`
  - `BUILTIN_ADAPTER_DESCRIPTORS: tuple[ModelAdapterDescriptor, ...]`
  - `ProviderPluginRegistry.entry_point_names(self) -> tuple[str, ...]`
  - `ProviderPluginRegistry.metadata(self, name: str) -> ProviderPluginMetadata` (loads exactly the named plugin)

- [ ] **Step 1: Write the failing catalog tests**

Create `tests/providers/test_adapter_catalog.py`:

```python
"""The single catalog of installed model adapters."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterator
from typing import Any

import pytest

from korvid.agent.model_profiles import (
    AgentAuthConfig,
    AgentProfileConfig,
    EndpointRequirement,
)
from korvid.providers.adapter_catalog import ProviderModelCatalog
from korvid.providers.plugin_registry import ProviderPluginRegistry
from korvid.providers.token_store import TokenStore


class _MemoryTokenStore(TokenStore):
    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def save(self, key: str, value: str) -> None:
        self._tokens[key] = value

    def load(self, key: str) -> str | None:
        return self._tokens.get(key)

    def delete(self, key: str) -> None:
        self._tokens.pop(key, None)


@pytest.fixture
def catalog() -> ProviderModelCatalog:
    return ProviderModelCatalog(token_store=_MemoryTokenStore())


def test_builtin_adapters_are_offered(catalog: ProviderModelCatalog) -> None:
    ids = {descriptor.id for descriptor in catalog.descriptors()}
    assert {
        "openai",
        "azure",
        "ollama",
        "github-copilot",
        "anthropic",
        "google",
        "bedrock",
    } <= ids


def test_azure_adapter_requires_an_endpoint_and_keeps_its_own_auth(
    catalog: ProviderModelCatalog,
) -> None:
    """Azure is a distinct adapter, not an alias of the OpenAI one.

    Its key travels in the `api-key` header and `provider-default` means
    an Entra token — neither of which the `openai` adapter can express.
    """
    descriptor = catalog.descriptor("azure")
    assert descriptor is not None
    assert descriptor.endpoint is EndpointRequirement.REQUIRED
    assert {method.id for method in descriptor.auth_methods} == {
        "environment",
        "keyring",
        "provider-default",
    }
    assert [field.id for field in descriptor.option_fields] == ["api_version", "azure_deployment"]


def test_the_ollama_adapter_keeps_every_legacy_tuning_knob(
    catalog: ProviderModelCatalog,
) -> None:
    """`agent.ollama` exposed six knobs; no operator loses a setting.

    `native_api` is the seventh and is new: it is how the retained native
    Ollama client is selected through the profile contract once Task 17
    removes the provider registry's hardcoded branch.
    """
    descriptor = catalog.descriptor("ollama")
    assert descriptor is not None
    assert {field.id for field in descriptor.option_fields} == {
        "num_ctx",
        "num_predict",
        "temperature",
        "seed",
        "think",
        "keep_alive",
        "native_api",
    }


def test_the_bedrock_adapter_asks_for_a_region_instead_of_an_endpoint(
    catalog: ProviderModelCatalog,
) -> None:
    """For Bedrock the region *is* the endpoint.

    `BedrockProvider(region_name="us-east-1")` resolves to
    `https://bedrock-runtime.us-east-1.amazonaws.com`, and constructing
    the model without one raises `UserError` from the SDK.
    """
    descriptor = catalog.descriptor("bedrock")
    assert descriptor is not None
    assert descriptor.endpoint is EndpointRequirement.UNSUPPORTED
    assert [field.id for field in descriptor.option_fields] == ["region_name"]
    assert descriptor.option_fields[0].required is True


def test_openai_adapter_declares_an_optional_endpoint_and_discovery(
    catalog: ProviderModelCatalog,
) -> None:
    descriptor = catalog.descriptor("openai")
    assert descriptor is not None
    assert descriptor.endpoint is EndpointRequirement.OPTIONAL
    assert descriptor.supports_model_discovery is True
    assert {method.id for method in descriptor.auth_methods} == {
        "none",
        "environment",
        "keyring",
        "provider-default",
    }


def test_copilot_adapter_declares_device_login(catalog: ProviderModelCatalog) -> None:
    descriptor = catalog.descriptor("github-copilot")
    assert descriptor is not None
    assert [method.id for method in descriptor.auth_methods] == ["device-login"]
    assert descriptor.endpoint is EndpointRequirement.UNSUPPORTED


def test_plugin_entry_points_appear_without_being_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[str] = []

    class _Plugin:
        pass

    ep = importlib.metadata.EntryPoint(
        name="company-llm", value="company_llm:Plugin", group="korvid.provider"
    )

    def _fake_discover() -> list[tuple[importlib.metadata.EntryPoint, str]]:
        return [(ep, "company-llm")]

    def _fake_load(entry_point: importlib.metadata.EntryPoint) -> object:
        loaded.append(entry_point.name)
        return _Plugin

    monkeypatch.setattr("korvid.providers.plugin_registry._discover_entry_points", _fake_discover)
    monkeypatch.setattr("korvid.providers.plugin_registry._load_entry_point", _fake_load)

    catalog = ProviderModelCatalog(
        token_store=_MemoryTokenStore(), plugin_registry=ProviderPluginRegistry()
    )
    ids = [descriptor.id for descriptor in catalog.descriptors()]
    assert "company-llm" in ids
    assert loaded == []


def test_selecting_a_plugin_descriptor_loads_only_that_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from korvid.agent.provider_plugin import ProviderPlugin, ProviderPluginMetadata

    loaded: list[str] = []

    class _Plugin(ProviderPlugin):
        @property
        def metadata(self) -> ProviderPluginMetadata:
            return ProviderPluginMetadata(
                api_version=2,
                name="company-llm",
                display_name="Company LLM",
                auth_methods=("api_key",),
            )

        def create(self, config: Any, credentials: Any) -> Any:
            raise AssertionError("not exercised")

    entry_points = [
        importlib.metadata.EntryPoint(
            name=name, value="company_llm:Plugin", group="korvid.provider"
        )
        for name in ("company-llm", "other-llm")
    ]

    monkeypatch.setattr(
        "korvid.providers.plugin_registry._discover_entry_points",
        lambda: [(ep, ep.name) for ep in entry_points],
    )

    def _fake_load(entry_point: importlib.metadata.EntryPoint) -> object:
        loaded.append(entry_point.name)
        return _Plugin

    monkeypatch.setattr("korvid.providers.plugin_registry._load_entry_point", _fake_load)

    catalog = ProviderModelCatalog(
        token_store=_MemoryTokenStore(), plugin_registry=ProviderPluginRegistry()
    )
    descriptor = catalog.descriptor("company-llm")
    assert descriptor is not None
    assert descriptor.display_name == "Company LLM"
    assert loaded == ["company-llm"]


def test_a_broken_plugin_never_removes_the_built_in_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode() -> Iterator[tuple[importlib.metadata.EntryPoint, str]]:
        raise RuntimeError("distribution metadata is unreadable")

    monkeypatch.setattr("korvid.providers.plugin_registry._discover_entry_points", _explode)
    catalog = ProviderModelCatalog(
        token_store=_MemoryTokenStore(), plugin_registry=ProviderPluginRegistry()
    )
    assert {descriptor.id for descriptor in catalog.descriptors()} >= {"openai", "ollama"}


def test_unavailable_adapter_reports_an_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korvid.providers.adapter_catalog._adapter_available",
        lambda adapter: adapter != "anthropic",
    )
    catalog = ProviderModelCatalog(token_store=_MemoryTokenStore())
    descriptor = catalog.descriptor("anthropic")
    assert descriptor is not None
    assert descriptor.available is False
    assert descriptor.install_hint is not None
    assert "provider-anthropic" in descriptor.install_hint


@pytest.mark.asyncio
async def test_listing_models_for_an_adapter_without_discovery_returns_empty() -> None:
    catalog = ProviderModelCatalog(token_store=_MemoryTokenStore())
    profile = AgentProfileConfig(
        model="bedrock:anthropic.claude-3-5-sonnet-20241022-v2:0",
        auth=AgentAuthConfig(method="provider-default"),
    )
    assert await catalog.list_models(profile) == []


@pytest.mark.asyncio
async def test_begin_auth_is_a_no_op_for_non_interactive_adapters() -> None:
    catalog = ProviderModelCatalog(token_store=_MemoryTokenStore())
    profile = AgentProfileConfig(model="openai:gpt-4o", auth=AgentAuthConfig(method="none"))
    assert await catalog.begin_auth(profile) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/providers/test_adapter_catalog.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'korvid.providers.adapter_catalog'`.

- [ ] **Step 3: Add metadata-only plugin enumeration**

In `src/korvid/providers/plugin_registry.py`, add to `ProviderPluginRegistry`:

```python
    def entry_point_names(self) -> tuple[str, ...]:
        """Normalized names of every discoverable provider plugin.

        Enumeration reads distribution metadata only — no entry point is
        loaded, so an unselected plugin never executes code just because
        the wizard was opened. Discovery failures degrade to an empty
        tuple: a broken third-party distribution can never remove
        korvid's built-in adapters from the catalog.
        """
        try:
            discovered = _discover_entry_points()
        except ProviderPluginError:
            logger.warning("provider plugin discovery failed; only built-in adapters are offered")
            return ()
        except Exception:
            logger.warning("provider plugin discovery raised; only built-in adapters are offered")
            return ()
        names = {
            normalize_provider_name(ep.name)
            for ep, _ in discovered
            if normalize_provider_name(ep.name) not in RESERVED_PROVIDER_NAMES
        }
        return tuple(sorted(names))

    def metadata(self, name: str) -> ProviderPluginMetadata:
        """Validated metadata for one plugin, loading only that entry point.

        Raises:
            ProviderPluginError: If discovery, loading, or validation fails.
        """
        normalized = normalize_provider_name(name)
        if normalized not in self._cache:
            # `load_selected` validates and caches `(plugin, metadata)`.
            self.load_selected(name)
        return self._cache[normalized][1]
```

- [ ] **Step 4: Write the shared adapter-extra table**

The wizard (which offers an adapter) and the factory (which builds it)
must agree about which extras are installed and what to tell the operator
when one is missing. Two tables would drift, so there is exactly one.

Create `src/korvid/providers/adapter_extras.py`:

```python
"""Which optional extra backs each built-in model adapter.

Imported by both `adapter_catalog` (to grey out an adapter and show an
install hint) and `pydantic_factory` (to refuse construction with the
same hint). Stdlib-only and side-effect-free: importing this module — or
asking it whether an adapter is available — never imports a vendor SDK.

This is also the shared-constant module for the adapter vocabulary
(`BUILTIN_ADAPTERS`, `KEYLESS_API_KEY_SENTINEL`). It sits at the bottom
of the import graph: it imports nothing from `korvid`, so `core`, `agent`
and `providers` can all depend on it without a cycle.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class AdapterExtra:
    """The distribution that must be installed for one adapter, and its extra."""

    #: The *distribution* name, not the import name. Availability is
    #: decided from installed-distribution metadata rather than
    #: `importlib.util.find_spec`, because `find_spec("google.genai")`
    #: imports the `google` namespace package as a side effect — which
    #: would execute third-party `google/__init__.py` code merely because
    #: the operator opened the wizard.
    distribution: str
    extra: str


#: Adapter id → the extra that backs it. The Azure, Ollama and GitHub
#: Copilot adapters are all built on Pydantic AI's OpenAI-compatible
#: stack, so they need the same `provider-openai` extra as `openai`.
#: `MappingProxyType` makes the table read-only at runtime: a test that
#: needs a different table passes one in through the `extras` parameter
#: rather than mutating shared state that leaks across test order.
ADAPTER_EXTRAS: Final[Mapping[str, AdapterExtra]] = MappingProxyType(
    {
        "openai": AdapterExtra("openai", "provider-openai"),
        "azure": AdapterExtra("openai", "provider-openai"),
        "ollama": AdapterExtra("openai", "provider-openai"),
        "github-copilot": AdapterExtra("openai", "provider-openai"),
        "anthropic": AdapterExtra("anthropic", "provider-anthropic"),
        "google": AdapterExtra("google-genai", "provider-google"),
        "bedrock": AdapterExtra("boto3", "provider-bedrock"),
    }
)

#: Every adapter korvid ships. Third-party plugin ids are *not* here.
#: Defined next to the extras table so the two can never disagree about
#: what "built-in" means, and importable from anywhere without a cycle.
BUILTIN_ADAPTERS: Final[frozenset[str]] = frozenset(ADAPTER_EXTRAS)

#: The literal placeholder korvid sends as the API key when an operator
#: configures a *custom* OpenAI-compatible endpoint with `auth: none`.
#: Pydantic AI's own `OllamaProvider` uses the same idiom for the same
#: reason: the OpenAI client refuses to construct without a key, and a
#: `None` key makes it read `OPENAI_API_KEY` from the ambient
#: environment — silently authenticating to a self-hosted endpoint with
#: a real OpenAI credential. This sentinel is only ever used when an
#: endpoint was supplied; `api.openai.com` never receives it.
KEYLESS_API_KEY_SENTINEL: Final[str] = "api-key-not-set"


def adapter_available(adapter: str, *, extras: Mapping[str, AdapterExtra] | None = None) -> bool:
    """Whether the distribution backing *adapter* is installed.

    Args:
        adapter: The adapter id to test.
        extras: An alternative table, for tests. Defaults to
            `ADAPTER_EXTRAS`.

    Returns:
        True when the adapter needs no extra (a third-party plugin, whose
        own registry entry decides whether it loads) or its distribution
        is installed.
    """
    entry = (extras if extras is not None else ADAPTER_EXTRAS).get(adapter)
    if entry is None:
        return True
    try:
        importlib.metadata.version(entry.distribution)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def install_hint(adapter: str, *, extras: Mapping[str, AdapterExtra] | None = None) -> str | None:
    """The exact install command for *adapter*, or None if it needs none."""
    entry = (extras if extras is not None else ADAPTER_EXTRAS).get(adapter)
    if entry is None:
        return None
    return f"install korvid[{entry.extra}] to use the {adapter} model adapter"
```

Create `tests/providers/test_adapter_extras.py`:

```python
"""One table decides adapter availability and its install hint."""

from __future__ import annotations

import sys

import pytest

from korvid.providers.adapter_extras import (
    ADAPTER_EXTRAS,
    BUILTIN_ADAPTERS,
    KEYLESS_API_KEY_SENTINEL,
    AdapterExtra,
    adapter_available,
    install_hint,
)


@pytest.mark.parametrize(
    "adapter", ["openai", "azure", "ollama", "github-copilot", "anthropic", "google", "bedrock"]
)
def test_every_builtin_adapter_has_an_extra(adapter: str) -> None:
    assert adapter in ADAPTER_EXTRAS
    assert install_hint(adapter) == (
        f"install korvid[{ADAPTER_EXTRAS[adapter].extra}] to use the {adapter} model adapter"
    )


def test_builtin_adapters_are_exactly_the_table_keys() -> None:
    assert frozenset(ADAPTER_EXTRAS) == BUILTIN_ADAPTERS


def test_azure_ollama_and_copilot_share_the_openai_extra() -> None:
    """They are all built on Pydantic AI's OpenAI-compatible stack."""
    assert ADAPTER_EXTRAS["azure"] == ADAPTER_EXTRAS["openai"]
    assert ADAPTER_EXTRAS["ollama"] == ADAPTER_EXTRAS["openai"]
    assert ADAPTER_EXTRAS["github-copilot"] == ADAPTER_EXTRAS["openai"]


def test_an_unknown_adapter_is_available_with_no_hint() -> None:
    assert adapter_available("company-llm") is True
    assert install_hint("company-llm") is None


def test_a_missing_distribution_makes_its_adapter_unavailable() -> None:
    """The seam is a parameter, so no test mutates the shared table."""
    absent = {"openai": AdapterExtra("korvid-no-such-distribution", "provider-openai")}
    assert adapter_available("openai", extras=absent) is False
    assert install_hint("openai", extras=absent) == (
        "install korvid[provider-openai] to use the openai model adapter"
    )


def test_the_shared_table_cannot_be_mutated() -> None:
    with pytest.raises(TypeError, match="does not support item assignment"):
        ADAPTER_EXTRAS["openai"] = AdapterExtra("x", "y")  # type: ignore[index]  # proving read-only


def test_checking_availability_imports_no_vendor_sdk() -> None:
    """`find_spec("google.genai")` would import the `google` namespace package."""
    before = {name for name in sys.modules if name.split(".")[0] == "google"}
    assert adapter_available("google") in {True, False}
    after = {name for name in sys.modules if name.split(".")[0] == "google"}
    assert after == before


def test_the_keyless_sentinel_is_a_non_empty_placeholder() -> None:
    assert KEYLESS_API_KEY_SENTINEL
    assert "sk-" not in KEYLESS_API_KEY_SENTINEL
```

Run `uv run pytest -p no:tach tests/providers/test_adapter_extras.py -q` **before** creating the
module (expected: FAIL — `ModuleNotFoundError: No module named
'korvid.providers.adapter_extras'`) and again after (expected: PASS).

- [ ] **Step 5: Write the catalog**

Create `src/korvid/providers/adapter_catalog.py`:

```python
"""The single catalog of installed model adapters.

Built-in Pydantic AI adapters and validated `korvid.provider` entry
points are published through one contract, so the setup UI treats a
third-party adapter exactly like a built-in one. Enumeration is
metadata-only: selecting an adapter is what loads its plugin.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

import httpx

from korvid.agent.credentials import CredentialSource
from korvid.agent.model_profiles import (
    AgentProfileConfig,
    AuthMethodDescriptor,
    DeviceLoginPrompt,
    EndpointRequirement,
    ModelAdapterCatalog,
    ModelAdapterDescriptor,
    SetupField,
    SetupFieldKind,
    adapter_id,
)
from korvid.agent.outbound import OutboundPolicy, provider_prepared_messages
from korvid.providers.adapter_extras import adapter_available, install_hint
from korvid.providers.github_copilot import (
    COPILOT_CHAT_BASE_URL,
    CopilotCredentialSource,
    DeviceCodePrompt,
    GitHubDeviceFlow,
)
from korvid.providers.net import make_client
from korvid.providers.ollama import normalize_base_url
from korvid.providers.plugin_registry import ProviderPluginError, ProviderPluginRegistry
from korvid.providers.token_store import TokenStore

logger = logging.getLogger(__name__)

_PROBE_MESSAGE: Final[dict[str, str]] = {
    "role": "user",
    "content": "Reply with the single word: ok",
}
_PROBE_MAX_REQUEST_CHARS: Final[int] = 4_096

_ENV_FIELD = SetupField(
    id="key",
    label="Environment variable holding the API key",
    kind=SetupFieldKind.SECRET_REF,
    required=True,
    placeholder="OPENAI_API_KEY",
)
_KEYRING_FIELD = SetupField(
    id="key",
    label="Keyring entry name",
    kind=SetupFieldKind.SECRET_REF,
    required=True,
    placeholder="korvid-openai",
)

_NONE_AUTH = AuthMethodDescriptor(id="none", display_name="No authentication")
_ENV_AUTH = AuthMethodDescriptor(
    id="environment", display_name="Environment variable", fields=(_ENV_FIELD,)
)
_KEYRING_AUTH = AuthMethodDescriptor(
    id="keyring", display_name="System keyring", fields=(_KEYRING_FIELD,)
)
_SDK_AUTH = AuthMethodDescriptor(
    id="provider-default", display_name="Provider SDK default credentials"
)
_DEVICE_AUTH = AuthMethodDescriptor(id="device-login", display_name="Sign in with GitHub")

#: Every built-in adapter, described declaratively. korvid asserts no
#: capability it cannot observe: `supports_model_discovery` is True only
#: where the adapter exposes a real listing endpoint.
BUILTIN_ADAPTER_DESCRIPTORS: Final[tuple[ModelAdapterDescriptor, ...]] = (
    ModelAdapterDescriptor(
        id="openai",
        display_name="OpenAI-compatible endpoint",
        auth_methods=(_NONE_AUTH, _ENV_AUTH, _KEYRING_AUTH, _SDK_AUTH),
        endpoint=EndpointRequirement.OPTIONAL,
        supports_model_discovery=True,
    ),
    ModelAdapterDescriptor(
        id="azure",
        display_name="Azure OpenAI",
        # No `none`: an Azure OpenAI deployment always authenticates.
        # `provider-default` is Entra ID (DefaultAzureCredential), and an
        # API key travels in the `api-key` header, never as a bearer
        # token — which is precisely why this is its own adapter and not
        # an alias of `openai`.
        auth_methods=(_ENV_AUTH, _KEYRING_AUTH, _SDK_AUTH),
        endpoint=EndpointRequirement.REQUIRED,
        # The deployments listing is a management-plane call that needs a
        # different credential scope, so korvid asks for the deployment
        # name instead of claiming a discovery it cannot perform.
        supports_model_discovery=False,
        option_fields=(
            SetupField(
                id="api_version",
                label="Azure OpenAI API version",
                kind=SetupFieldKind.TEXT,
                placeholder="2024-10-21",
            ),
            SetupField(
                id="azure_deployment",
                label="Deployment name (defaults to the model tag)",
                kind=SetupFieldKind.TEXT,
                placeholder="gpt-4o",
            ),
        ),
    ),
    ModelAdapterDescriptor(
        id="ollama",
        display_name="Ollama (local)",
        auth_methods=(_NONE_AUTH, _ENV_AUTH, _KEYRING_AUTH),
        endpoint=EndpointRequirement.REQUIRED,
        supports_model_discovery=True,
        # Exactly the tuning knobs `agent.ollama` exposed before this
        # migration, so no operator loses a setting, plus `native_api`.
        # Task 14 maps `temperature`, `seed` and `num_predict` onto
        # `ModelSettings` and sends `num_ctx`, `think` and `keep_alive` in
        # `extra_body`; Task 17 routes `native_api` to the retained
        # native client.
        option_fields=(
            SetupField(id="num_ctx", label="Context window (num_ctx)", kind=SetupFieldKind.INTEGER),
            SetupField(
                id="num_predict",
                label="Maximum tokens to generate (num_predict)",
                kind=SetupFieldKind.INTEGER,
            ),
            SetupField(id="temperature", label="Temperature (0-2)", kind=SetupFieldKind.TEXT),
            SetupField(id="seed", label="Sampling seed", kind=SetupFieldKind.INTEGER),
            SetupField(id="think", label="Enable thinking", kind=SetupFieldKind.BOOLEAN),
            SetupField(
                id="keep_alive",
                label="Keep the model loaded for (e.g. 5m)",
                kind=SetupFieldKind.TEXT,
                placeholder="5m",
            ),
            SetupField(
                id="native_api",
                label="Use Ollama's native /api endpoint instead of /v1",
                kind=SetupFieldKind.BOOLEAN,
            ),
        ),
    ),
    ModelAdapterDescriptor(
        id="anthropic",
        display_name="Anthropic",
        auth_methods=(_ENV_AUTH, _KEYRING_AUTH, _SDK_AUTH),
        endpoint=EndpointRequirement.OPTIONAL,
        supports_model_discovery=False,
    ),
    ModelAdapterDescriptor(
        id="google",
        display_name="Google Gemini",
        auth_methods=(_ENV_AUTH, _KEYRING_AUTH, _SDK_AUTH),
        endpoint=EndpointRequirement.OPTIONAL,
        supports_model_discovery=False,
    ),
    ModelAdapterDescriptor(
        id="bedrock",
        display_name="Amazon Bedrock",
        auth_methods=(_SDK_AUTH,),
        endpoint=EndpointRequirement.UNSUPPORTED,
        # Bedrock has no endpoint field because the region *is* the
        # endpoint: `BedrockProvider(region_name="us-east-1")` resolves to
        # `https://bedrock-runtime.us-east-1.amazonaws.com`. Without a
        # region the SDK raises
        # `UserError: You must provide a `region_name` or a boto3 client
        # for Bedrock Runtime.`, so the wizard asks for it up front.
        option_fields=(
            SetupField(
                id="region_name",
                label="AWS region",
                kind=SetupFieldKind.TEXT,
                required=True,
                placeholder="us-east-1",
            ),
        ),
        supports_model_discovery=False,
    ),
    ModelAdapterDescriptor(
        id="github-copilot",
        display_name="GitHub Copilot",
        auth_methods=(_DEVICE_AUTH,),
        endpoint=EndpointRequirement.UNSUPPORTED,
        supports_model_discovery=True,
    ),
)


def _adapter_available(adapter: str) -> bool:
    """Whether the optional extra backing *adapter* is importable.

    Delegates to the shared table so the wizard's availability answer and
    the factory's refusal can never disagree. Kept as a module-level
    function because tests monkeypatch this name.
    """
    return adapter_available(adapter)


def _install_hint(adapter: str) -> str | None:
    return install_hint(adapter)


def _with_availability(descriptor: ModelAdapterDescriptor) -> ModelAdapterDescriptor:
    if _adapter_available(descriptor.id):
        return descriptor
    return ModelAdapterDescriptor(
        id=descriptor.id,
        display_name=descriptor.display_name,
        auth_methods=descriptor.auth_methods,
        endpoint=descriptor.endpoint,
        supports_model_discovery=descriptor.supports_model_discovery,
        option_fields=descriptor.option_fields,
        available=False,
        install_hint=_install_hint(descriptor.id),
    )


#: Plugin auth ids → the common ids. A plugin still publishes the v2
#: contract's `api_key`/`entra` vocabulary; the catalog is the single
#: place that translation happens.
_PLUGIN_AUTH_METHODS: Final[dict[str, AuthMethodDescriptor]] = {
    "none": _NONE_AUTH,
    "api_key": _ENV_AUTH,
    "entra": _SDK_AUTH,
}


class ProviderModelCatalog(ModelAdapterCatalog):
    """`ModelAdapterCatalog` over built-in adapters and provider plugins."""

    def __init__(
        self,
        *,
        token_store: TokenStore,
        plugin_registry: ProviderPluginRegistry | None = None,
        ca_bundle: str | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        flow_factory: Callable[[], GitHubDeviceFlow] = GitHubDeviceFlow,
    ) -> None:
        self._store = token_store
        self._plugins = plugin_registry
        self._ca_bundle = ca_bundle
        self._http_client_factory = http_client_factory
        self._flow_factory = flow_factory
        self._outbound = OutboundPolicy(max_request_chars=_PROBE_MAX_REQUEST_CHARS)
        self._flow: GitHubDeviceFlow | None = None
        self._prompt: DeviceCodePrompt | None = None

    def descriptors(self) -> tuple[ModelAdapterDescriptor, ...]:
        built_in = tuple(_with_availability(item) for item in BUILTIN_ADAPTER_DESCRIPTORS)
        if self._plugins is None:
            return built_in
        known = {descriptor.id for descriptor in built_in}
        plugins = tuple(
            # Metadata-only placeholder: the display name and auth methods
            # are filled in by `descriptor()` when the operator selects it,
            # which is the first moment loading the plugin is warranted.
            ModelAdapterDescriptor(
                id=name,
                display_name=name,
                auth_methods=(_ENV_AUTH,),
                endpoint=EndpointRequirement.OPTIONAL,
                supports_model_discovery=False,
            )
            for name in self._plugins.entry_point_names()
            if name not in known
        )
        return built_in + plugins

    def descriptor(self, adapter_id_value: str) -> ModelAdapterDescriptor | None:
        for descriptor in BUILTIN_ADAPTER_DESCRIPTORS:
            if descriptor.id == adapter_id_value:
                return _with_availability(descriptor)
        if self._plugins is None:
            return None
        try:
            meta = self._plugins.metadata(adapter_id_value)
        except ProviderPluginError as exc:
            logger.warning("provider plugin %r is unusable: %s", adapter_id_value, exc)
            return None
        auth_methods = tuple(
            _PLUGIN_AUTH_METHODS[method]
            for method in meta.auth_methods
            if method in _PLUGIN_AUTH_METHODS
        ) or (_NONE_AUTH,)
        return ModelAdapterDescriptor(
            id=meta.name,
            display_name=meta.display_name,
            auth_methods=auth_methods,
            endpoint=EndpointRequirement.OPTIONAL,
            supports_model_discovery=False,
        )

    def _endpoint_client(self) -> httpx.AsyncClient:
        """Endpoint calls share the live provider's `network.ca_bundle` trust."""
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client(self._ca_bundle, timeout=15.0)

    def _public_client(self) -> httpx.AsyncClient:
        """GitHub Copilot discovery uses default trust, matching the live adapter."""
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return httpx.AsyncClient(timeout=15.0)

    async def list_models(self, profile: AgentProfileConfig) -> list[str]:
        """Selectable model ids; `[]` on any failure so the wizard falls back to input."""
        adapter = adapter_id(profile.model)
        descriptor = self.descriptor(adapter)
        if descriptor is None or not descriptor.supports_model_discovery:
            return []
        try:
            if adapter == "github-copilot":
                return await self._list_copilot_models()
            if adapter == "ollama":
                return await self._list_ollama_models(profile)
            return await self._list_openai_models(profile)
        except Exception:  # model listing is best-effort — never break the wizard
            logger.debug("model listing failed", exc_info=True)
            return []

    async def _list_copilot_models(self) -> list[str]:
        oauth = self._store.load("github-oauth")
        if not oauth:
            return []
        client = self._public_client()
        creds = CopilotCredentialSource(oauth, client=client)
        try:
            headers = await creds.headers()
            resp = await client.get(f"{COPILOT_CHAT_BASE_URL}/models", headers=headers)
        finally:
            await creds.aclose()
        if resp.status_code != 200:
            return []
        data = resp.json().get("data", [])
        return sorted(
            {
                str(item["id"])
                for item in data
                if isinstance(item, dict)
                and "id" in item
                and item.get("capabilities", {}).get("type") == "chat"
            }
        )

    async def _auth_headers(
        self, profile: AgentProfileConfig
    ) -> tuple[dict[str, str], CredentialSource | None]:
        from korvid.providers.pydantic_factory import resolve_credential

        creds = resolve_credential(profile)
        headers = await creds.headers() if creds is not None else {}
        return headers, creds

    async def _list_openai_models(self, profile: AgentProfileConfig) -> list[str]:
        if not profile.endpoint:
            return []
        headers, creds = await self._auth_headers(profile)
        try:
            async with self._endpoint_client() as client:
                resp = await client.get(f"{profile.endpoint.rstrip('/')}/models", headers=headers)
        finally:
            if creds is not None:
                await creds.aclose()
        if resp.status_code != 200:
            return []
        data = resp.json().get("data", [])
        return sorted({str(item["id"]) for item in data if isinstance(item, dict) and "id" in item})

    async def _list_ollama_models(self, profile: AgentProfileConfig) -> list[str]:
        """Native model listing via /api/tags (the native API has no /models)."""
        if not profile.endpoint:
            return []
        headers, creds = await self._auth_headers(profile)
        try:
            async with self._endpoint_client() as client:
                resp = await client.get(
                    f"{normalize_base_url(profile.endpoint)}/api/tags", headers=headers
                )
        finally:
            if creds is not None:
                await creds.aclose()
        if resp.status_code != 200:
            return []
        models = resp.json().get("models", [])
        return sorted(
            {str(item["name"]) for item in models if isinstance(item, dict) and "name" in item}
        )

    async def test(self, profile: AgentProfileConfig) -> str:
        """One bounded live probe through the same outbound policy as the agent."""
        from korvid.providers.registry import create_profile_provider

        provider = create_profile_provider(
            profile,
            oauth_token=self._store.load("github-oauth"),
            ca_bundle=self._ca_bundle,
            plugin_registry=self._plugins,
        )
        if provider is None:
            raise RuntimeError("configuration incomplete — provider could not be created")
        text = ""
        try:
            prepared = self._outbound.prepare(
                provider.descriptor.model,
                provider_prepared_messages(provider, [_PROBE_MESSAGE]),
                [],
                iteration=1,
            )
            async for event in provider.complete(prepared.messages, prepared.tools):
                if event.get("type") == "text_delta":
                    text += str(event.get("text", ""))
        finally:
            await provider.aclose()
        if not text.strip():
            raise RuntimeError("provider returned no text")
        return text.strip()

    async def begin_auth(self, profile: AgentProfileConfig) -> DeviceLoginPrompt | None:
        if profile.auth.method != "device-login":
            return None
        flow = self._flow_factory()
        try:
            prompt = await flow.start()
        except BaseException:
            # Never leak the flow's HTTP client when the device-code request
            # fails or the worker is cancelled before a prompt is obtained.
            await flow.aclose()
            raise
        self._flow = flow
        self._prompt = prompt
        return DeviceLoginPrompt(prompt.user_code, prompt.verification_uri)

    async def finish_auth(self, profile: AgentProfileConfig) -> None:
        if profile.auth.method != "device-login":
            return
        if self._flow is None or self._prompt is None:
            raise RuntimeError("begin_auth must be called first")
        try:
            token = await self._flow.poll(self._prompt)
        finally:
            await self._flow.aclose()
            self._flow = None
            self._prompt = None
        self._store.save("github-oauth", token)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/providers/test_adapter_extras.py tests/providers/test_adapter_catalog.py tests/providers/test_plugin_registry.py -q`
Expected: PASS. `test()` and `_auth_headers` reference `create_profile_provider`/`resolve_credential` from Task 14; until then those two paths are covered only by the tests added in Task 14, and the imports are function-local so importing the module does not fail.

- [ ] **Step 7: Lint, typecheck, layer check and commit**

```bash
uv run ruff check --fix src/korvid/providers/adapter_extras.py src/korvid/providers/adapter_catalog.py src/korvid/providers/plugin_registry.py tests/providers/test_adapter_extras.py tests/providers/test_adapter_catalog.py
uv run ruff format src/korvid/providers/adapter_extras.py src/korvid/providers/adapter_catalog.py src/korvid/providers/plugin_registry.py tests/providers/test_adapter_extras.py tests/providers/test_adapter_catalog.py
uv run mypy src/korvid/providers/adapter_extras.py src/korvid/providers/adapter_catalog.py src/korvid/providers/plugin_registry.py
uv run tach check
git add src/korvid/providers/adapter_extras.py src/korvid/providers/adapter_catalog.py src/korvid/providers/plugin_registry.py tests/providers/test_adapter_extras.py tests/providers/test_adapter_catalog.py
git commit -m "feat: publish one catalog of built-in and plugin model adapters" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: Inject the catalog from the composition root

**Files:**
- Modify: `src/korvid/__main__.py` (agent wiring, `_build_agent_wiring`)
- Modify: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: `ProviderModelCatalog` (Task 6), `ModelAdapterCatalog` (Task 5), `TokenStore`, `ProviderPluginRegistry`.
- Produces: `AgentWiring.catalog: ModelAdapterCatalog | None` and `_build_adapter_catalog(token_store, plugin_registry, ca_bundle) -> ModelAdapterCatalog`.

- [ ] **Step 1: Write the failing wiring test**

Append to `tests/test_main_wiring.py`:

```python
def test_agent_wiring_exposes_a_model_adapter_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.model_profiles import ModelAdapterCatalog

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    _stub_providers(monkeypatch)
    wiring = _build_agent_wiring(_agent_config(), cast("Any", object()), {})
    assert isinstance(wiring.catalog, ModelAdapterCatalog)
    assert wiring.catalog.descriptor("openai") is not None


def test_wiring_without_the_agent_extra_has_no_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from korvid.__main__ import _build_agent_wiring

    # `_build_agent_wiring` takes the unavailable branch when
    # `_missing_extra_packages(_AGENT_EXTRA_ROOTS)` returns a non-empty
    # list; forcing that reaches it without uninstalling an extra.
    monkeypatch.setattr(
        "korvid.__main__._missing_extra_packages", lambda roots: ["pydantic_ai"]
    )
    wiring = _build_agent_wiring(_agent_config(), cast("Any", object()), {})
    assert wiring.session is None
    assert wiring.catalog is None
```

These call the file's real helpers with their real signatures: `_agent_config(**overrides)`
returns an agent-enabled `KorvidConfig`, `_stub_providers(monkeypatch)` replaces
`korvid.providers.registry.create_provider`, and `_build_agent_wiring(config, kube,
aliases, …)` takes the kube client positionally (`cast("Any", object())` is the file's
own placeholder for it). `cast` and `pytest` are already imported there. The
unavailable branch is `missing = _missing_extra_packages(_AGENT_EXTRA_ROOTS)` followed by
`return _agent_unavailable_wiring(config, missing, ui_proxy, agent_ui_proxy, provider_box,
session_box)`, so patching `_missing_extra_packages` reaches that branch through its real
call instead of inventing a signature for it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/test_main_wiring.py -q -k catalog`
Expected: FAIL — `AttributeError: 'AgentWiring' object has no attribute 'catalog'`.

- [ ] **Step 3: Add the catalog to the wiring**

In `src/korvid/__main__.py`, add the field to `AgentWiring` (defaulting to `None` so `_agent_unavailable_wiring` is unchanged):

```python
    #: The single source of truth for installed model adapters, consumed
    #: by the `:ai` wizard. None when the agent extra is unavailable.
    catalog: ModelAdapterCatalog | None = None
```

Add the builder next to `_build_agent_wiring`:

```python
def _build_adapter_catalog(
    token_store: TokenStore,
    plugin_registry: ProviderPluginRegistry | None,
    ca_bundle: str | None,
) -> ModelAdapterCatalog:
    """Construct the adapter catalog the setup UI consumes."""
    from korvid.providers.adapter_catalog import ProviderModelCatalog

    return ProviderModelCatalog(
        token_store=token_store,
        plugin_registry=plugin_registry,
        ca_bundle=ca_bundle,
    )
```

In `_build_agent_wiring`, immediately after the `ProviderConfigurator` is constructed, build the catalog from the same `token_store`, `plugin_registry` and `ca_bundle` and pass `catalog=catalog` into the `AgentWiring(...)` construction.

Import `ModelAdapterCatalog` from `korvid.agent.model_profiles` at module scope (the agent layer is always importable); keep the `korvid.providers.adapter_catalog` import function-local so the base install still imports `__main__` without the `[agent]` extra.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/test_main_wiring.py tests/test_optional_extras.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck, layer check and commit**

```bash
uv run ruff check --fix src/korvid/__main__.py tests/test_main_wiring.py
uv run ruff format src/korvid/__main__.py tests/test_main_wiring.py
uv run mypy src/korvid/__main__.py
uv run tach check
git add src/korvid/__main__.py tests/test_main_wiring.py
git commit -m "feat: inject the model adapter catalog at the composition root" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Commit group 3 — Profile-driven setup UI (Tasks 8–10)

### Task 8: Profile manager stage

**Files:**
- Modify: `src/korvid/ui/widgets/agent_setup_screen.py`
- Create: `tests/ui/test_agent_setup_profiles.py`

**Interfaces:**
- Consumes: `ModelAdapterCatalog`, `ModelAdapterDescriptor`, `AgentProfilesConfig`, `AgentProfileConfig`, `AgentAuthConfig` from `korvid.agent.model_profiles`; `is_valid_profile_name` from `korvid.core.config`.
- Produces:
  - `AgentSetupScreen(ModalScreen["AgentProfilesConfig | None"])` with `__init__(self, catalog: ModelAdapterCatalog, profiles: AgentProfilesConfig, apply_profiles: Callable[[AgentProfilesConfig, str | None], bool] | None = None, current_tier: str | None = None) -> None`
  - Widget ids `#setup-profiles`, `#setup-profile-actions`, `#setup-profile-name`, and actions `action_delete_profile`, `action_cancel`.
  - Read-only seams `draft_profiles`, `status_text`, `seeded_endpoint`, `seeded_auth_settings`, `seeded_model`.

- [ ] **Step 1: Write the failing profile-manager tests**

Create `tests/ui/test_agent_setup_profiles.py`:

```python
"""The `:ai` wizard manages named profiles and reads adapters from the catalog."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList

from korvid.agent.model_profiles import (
    AgentAuthConfig,
    AgentProfileConfig,
    AgentProfilesConfig,
    AuthMethodDescriptor,
    DeviceLoginPrompt,
    EndpointRequirement,
    ModelAdapterCatalog,
    ModelAdapterDescriptor,
    SetupField,
    SetupFieldKind,
)
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen
from tests.ui.waits import until

_OPENAI = ModelAdapterDescriptor(
    id="openai",
    display_name="OpenAI-compatible endpoint",
    auth_methods=(
        AuthMethodDescriptor(id="none", display_name="No authentication"),
        AuthMethodDescriptor(
            id="environment",
            display_name="Environment variable",
            fields=(
                SetupField(
                    id="key",
                    label="Environment variable holding the API key",
                    kind=SetupFieldKind.SECRET_REF,
                    required=True,
                ),
            ),
        ),
    ),
    endpoint=EndpointRequirement.OPTIONAL,
    supports_model_discovery=True,
)
_LOCAL = ModelAdapterDescriptor(
    id="local-llm",
    display_name="Local LLM",
    auth_methods=(AuthMethodDescriptor(id="none", display_name="No authentication"),),
    endpoint=EndpointRequirement.REQUIRED,
    supports_model_discovery=False,
)


class FakeCatalog(ModelAdapterCatalog):
    """Catalog stub: records calls, never touches the network."""

    def __init__(
        self,
        descriptors: tuple[ModelAdapterDescriptor, ...] = (_OPENAI, _LOCAL),
        models: list[str] | None = None,
        test_error: Exception | None = None,
    ) -> None:
        self._descriptors = descriptors
        self._models = models if models is not None else []
        self._test_error = test_error
        self.tested: list[AgentProfileConfig] = []
        self.listed: list[AgentProfileConfig] = []
        self.auth_started: list[AgentProfileConfig] = []

    def descriptors(self) -> tuple[ModelAdapterDescriptor, ...]:
        return self._descriptors

    async def list_models(self, profile: AgentProfileConfig) -> list[str]:
        self.listed.append(profile)
        return list(self._models)

    async def test(self, profile: AgentProfileConfig) -> str:
        self.tested.append(profile)
        if self._test_error is not None:
            raise self._test_error
        return "ok"

    async def begin_auth(self, profile: AgentProfileConfig) -> DeviceLoginPrompt | None:
        self.auth_started.append(profile)
        return None

    async def finish_auth(self, profile: AgentProfileConfig) -> None:
        return None


class SetupApp(App[None]):
    def __init__(self, screen: AgentSetupScreen) -> None:
        super().__init__()
        self._screen = screen
        self.result: AgentProfilesConfig | None = None

    def compose(self) -> ComposeResult:
        return iter(())

    async def on_mount(self) -> None:
        self.push_screen(self._screen, callback=self._store)

    def _store(self, value: AgentProfilesConfig | None) -> None:
        self.result = value


_EXISTING = AgentProfilesConfig(
    active="production",
    profiles={
        "production": AgentProfileConfig(
            model="openai:gpt-4o",
            endpoint="https://api.openai.com/v1",
            auth=AgentAuthConfig(method="environment", settings={"key": "OPENAI_API_KEY"}),
        ),
        "local": AgentProfileConfig(model="local-llm:v1", endpoint="http://localhost:8000"),
    },
)


@pytest.mark.asyncio
async def test_existing_profiles_are_listed_in_file_order_with_the_active_one_highlighted() -> None:
    """Insertion order, never alphabetical: the list mirrors the config file."""
    screen = AgentSetupScreen(catalog=FakeCatalog(), profiles=_EXISTING)
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        options = screen.query_one("#setup-profiles", OptionList)
        prompts = [
            str(options.get_option_at_index(index).prompt)
            for index in range(options.option_count)
        ]
        assert prompts[0].startswith("production")
        assert "(active)" in prompts[0]
        assert prompts[1].startswith("local")
        assert prompts[-1] == "+ Add a new profile"
        assert options.highlighted == 0


@pytest.mark.asyncio
async def test_choosing_an_existing_profile_offers_activate_edit_and_delete() -> None:
    screen = AgentSetupScreen(catalog=FakeCatalog(), profiles=_EXISTING)
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        screen.query_one("#setup-profiles", OptionList).highlighted = 1
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-profile-actions", OptionList).display)
        actions = screen.query_one("#setup-profile-actions", OptionList)
        assert [
            actions.get_option_at_index(index).id for index in range(actions.option_count)
        ] == ["activate", "edit", "delete"]


@pytest.mark.asyncio
async def test_activating_a_profile_applies_it_without_reconfiguring_anything() -> None:
    """The switch-profile path never asks about adapters, endpoints or keys."""
    applied: list[tuple[AgentProfilesConfig, str | None]] = []
    screen = AgentSetupScreen(
        catalog=FakeCatalog(),
        profiles=_EXISTING,
        apply_profiles=lambda profiles, tier: (applied.append((profiles, tier)), True)[1],
    )
    app = SetupApp(screen)
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        screen.query_one("#setup-profiles", OptionList).highlighted = 1
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-profile-actions", OptionList).display)
        screen.query_one("#setup-profile-actions", OptionList).highlighted = 0
        await pilot.press("enter")
        await until(pilot, lambda: app.result is not None)
    assert applied
    assert applied[0][0].active == "local"
    assert applied[0][0].profiles["local"] == _EXISTING.profiles["local"]
    assert app.result is not None
    assert app.result.active == "local"


@pytest.mark.asyncio
async def test_editing_a_profile_seeds_the_wizard_with_its_current_values() -> None:
    """B1: an edit starts from what is configured, not from an empty form."""
    screen = AgentSetupScreen(catalog=FakeCatalog(), profiles=_EXISTING)
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        screen.query_one("#setup-profiles", OptionList).highlighted = 0
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-profile-actions", OptionList).display)
        screen.query_one("#setup-profile-actions", OptionList).highlighted = 1
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        assert screen.seeded_endpoint == "https://api.openai.com/v1"
        assert screen.seeded_auth_settings == {"key": "OPENAI_API_KEY"}
        assert screen.seeded_model == "gpt-4o"
        options = screen.query_one("#setup-adapter", OptionList)
        assert options.highlighted == 0


@pytest.mark.asyncio
async def test_adding_a_profile_asks_for_a_name_and_rejects_invalid_ones() -> None:
    screen = AgentSetupScreen(catalog=FakeCatalog(), profiles=_EXISTING)
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        screen.query_one("#setup-profiles", OptionList).highlighted = 2
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-profile-name", Input).display)
        name_input = screen.query_one("#setup-profile-name", Input)
        name_input.value = "bad name"
        await pilot.press("enter")
        await until(pilot, lambda: "letters" in screen.status_text)
        assert screen.query_one("#setup-profile-name", Input).display


@pytest.mark.asyncio
async def test_a_duplicate_profile_name_is_refused() -> None:
    screen = AgentSetupScreen(catalog=FakeCatalog(), profiles=_EXISTING)
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        screen.query_one("#setup-profiles", OptionList).highlighted = 2
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-profile-name", Input).display)
        screen.query_one("#setup-profile-name", Input).value = "production"
        await pilot.press("enter")
        await until(pilot, lambda: "already exists" in screen.status_text)
        assert screen.query_one("#setup-profile-name", Input).display


@pytest.mark.asyncio
async def test_adapters_come_from_the_catalog_not_a_hardcoded_list() -> None:
    catalog = FakeCatalog(descriptors=(_LOCAL,))
    screen = AgentSetupScreen(catalog=catalog, profiles=AgentProfilesConfig())
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        options = screen.query_one("#setup-adapter", OptionList)
        prompts = [
            str(options.get_option_at_index(index).prompt)
            for index in range(options.option_count)
        ]
        assert prompts == ["Local LLM"]


@pytest.mark.asyncio
async def test_an_unavailable_adapter_shows_its_install_hint_and_is_not_selectable() -> None:
    unavailable = ModelAdapterDescriptor(
        id="bedrock",
        display_name="Amazon Bedrock",
        auth_methods=(AuthMethodDescriptor(id="provider-default", display_name="SDK default"),),
        endpoint=EndpointRequirement.UNSUPPORTED,
        supports_model_discovery=False,
        available=False,
        install_hint="install korvid[provider-bedrock]",
    )
    screen = AgentSetupScreen(
        catalog=FakeCatalog(descriptors=(_LOCAL, unavailable)), profiles=AgentProfilesConfig()
    )
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        options = screen.query_one("#setup-adapter", OptionList)
        options.highlighted = 1
        await pilot.press("enter")
        await until(pilot, lambda: "provider-bedrock" in screen.status_text)
        assert screen.query_one("#setup-adapter", OptionList).display


@pytest.mark.asyncio
async def test_deleting_a_profile_removes_it_and_clears_a_stale_active() -> None:
    screen = AgentSetupScreen(catalog=FakeCatalog(), profiles=_EXISTING)
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        screen.query_one("#setup-profiles", OptionList).highlighted = 0
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: screen.query_one("#setup-profiles", OptionList).option_count == 2
        )
        assert set(screen.draft_profiles.profiles) == {"local"}
        assert screen.draft_profiles.active is None


@pytest.mark.asyncio
async def test_escape_discards_every_draft_edit() -> None:
    screen = AgentSetupScreen(catalog=FakeCatalog(), profiles=_EXISTING)
    app = SetupApp(screen)
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        screen.query_one("#setup-profiles", OptionList).highlighted = 1
        await pilot.press("ctrl+d")
        await pilot.press("escape")
        await until(pilot, lambda: app.result is None and screen.parent is None)
    assert app.result is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/ui/test_agent_setup_profiles.py -q`
Expected: FAIL — `TypeError: AgentSetupScreen.__init__() got an unexpected keyword argument 'catalog'`.

- [ ] **Step 3: Rewrite the screen's constructor, state and profile stage**

In `src/korvid/ui/widgets/agent_setup_screen.py`, replace the module docstring, imports, `_DEFAULTS`, `_PROVIDER_LABELS`, `_OPENAI_COMPAT_ALIASES` and `_canonical_provider` with:

```python
"""AgentSetupScreen: the in-TUI model connection wizard (`:ai`).

Stage order: profile → model adapter → endpoint → auth method → auth
fields → model → tier → live test → apply+save. Every stage is derived
from a `ModelAdapterDescriptor`, so the wizard has no provider names in
it: a third-party adapter is configured through exactly the same path as
a built-in one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from korvid.agent.model_profiles import (
    AgentAuthConfig,
    AgentProfileConfig,
    AgentProfilesConfig,
    AuthMethodDescriptor,
    EndpointRequirement,
    ModelAdapterCatalog,
    ModelAdapterDescriptor,
    SetupField,
    adapter_id,
    model_tag,
)
from korvid.core.config import is_valid_profile_name
from korvid.ui.widgets.agent_setup_fields import coerce_field_value, field_prompt

_OptionHandler = Callable[[OptionList.OptionSelected], None]
_InputHandler = Callable[[Input.Submitted], None]

_ADD_PROFILE_OPTION_ID = "__add__"
_ADD_PROFILE_LABEL = "+ Add a new profile"
```

Replace `class AgentSetupScreen(ModalScreen["AgentSettings | None"])`'s declaration, bindings and `__init__` with:

```python
class AgentSetupScreen(ModalScreen["AgentProfilesConfig | None"]):
    """Conversational wizard: one question at a time + a completed-step checklist."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+r", "retry", "Retry test", show=False),
        Binding("ctrl+d", "delete_profile", "Delete profile", show=True),
    ]

    def __init__(
        self,
        catalog: ModelAdapterCatalog,
        profiles: AgentProfilesConfig,
        apply_profiles: Callable[[AgentProfilesConfig, str | None], bool] | None = None,
        current_tier: str | None = None,
    ) -> None:
        super().__init__()
        self._catalog = catalog
        self._apply_profiles = apply_profiles
        # Every edit lands in this draft; nothing is applied or persisted
        # until the live test passes, so Esc always leaves the running
        # configuration untouched.
        self._draft = profiles
        self._model_tier = current_tier
        self._profile_name = ""
        self._descriptor: ModelAdapterDescriptor | None = None
        self._auth: AuthMethodDescriptor | None = None
        self._auth_settings: dict[str, object] = {}
        self._pending_fields: list[SetupField] = []
        self._endpoint: str | None = None
        self._options: Mapping[str, object] = {}
        self._models: list[str] = []
        self._chosen_model = ""
        self._candidate: AgentProfileConfig | None = None
        self._status_text = ""
        self._done_steps: list[str] = []

    @property
    def draft_profiles(self) -> AgentProfilesConfig:
        """The uncommitted profile collection (test seam)."""
        return self._draft

    @property
    def status_text(self) -> str:
        """The last status message shown (test seam)."""
        return self._status_text
```

Replace `compose` and `on_mount` with:

```python
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="setup-steps")
            yield Static("Which connection profile would you like to configure?", id="setup-title")
            yield OptionList(id="setup-profiles")
            yield OptionList(id="setup-profile-actions")
            yield Input(id="setup-profile-name", placeholder="profile name")
            yield OptionList(id="setup-adapter")
            yield Input(id="setup-endpoint", placeholder="endpoint URL")
            yield OptionList(id="setup-auth")
            yield Input(id="setup-field", placeholder="value")
            yield Input(id="setup-option", placeholder="value")
            yield Input(id="setup-model-filter", placeholder="type to filter — Enter to select")
            yield OptionList(id="setup-model-list")
            yield Input(id="setup-model", placeholder="model")
            yield OptionList(
                Option("Automatic", id="automatic"),
                Option("Low", id="low"),
                Option("High", id="high"),
                id="setup-tier",
            )
            yield Static(id="setup-device-code")
            yield Static(id="setup-status")

    _STAGE_WIDGETS: ClassVar[tuple[str, ...]] = (
        "#setup-profiles",
        "#setup-profile-actions",
        "#setup-profile-name",
        "#setup-adapter",
        "#setup-endpoint",
        "#setup-auth",
        "#setup-field",
        "#setup-option",
        "#setup-model-filter",
        "#setup-model-list",
        "#setup-model",
        "#setup-tier",
        "#setup-device-code",
    )

    def on_mount(self) -> None:
        for widget_id in self._STAGE_WIDGETS:
            self.query_one(widget_id).display = False
        if self._draft.profiles:
            self._show_profiles()
        else:
            # No profiles yet: skip straight to creating the first one.
            self._profile_name = "default"
            self._show_adapters()

    def _show_profiles(self) -> None:
        self._ask("Which connection profile would you like to configure?")
        options = self.query_one("#setup-profiles", OptionList)
        options.clear_options()
        # Insertion order, never sorted: the list mirrors the config file
        # the operator wrote, and the same order is written back out.
        names = list(self._draft.profiles)
        options.add_options(
            [
                Option(
                    f"{name} — {self._draft.profiles[name].model}"
                    + (" (active)" if name == self._draft.active else ""),
                    id=name,
                )
                for name in names
            ]
            + [Option(_ADD_PROFILE_LABEL, id=_ADD_PROFILE_OPTION_ID)]
        )
        options.display = True
        options.highlighted = names.index(self._draft.active) if self._draft.active in names else 0
        options.focus()

    def _select_profile(self, event: OptionList.OptionSelected) -> None:
        chosen = event.option.id or ""
        if chosen == _ADD_PROFILE_OPTION_ID:
            self._ask("What should the new profile be called?")
            self.query_one("#setup-profiles").display = False
            name_input = self.query_one("#setup-profile-name", Input)
            name_input.value = ""
            name_input.display = True
            name_input.focus()
            return
        self._profile_name = chosen
        self.query_one("#setup-profiles").display = False
        self._show_profile_actions(chosen)

    def _show_profile_actions(self, name: str) -> None:
        """Offer the three things an operator can do with an existing profile.

        Switching profiles is the common case and must not require
        re-answering the adapter, endpoint, auth and model questions, so
        `activate` is a direct path that applies the draft immediately.
        """
        self._ask(f"What would you like to do with {name}?")
        options = self.query_one("#setup-profile-actions", OptionList)
        options.clear_options()
        options.add_options(
            [
                Option(f"Use {name} for the agent", id="activate"),
                Option(f"Edit {name}", id="edit"),
                Option(f"Delete {name}", id="delete"),
            ]
        )
        options.display = True
        options.highlighted = 0
        options.focus()

    def _select_profile_action(self, event: OptionList.OptionSelected) -> None:
        action = event.option.id or ""
        self.query_one("#setup-profile-actions").display = False
        if action == "activate":
            self._activate_profile(self._profile_name)
            return
        if action == "delete":
            self._delete_profile(self._profile_name)
            self._show_profiles()
            return
        self._mark_done(f"Profile: {self._profile_name}")
        self._seed_from_existing(self._profile_name)
        self._show_adapters()

    def _activate_profile(self, name: str) -> None:
        """Apply an already-configured profile without reconfiguring it."""
        self._draft = replace(self._draft, active=name)
        if self._apply_profiles is not None and not self._apply_profiles(
            self._draft, self._model_tier
        ):
            self._status(f"Could not switch to {name} — the configuration was not saved")
            self._show_profiles()
            return
        self.dismiss(self._draft)

    def _seed_from_existing(self, name: str) -> None:
        """Prefill every stage from the profile being edited.

        Without this, editing a profile to change one field silently
        discards its endpoint, auth settings and options.
        """
        existing = self._draft.profiles.get(name)
        if existing is None:
            return
        self._endpoint = existing.endpoint
        self._auth_settings = dict(existing.auth.settings)
        self._options = dict(existing.options)
        self._chosen_model = model_tag(existing.model)

    def _submit_profile_name(self, event: Input.Submitted) -> None:
        name = event.input.value.strip()
        if not is_valid_profile_name(name):
            self._status(
                "Profile names use letters, digits, dot, underscore or hyphen (max 100 characters)"
            )
            return
        if name in self._draft.profiles:
            self._status(f"A profile named {name} already exists")
            return
        self._profile_name = name
        self.query_one("#setup-profile-name").display = False
        self._mark_done(f"Profile: {name}")
        self._show_adapters()

    def _delete_profile(self, name: str) -> None:
        """Remove *name* from the draft, clearing a now-dangling `active`.

        `unparsed` loses the same key. A profile whose `options` or `auth`
        block was rejected is held in *both* collections (Task 1), and
        `save_agent_profiles` re-emits every `unparsed` entry that has no
        parsed counterpart — so dropping it from `profiles` alone would
        write the deleted profile straight back to disk. Deleting from
        both is still not "deleting what korvid could not parse": the
        operator asked for this one by name.
        """
        remaining = {
            key: value for key, value in self._draft.profiles.items() if key != name
        }
        self._draft = replace(
            self._draft,
            active=self._draft.active if self._draft.active in remaining else None,
            profiles=remaining,
            unparsed={
                key: value for key, value in self._draft.unparsed.items() if key != name
            },
        )
        self._status(f"Removed {name} — press Enter on a profile to save the change")

    def action_delete_profile(self) -> None:
        """Delete the highlighted profile (`ctrl+d` shortcut for the menu entry)."""
        options = self.query_one("#setup-profiles", OptionList)
        if not options.display or options.highlighted is None:
            return
        option = options.get_option_at_index(options.highlighted)
        name = option.id or ""
        if name in (_ADD_PROFILE_OPTION_ID, ""):
            return
        self._delete_profile(name)
        if self._draft.profiles:
            self._show_profiles()
        else:
            options.clear_options()
            options.add_options([Option(_ADD_PROFILE_LABEL, id=_ADD_PROFILE_OPTION_ID)])
            options.highlighted = 0
```

Add the three read-only seams the seeding test reads, next to `draft_profiles`:

```python
    @property
    def seeded_endpoint(self) -> str | None:
        """The endpoint the wizard starts from (test seam)."""
        return self._endpoint

    @property
    def seeded_auth_settings(self) -> Mapping[str, object]:
        """The auth settings the wizard starts from (test seam)."""
        return dict(self._auth_settings)

    @property
    def seeded_model(self) -> str:
        """The model tag the wizard starts from (test seam)."""
        return self._chosen_model
```

Update `_status` so the test seam stays truthful:

```python
    def _status(self, text: str) -> None:
        self._status_text = text
        self.query_one("#setup-status", Static).update(text)
```

Add the adapter stage:

```python
    def _show_adapters(self) -> None:
        self._ask("Which model adapter should this profile use?")
        options = self.query_one("#setup-adapter", OptionList)
        options.clear_options()
        self._descriptors = self._catalog.descriptors()
        options.add_options(
            [
                Option(descriptor.display_name, id=descriptor.id)
                for descriptor in self._descriptors
            ]
        )
        options.display = True
        existing = self._draft.profiles.get(self._profile_name)
        current_adapter = adapter_id(existing.model) if existing is not None else ""
        ids = [descriptor.id for descriptor in self._descriptors]
        options.highlighted = ids.index(current_adapter) if current_adapter in ids else 0
        options.focus()

    def _select_adapter(self, event: OptionList.OptionSelected) -> None:
        descriptor = self._catalog.descriptor(event.option.id or "")
        if descriptor is None:
            self._status("That adapter is no longer installed")
            return
        if not descriptor.available:
            # Never substitute a different adapter: say what to install.
            self._status(descriptor.install_hint or f"{descriptor.display_name} is not installed")
            return
        self._descriptor = descriptor
        self.query_one("#setup-adapter").display = False
        self._mark_done(f"Adapter: {descriptor.display_name}")
        self._show_endpoint()
```

Register the new handlers:

```python
    def _option_handlers(self) -> dict[str, _OptionHandler]:
        return {
            "setup-profiles": self._select_profile,
            "setup-profile-actions": self._select_profile_action,
            "setup-adapter": self._select_adapter,
            "setup-auth": self._select_auth,
            "setup-model-list": self._select_model_option,
            "setup-tier": self._select_tier_option,
        }

    def _input_handlers(self) -> dict[str, _InputHandler]:
        return {
            "setup-profile-name": self._submit_profile_name,
            "setup-endpoint": self._submit_endpoint,
            "setup-field": self._submit_field,
            "setup-option": self._submit_option,
            "setup-model-filter": self._submit_model_filter,
            "setup-model": self._submit_model,
        }
```

Add `self._descriptors: tuple[ModelAdapterDescriptor, ...] = ()` to `__init__` so `_show_adapters` never reads an unset attribute. `_submit_option` arrives in Task 9 with the rest of the field machinery; until then, register it as a stub that raises `NotImplementedError` only if reached — the `#setup-option` widget stays hidden in this task, so no test can reach it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/ui/test_agent_setup_profiles.py -q -k "file_order or activate_edit_and_delete or activating or seeds or profile_name or duplicate or delete or escape or adapters or unavailable"`
Expected: PASS. Endpoint/auth/option/model stages still fail here — Task 9 implements them.

- [ ] **Step 5: Lint, format and commit**

```bash
uv run ruff check --fix src/korvid/ui/widgets/agent_setup_screen.py tests/ui/test_agent_setup_profiles.py
uv run ruff format src/korvid/ui/widgets/agent_setup_screen.py tests/ui/test_agent_setup_profiles.py
git add src/korvid/ui/widgets/agent_setup_screen.py tests/ui/test_agent_setup_profiles.py
git commit -m "feat: manage named profiles in the agent setup wizard" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 9: Descriptor-driven endpoint, auth and model stages

**Files:**
- Create: `src/korvid/ui/widgets/agent_setup_fields.py`
- Modify: `src/korvid/ui/widgets/agent_setup_screen.py`
- Modify: `tests/ui/test_agent_setup_profiles.py`

**Interfaces:**
- Consumes: `SetupField`, `SetupFieldKind`, `EndpointRequirement`, `AuthMethodDescriptor`, `ModelAdapterDescriptor` (Task 5); `FakeCatalog` (Task 8).
- Produces:
  - `field_prompt(field: SetupField) -> str`
  - `coerce_field_value(field: SetupField, raw: str) -> tuple[object | None, str | None]` — `(value, error)`; exactly one is non-None, and a blank optional field yields `(None, None)`.
  - `AgentSetupScreen._show_endpoint`, `_submit_endpoint`, `_show_auth_methods`, `_select_auth`, `_ask_next_field`, `_submit_field`, `_show_options`, `_ask_next_option`, `_submit_option`, `_draft_profile`.

- [ ] **Step 1: Write the failing field and stage tests**

Create `tests/ui/test_agent_setup_fields.py`:

```python
"""Declarative setup-field coercion used by the profile wizard."""

from __future__ import annotations

import pytest

from korvid.agent.model_profiles import SetupField, SetupFieldKind
from korvid.ui.widgets.agent_setup_fields import coerce_field_value, field_prompt


def test_prompt_marks_a_required_field() -> None:
    field = SetupField(id="key", label="API key variable", kind=SetupFieldKind.SECRET_REF, required=True)
    assert field_prompt(field) == "API key variable (required)"


def test_prompt_lists_choices() -> None:
    field = SetupField(
        id="api_version",
        label="API version",
        kind=SetupFieldKind.CHOICE,
        choices=("2024-10-21", "2025-04-01-preview"),
    )
    assert field_prompt(field) == "API version (2024-10-21, 2025-04-01-preview)"


def test_integer_field_rejects_non_numeric_input() -> None:
    field = SetupField(id="num_ctx", label="Context window", kind=SetupFieldKind.INTEGER)
    value, error = coerce_field_value(field, "many")
    assert value is None
    assert error == "Context window must be a whole number"


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("yes", True), ("no", False), ("0", False)])
def test_boolean_field_accepts_common_spellings(raw: str, expected: bool) -> None:
    field = SetupField(id="think", label="Enable thinking", kind=SetupFieldKind.BOOLEAN)
    value, error = coerce_field_value(field, raw)
    assert error is None
    assert value is expected


def test_choice_field_rejects_a_value_outside_the_choices() -> None:
    field = SetupField(
        id="api_version", label="API version", kind=SetupFieldKind.CHOICE, choices=("a", "b")
    )
    value, error = coerce_field_value(field, "c")
    assert value is None
    assert error == "API version must be one of: a, b"


def test_blank_required_field_is_an_error_and_blank_optional_field_is_omitted() -> None:
    required = SetupField(id="key", label="Key", kind=SetupFieldKind.SECRET_REF, required=True)
    optional = SetupField(id="key", label="Key", kind=SetupFieldKind.SECRET_REF)
    assert coerce_field_value(required, "  ") == (None, "Key is required")
    assert coerce_field_value(optional, "  ") == (None, None)


def test_secret_ref_field_keeps_the_reference_verbatim() -> None:
    field = SetupField(id="key", label="Key", kind=SetupFieldKind.SECRET_REF, required=True)
    assert coerce_field_value(field, " OPENAI_API_KEY ") == ("OPENAI_API_KEY", None)
```

Append to `tests/ui/test_agent_setup_profiles.py`:

```python
@pytest.mark.asyncio
async def test_an_adapter_that_forbids_an_endpoint_skips_the_endpoint_stage() -> None:
    no_endpoint = ModelAdapterDescriptor(
        id="hosted",
        display_name="Hosted",
        auth_methods=(AuthMethodDescriptor(id="none", display_name="No authentication"),),
        endpoint=EndpointRequirement.UNSUPPORTED,
        supports_model_discovery=False,
    )
    screen = AgentSetupScreen(
        catalog=FakeCatalog(descriptors=(no_endpoint,)), profiles=AgentProfilesConfig()
    )
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model", Input).display)
        assert not screen.query_one("#setup-endpoint", Input).display


@pytest.mark.asyncio
async def test_a_required_endpoint_must_not_be_blank() -> None:
    screen = AgentSetupScreen(
        catalog=FakeCatalog(descriptors=(_LOCAL,)), profiles=AgentProfilesConfig()
    )
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        screen.query_one("#setup-endpoint", Input).value = ""
        await pilot.press("enter")
        await until(pilot, lambda: "endpoint is required" in screen.status_text)
        assert screen.query_one("#setup-endpoint", Input).display


@pytest.mark.asyncio
async def test_auth_methods_and_their_fields_come_from_the_descriptor() -> None:
    catalog = FakeCatalog(descriptors=(_OPENAI,))
    screen = AgentSetupScreen(catalog=catalog, profiles=AgentProfilesConfig())
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        screen.query_one("#setup-endpoint", Input).value = "https://api.openai.com/v1"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-auth", OptionList).display)
        auth = screen.query_one("#setup-auth", OptionList)
        prompts = [
            str(auth.get_option_at_index(index).prompt) for index in range(auth.option_count)
        ]
        assert prompts == ["No authentication", "Environment variable"]
        auth.highlighted = 1
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-field", Input).display)
        assert "Environment variable holding the API key" in screen.question_text


@pytest.mark.asyncio
async def test_the_draft_profile_carries_the_collected_auth_reference() -> None:
    catalog = FakeCatalog(descriptors=(_OPENAI,), models=["gpt-4o", "gpt-4o-mini"])
    screen = AgentSetupScreen(catalog=catalog, profiles=AgentProfilesConfig())
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        screen.query_one("#setup-endpoint", Input).value = "https://api.openai.com/v1"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-auth", OptionList).display)
        screen.query_one("#setup-auth", OptionList).highlighted = 1
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-field", Input).display)
        screen.query_one("#setup-field", Input).value = "OPENAI_API_KEY"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model-list", OptionList).display)
        assert catalog.listed[-1].auth == AgentAuthConfig(
            method="environment", settings={"key": "OPENAI_API_KEY"}
        )
        assert catalog.listed[-1].endpoint == "https://api.openai.com/v1"
        assert catalog.listed[-1].model == "openai:"


@pytest.mark.asyncio
async def test_a_model_chosen_from_the_list_is_stored_with_its_adapter_prefix() -> None:
    catalog = FakeCatalog(descriptors=(_OPENAI,), models=["gpt-4o", "gpt-4o-mini"])
    screen = AgentSetupScreen(catalog=catalog, profiles=AgentProfilesConfig())
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-auth", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model-list", OptionList).display)
        screen.query_one("#setup-model-list", OptionList).highlighted = 1
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-tier", OptionList).display)
        assert screen.chosen_model_reference == "openai:gpt-4o-mini"


_TUNED = ModelAdapterDescriptor(
    id="tuned",
    display_name="Tuned adapter",
    auth_methods=(AuthMethodDescriptor(id="none", display_name="No authentication"),),
    endpoint=EndpointRequirement.REQUIRED,
    supports_model_discovery=False,
    option_fields=(
        SetupField(id="num_ctx", label="Context window", kind=SetupFieldKind.INTEGER),
        SetupField(id="think", label="Enable thinking", kind=SetupFieldKind.BOOLEAN),
    ),
)


@pytest.mark.asyncio
async def test_an_adapter_with_no_option_fields_skips_the_option_stage() -> None:
    catalog = FakeCatalog(descriptors=(_OPENAI,), models=["gpt-4o"])
    screen = AgentSetupScreen(catalog=catalog, profiles=AgentProfilesConfig())
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-auth", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model-list", OptionList).display)
        assert not screen.query_one("#setup-option", Input).display


@pytest.mark.asyncio
async def test_the_option_stage_collects_declared_tuning_into_the_draft() -> None:
    catalog = FakeCatalog(descriptors=(_TUNED,))
    screen = AgentSetupScreen(catalog=catalog, profiles=AgentProfilesConfig())
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        screen.query_one("#setup-endpoint", Input).value = "http://localhost:11434"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-option", Input).display)
        screen.query_one("#setup-option", Input).value = "16384"
        await pilot.press("enter")
        await until(pilot, lambda: screen.question_text.startswith("Enable thinking"))
        screen.query_one("#setup-option", Input).value = "yes"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model", Input).display)
        assert catalog.listed[-1].options == {"num_ctx": 16384, "think": True}


@pytest.mark.asyncio
async def test_an_invalid_option_is_refused_without_leaving_the_stage() -> None:
    screen = AgentSetupScreen(catalog=FakeCatalog(descriptors=(_TUNED,)), profiles=AgentProfilesConfig())
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        screen.query_one("#setup-endpoint", Input).value = "http://localhost:11434"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-option", Input).display)
        screen.query_one("#setup-option", Input).value = "lots"
        await pilot.press("enter")
        await until(pilot, lambda: "whole number" in screen.status_text)
        assert screen.query_one("#setup-option", Input).display


@pytest.mark.asyncio
async def test_editing_seeds_the_option_stage_and_a_blank_answer_clears_the_key() -> None:
    existing = AgentProfilesConfig(
        active="local",
        profiles={
            "local": AgentProfileConfig(
                model="tuned:v1",
                endpoint="http://localhost:11434",
                options={"num_ctx": 16384, "think": True},
            )
        },
    )
    catalog = FakeCatalog(descriptors=(_TUNED,))
    screen = AgentSetupScreen(catalog=catalog, profiles=existing)
    async with SetupApp(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-profile-actions", OptionList).display)
        screen.query_one("#setup-profile-actions", OptionList).highlighted = 1
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-option", Input).display)
        assert screen.query_one("#setup-option", Input).value == "16384"
        screen.query_one("#setup-option", Input).value = ""
        await pilot.press("enter")
        await until(pilot, lambda: screen.question_text.startswith("Enable thinking"))
        assert screen.query_one("#setup-option", Input).value == "True"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model", Input).display)
        assert catalog.listed[-1].options == {"think": True}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/ui/test_agent_setup_fields.py tests/ui/test_agent_setup_profiles.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'korvid.ui.widgets.agent_setup_fields'`.

- [ ] **Step 3: Write the field helper module**

Create `src/korvid/ui/widgets/agent_setup_fields.py`:

```python
"""Pure prompt/coercion helpers for declarative setup fields.

Kept out of the screen so the wizard's stage machinery stays under
ruff's complexity ceiling and the coercion rules are unit-testable
without mounting a Textual app.
"""

from __future__ import annotations

from korvid.agent.model_profiles import SetupField, SetupFieldKind

_TRUE_VALUES = frozenset({"true", "yes", "y", "on", "1"})
_FALSE_VALUES = frozenset({"false", "no", "n", "off", "0"})


def field_prompt(field: SetupField) -> str:
    """The question shown for *field*."""
    if field.kind is SetupFieldKind.CHOICE and field.choices:
        return f"{field.label} ({', '.join(field.choices)})"
    if field.required:
        return f"{field.label} (required)"
    return field.label


def coerce_field_value(field: SetupField, raw: str) -> tuple[object | None, str | None]:
    """Coerce operator input for *field*.

    Returns:
        `(value, None)` on success, `(None, message)` on a validation
        failure, and `(None, None)` when an optional field was left blank
        (the caller omits the key entirely rather than storing an empty
        string that a provider would reject later).
    """
    text = raw.strip()
    if not text:
        return (None, f"{field.label} is required") if field.required else (None, None)
    if field.kind is SetupFieldKind.INTEGER:
        try:
            return int(text), None
        except ValueError:
            return None, f"{field.label} must be a whole number"
    if field.kind is SetupFieldKind.BOOLEAN:
        lowered = text.lower()
        if lowered in _TRUE_VALUES:
            return True, None
        if lowered in _FALSE_VALUES:
            return False, None
        return None, f"{field.label} must be true or false"
    if field.kind is SetupFieldKind.CHOICE:
        if field.choices and text not in field.choices:
            return None, f"{field.label} must be one of: {', '.join(field.choices)}"
        return text, None
    return text, None
```

- [ ] **Step 4: Implement the descriptor-driven stages**

In `src/korvid/ui/widgets/agent_setup_screen.py`, add the endpoint, auth and field stages and the draft builder, and expose the two test seams:

```python
    @property
    def question_text(self) -> str:
        """The question currently shown (test seam)."""
        return self._question_text

    @property
    def chosen_model_reference(self) -> str:
        """The `provider:model` reference the wizard settled on (test seam)."""
        return self._chosen_model

    def _ask(self, question: str) -> None:
        self._question_text = question
        self.query_one("#setup-title", Static).update(question)

    def _show_endpoint(self) -> None:
        descriptor = self._descriptor
        assert descriptor is not None  # noqa: S101  # stage invariant, unreachable via the UI
        if descriptor.endpoint is EndpointRequirement.UNSUPPORTED:
            self._endpoint = None
            self._show_auth_methods()
            return
        existing = self._draft.profiles.get(self._profile_name)
        self._ask(f"Where is your {descriptor.display_name} endpoint?")
        endpoint_input = self.query_one("#setup-endpoint", Input)
        endpoint_input.value = existing.endpoint if existing and existing.endpoint else ""
        endpoint_input.display = True
        endpoint_input.focus()

    def _submit_endpoint(self, event: Input.Submitted) -> None:
        descriptor = self._descriptor
        assert descriptor is not None  # noqa: S101  # stage invariant
        endpoint = event.input.value.strip() or None
        if endpoint is None and descriptor.endpoint is EndpointRequirement.REQUIRED:
            self._status(f"An endpoint is required for {descriptor.display_name}")
            return
        self._endpoint = endpoint
        self.query_one("#setup-endpoint").display = False
        self._mark_done(f"Endpoint: {endpoint or 'default'}")
        self._show_auth_methods()

    def _show_auth_methods(self) -> None:
        descriptor = self._descriptor
        assert descriptor is not None  # noqa: S101  # stage invariant
        if len(descriptor.auth_methods) == 1:
            # A single supported method is not a question.
            self._begin_auth_method(descriptor.auth_methods[0])
            return
        self._ask("How should korvid authenticate?")
        options = self.query_one("#setup-auth", OptionList)
        options.clear_options()
        options.add_options(
            [Option(method.display_name, id=method.id) for method in descriptor.auth_methods]
        )
        options.display = True
        existing = self._draft.profiles.get(self._profile_name)
        ids = [method.id for method in descriptor.auth_methods]
        current = existing.auth.method if existing is not None else ""
        options.highlighted = ids.index(current) if current in ids else 0
        options.focus()

    def _select_auth(self, event: OptionList.OptionSelected) -> None:
        descriptor = self._descriptor
        assert descriptor is not None  # noqa: S101  # stage invariant
        chosen = event.option.id or ""
        method = next((item for item in descriptor.auth_methods if item.id == chosen), None)
        if method is None:
            self._status("That authentication method is not supported by this adapter")
            return
        self.query_one("#setup-auth").display = False
        self._begin_auth_method(method)

    def _begin_auth_method(self, method: AuthMethodDescriptor) -> None:
        self._auth = method
        existing = self._draft.profiles.get(self._profile_name)
        # Keep the seeded settings only when the operator stayed on the
        # method they already had; switching methods must not carry a
        # keyring entry name over into an environment-variable profile.
        if existing is None or existing.auth.method != method.id:
            self._auth_settings = {}
        self._pending_fields = list(method.fields)
        self._mark_done(f"Auth: {method.display_name}")
        if method.id == "device-login":
            self.run_worker(self._interactive_login(), exclusive=True)
            return
        self._ask_next_field()

    def _ask_next_field(self) -> None:
        if not self._pending_fields:
            self.query_one("#setup-field").display = False
            self._show_options()
            return
        field = self._pending_fields[0]
        self._ask(field_prompt(field))
        field_input = self.query_one("#setup-field", Input)
        seeded = self._auth_settings.get(field.id)
        field_input.value = str(seeded) if seeded is not None else (field.default or "")
        field_input.display = True
        field_input.focus()

    def _submit_field(self, event: Input.Submitted) -> None:
        if not self._pending_fields:
            return
        field = self._pending_fields[0]
        value, error = coerce_field_value(field, event.input.value)
        if error is not None:
            self._status(error)
            return
        if value is not None:
            self._auth_settings[field.id] = value
        else:
            self._auth_settings.pop(field.id, None)
        self._pending_fields.pop(0)
        # Field values are references (env var / keyring entry names),
        # never secrets, so echoing one in the checklist is safe.
        self._mark_done(f"{field.label}: {value if value is not None else '(unset)'}")
        self._ask_next_field()

    def _show_options(self) -> None:
        """Ask the adapter's declared tuning questions, seeded from the profile.

        An adapter with no `option_fields` skips the stage entirely, so
        the common path is unchanged.
        """
        descriptor = self._descriptor
        assert descriptor is not None  # noqa: S101  # stage invariant
        self._pending_options = list(descriptor.option_fields)
        self._ask_next_option()

    def _ask_next_option(self) -> None:
        if not self._pending_options:
            self.query_one("#setup-option").display = False
            self.run_worker(self._fetch_models(), exclusive=True)
            return
        field = self._pending_options[0]
        self._ask(field_prompt(field))
        option_input = self.query_one("#setup-option", Input)
        seeded = self._options.get(field.id)
        option_input.value = str(seeded) if seeded is not None else (field.default or "")
        option_input.display = True
        option_input.focus()

    def _submit_option(self, event: Input.Submitted) -> None:
        if not self._pending_options:
            return
        field = self._pending_options[0]
        value, error = coerce_field_value(field, event.input.value)
        if error is not None:
            self._status(error)
            return
        collected = dict(self._options)
        if value is None:
            # A blank optional field removes the key rather than storing
            # an empty string the adapter would have to interpret.
            collected.pop(field.id, None)
        else:
            collected[field.id] = value
        self._options = collected
        self._pending_options.pop(0)
        self._mark_done(f"{field.label}: {value if value is not None else '(unset)'}")
        self._ask_next_option()

    def _draft_profile(self, model_tag_value: str) -> AgentProfileConfig:
        descriptor = self._descriptor
        assert descriptor is not None  # noqa: S101  # stage invariant
        auth = self._auth
        return AgentProfileConfig(
            model=f"{descriptor.id}:{model_tag_value}",
            endpoint=self._endpoint,
            auth=AgentAuthConfig(
                method=auth.id if auth is not None else "none",
                settings=dict(self._auth_settings),
            ),
            options=dict(self._options),
        )
```

Add `self._pending_options: list[SetupField] = []` to `__init__` alongside `_pending_fields`,
and replace the Task 8 stub `_submit_option` with the implementation above.

Replace the model stage's default lookup and the worker bodies so they use the catalog and the draft profile:

```python
    def _show_model_step(self, models: list[str]) -> None:
        self._models = models
        existing = self._draft.profiles.get(self._profile_name)
        default_model = model_tag(existing.model) if existing is not None else ""
        if models:
            self._ask(f"Choose a model ({len(models)} available)")
            self.query_one("#setup-model-filter", Input).display = True
            model_list = self.query_one("#setup-model-list", OptionList)
            model_list.display = True
            self._populate_model_list(models)
            if default_model in models:
                model_list.highlighted = models.index(default_model)
            self.query_one("#setup-model-filter", Input).focus()
        else:
            self._ask("Which model should korvid use?")
            model_input = self.query_one("#setup-model", Input)
            model_input.value = default_model
            model_input.display = True
            model_input.focus()

    def _choose_model(self, model: str) -> None:
        self.query_one("#setup-model-filter", Input).display = False
        self.query_one("#setup-model-list", OptionList).display = False
        self.query_one("#setup-model", Input).display = False
        self._mark_done(f"Model: {model}")
        self._chosen_model = self._draft_profile(model).model
        self._ask_tier()

    async def _interactive_login(self) -> None:
        """Adapter-driven interactive login (device flow and equivalents)."""
        probe = self._draft_profile("")
        self._status("Checking for an existing login…")
        models = await self._catalog.list_models(probe)
        if models:
            self._mark_done("Signed in (existing session)")
            self._status("")
            self._show_model_step(models)
            return
        device = self.query_one("#setup-device-code", Static)
        try:
            prompt = await self._catalog.begin_auth(probe)
            if prompt is not None:
                device.display = True
                device.update(f"Enter code {prompt.user_code} at {prompt.verification_uri}")
                self._status("Waiting for authorization…")
            await self._catalog.finish_auth(probe)
        except Exception as exc:  # login errors must not crash the app
            self._status(f"Login failed: {exc}")
            return
        device.display = False
        self._mark_done("Signed in")
        self._status("Fetching available models…")
        models = await self._catalog.list_models(probe)
        self._status("")
        self._show_model_step(models)

    async def _fetch_models(self) -> None:
        self._status("Fetching available models…")
        models = await self._catalog.list_models(self._draft_profile(""))
        self._status("")
        self._show_model_step(models)
```

Add `self._question_text = ""` to `__init__`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/ui/test_agent_setup_fields.py tests/ui/test_agent_setup_profiles.py -q`
Expected: PASS for every test except the apply/save cases Task 10 adds.

- [ ] **Step 6: Lint, complexity check and commit**

```bash
uv run ruff check --fix src/korvid/ui/widgets/agent_setup_fields.py src/korvid/ui/widgets/agent_setup_screen.py tests/ui/
uv run ruff format src/korvid/ui/widgets/agent_setup_fields.py src/korvid/ui/widgets/agent_setup_screen.py tests/ui/
uv run mypy src/korvid/ui/widgets/agent_setup_fields.py src/korvid/ui/widgets/agent_setup_screen.py
git add src/korvid/ui/widgets/agent_setup_fields.py src/korvid/ui/widgets/agent_setup_screen.py tests/ui/test_agent_setup_fields.py tests/ui/test_agent_setup_profiles.py
git commit -m "feat: drive the agent setup wizard from adapter descriptors" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 10: Atomic apply, persistence, and controller wiring

**Files:**
- Modify: `src/korvid/ui/widgets/agent_setup_screen.py` (probe/apply/save, `action_retry`)
- Modify: `src/korvid/ui/agent_ui_controller.py`
- Modify: `src/korvid/ui/app.py` (setup screen construction)
- Modify: `src/korvid/__main__.py` (`_make_rebuild_agent`, `_persist_agent_settings` → `_persist_agent_profiles`)
- Modify: `tests/ui/test_agent_setup_profiles.py`, `tests/ui/test_agent_ui_controller.py`

**Interfaces:**
- Consumes: `AgentProfilesConfig`, `AgentProfileConfig`, `ModelAdapterCatalog` (Tasks 5–9); `save_agent_profiles` (Task 3).
- Produces:
  - `AgentUiController.apply_profiles(self, profiles: AgentProfilesConfig, model_tier: str | None) -> bool`
  - `AgentUiController.current_profiles(self) -> AgentProfilesConfig`
  - `AgentSetupScreen` dismisses with the committed `AgentProfilesConfig`.

The class is `AgentUiController` (lowercase `i`), it reports a live turn through the
`busy` property (there is no `_turn_active` flag), it notifies through
`self._ui.notify(message, severity=..., markup=...)`, and its injected
`rebuild: Callable[[AgentSettings], AgentSession | None]` returns a **session or
None**, not a bool. `apply_profiles` mirrors the existing `apply_settings` on all
four points.

- [ ] **Step 1: Write the failing transaction tests**

Append to `tests/ui/test_agent_setup_profiles.py`:

```python
@pytest.mark.asyncio
async def test_a_successful_run_applies_then_dismisses_with_the_new_collection() -> None:
    applied: list[AgentProfilesConfig] = []
    catalog = FakeCatalog(descriptors=(_LOCAL,))
    screen = AgentSetupScreen(
        catalog=catalog,
        profiles=AgentProfilesConfig(),
        apply_profiles=lambda profiles, tier: (applied.append(profiles), True)[1],
    )
    app = SetupApp(screen)
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        screen.query_one("#setup-endpoint", Input).value = "http://localhost:8000"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model", Input).display)
        screen.query_one("#setup-model", Input).value = "v1"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-tier", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: app.result is not None)
    assert applied
    assert applied[-1].active == "default"
    assert applied[-1].profiles["default"].model == "local-llm:v1"
    assert catalog.tested[-1].model == "local-llm:v1"
    assert app.result == applied[-1]


@pytest.mark.asyncio
async def test_a_failed_probe_neither_applies_nor_dismisses() -> None:
    applied: list[AgentProfilesConfig] = []
    catalog = FakeCatalog(descriptors=(_LOCAL,), test_error=RuntimeError("connection refused"))
    screen = AgentSetupScreen(
        catalog=catalog,
        profiles=AgentProfilesConfig(),
        apply_profiles=lambda profiles, tier: (applied.append(profiles), True)[1],
    )
    app = SetupApp(screen)
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        screen.query_one("#setup-endpoint", Input).value = "http://localhost:8000"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model", Input).display)
        screen.query_one("#setup-model", Input).value = "v1"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-tier", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: "connection refused" in screen.status_text)
    assert applied == []
    assert app.result is None


@pytest.mark.asyncio
async def test_a_refused_apply_does_not_dismiss_the_wizard() -> None:
    catalog = FakeCatalog(descriptors=(_LOCAL,))
    screen = AgentSetupScreen(
        catalog=catalog,
        profiles=AgentProfilesConfig(),
        apply_profiles=lambda profiles, tier: False,
    )
    app = SetupApp(screen)
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        screen.query_one("#setup-endpoint", Input).value = "http://localhost:8000"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model", Input).display)
        screen.query_one("#setup-model", Input).value = "v1"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-tier", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: "Apply failed" in screen.status_text)
    assert app.result is None


@pytest.mark.asyncio
async def test_editing_an_existing_profile_keeps_the_other_profiles() -> None:
    applied: list[AgentProfilesConfig] = []
    catalog = FakeCatalog(descriptors=(_LOCAL,))
    existing = AgentProfilesConfig(
        active="production",
        profiles={
            "production": AgentProfileConfig(model="local-llm:v0", endpoint="http://a:8000"),
            "spare": AgentProfileConfig(model="local-llm:v0", endpoint="http://b:8000"),
        },
    )
    screen = AgentSetupScreen(
        catalog=catalog,
        profiles=existing,
        apply_profiles=lambda profiles, tier: (applied.append(profiles), True)[1],
    )
    app = SetupApp(screen)
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        screen.query_one("#setup-endpoint", Input).value = "http://c:8000"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model", Input).display)
        screen.query_one("#setup-model", Input).value = "v2"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-tier", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: app.result is not None)
    assert set(applied[-1].profiles) == {"production", "spare"}
    assert applied[-1].profiles["production"].endpoint == "http://c:8000"
    assert applied[-1].profiles["spare"].endpoint == "http://b:8000"
    assert applied[-1].active == "production"


@pytest.mark.asyncio
async def test_saving_from_the_wizard_carries_the_unparsed_entries_through() -> None:
    """`:ai` must not delete what the parser could not model.

    `unparsed` (Task 1) holds the raw YAML of rejected or unmodellable
    profile entries. It is invisible in the wizard, so the only thing that
    can lose it is the screen rebuilding the collection instead of
    replacing fields on it.
    """
    applied: list[AgentProfilesConfig] = []
    existing = AgentProfilesConfig(
        active="production",
        profiles={"production": AgentProfileConfig(model="local-llm:v0", endpoint="http://a:8000")},
        unparsed={"experimental": {"model": "future-adapter:v9", "quirk": True}},
    )
    screen = AgentSetupScreen(
        catalog=FakeCatalog(descriptors=(_LOCAL,)),
        profiles=existing,
        apply_profiles=lambda profiles, tier: (applied.append(profiles), True)[1],
    )
    app = SetupApp(screen)
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-adapter", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-endpoint", Input).display)
        screen.query_one("#setup-endpoint", Input).value = "http://c:8000"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-model", Input).display)
        screen.query_one("#setup-model", Input).value = "v2"
        await pilot.press("enter")
        await until(pilot, lambda: screen.query_one("#setup-tier", OptionList).display)
        await pilot.press("enter")
        await until(pilot, lambda: app.result is not None)
    assert applied[-1].unparsed == {"experimental": {"model": "future-adapter:v9", "quirk": True}}


@pytest.mark.asyncio
async def test_deleting_a_profile_keeps_the_unparsed_entries() -> None:
    """Deletion is the other path that rebuilds the collection."""
    applied: list[AgentProfilesConfig] = []
    existing = AgentProfilesConfig(
        active="production",
        profiles={
            "production": AgentProfileConfig(model="local-llm:v0", endpoint="http://a:8000"),
            "spare": AgentProfileConfig(model="local-llm:v0", endpoint="http://b:8000"),
        },
        unparsed={"experimental": {"model": "future-adapter:v9"}},
    )
    screen = AgentSetupScreen(
        catalog=FakeCatalog(descriptors=(_LOCAL,)),
        profiles=existing,
        apply_profiles=lambda profiles, tier: (applied.append(profiles), True)[1],
    )
    app = SetupApp(screen)
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        await pilot.press("down")
        await pilot.press("ctrl+d")
        await until(pilot, lambda: "Removed spare" in screen.status_text)
    assert screen.draft_profiles.unparsed == {"experimental": {"model": "future-adapter:v9"}}
    assert set(screen.draft_profiles.profiles) == {"production"}


@pytest.mark.asyncio
async def test_deleting_a_rejected_profile_drops_its_unparsed_twin() -> None:
    """A delete must survive the writer's re-emission of `unparsed`.

    A profile whose `options` block korvid rejected is held in *both*
    `profiles` and `unparsed` (Task 1). `save_agent_profiles` writes back
    every `unparsed` entry that has no parsed counterpart, so a delete
    that only removed the parsed half would put the profile straight back
    into the file on the next save.
    """
    rejected = AgentProfileConfig(model="local-llm:v0", options={"blob": "x" * 4096})
    assert rejected.config_error is not None
    existing = AgentProfilesConfig(
        active="broken",
        profiles={
            "keep": AgentProfileConfig(model="local-llm:v0", endpoint="http://a:8000"),
            "broken": rejected,
        },
        unparsed={"broken": {"model": "local-llm:v0", "options": {"blob": "x" * 4096}}},
    )
    screen = AgentSetupScreen(
        catalog=FakeCatalog(descriptors=(_LOCAL,)),
        profiles=existing,
        apply_profiles=lambda profiles, tier: True,
    )
    app = SetupApp(screen)
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen.query_one("#setup-profiles", OptionList).display)
        await pilot.press("down")
        await pilot.press("ctrl+d")
        await until(pilot, lambda: "Removed broken" in screen.status_text)
    assert set(screen.draft_profiles.profiles) == {"keep"}
    assert "broken" not in screen.draft_profiles.unparsed
    assert screen.draft_profiles.active is None
```

The profile list is rendered in the collection's own order, so `down` from the
first entry highlights `broken`; the deletion clears `active`, which named it.

`draft_profiles` is the existing read-only property seam on the screen (Task 8 Step 3); the
deletion path never calls `apply_profiles`, so the draft is the only thing to assert on.

Controller tests go in `tests/ui/test_agent_ui_controller.py`, which builds the
controller through its `Env` harness (`tests/ui/test_agent_wiring.py` has no
controller helper). Extend `Env.__init__` with two pass-through keyword
arguments — `profiles: AgentProfilesConfig | None = None` and
`persist_profiles: Callable[[AgentProfilesConfig, str | None], None] | None = None`
— and forward them to `AgentUiController(profiles=…, persist_profiles=…)` in place of
the `configurator=` argument. Then append:

```python
_LOCAL_PROFILES = AgentProfilesConfig(
    active="local", profiles={"local": AgentProfileConfig(model="ollama:llama3")}
)


async def test_applying_profiles_swaps_the_session_and_persists(tmp_path: Path) -> None:
    fresh = ScriptedSession(policy=fake_policy(model="llama3"))
    persisted: list[tuple[AgentProfilesConfig, str | None]] = []
    env = Env(
        tmp_path=tmp_path,
        rebuild=lambda profiles, tier: fresh,
        persist_profiles=lambda profiles, tier: persisted.append((profiles, tier)),
    )
    assert env.controller.apply_profiles(_LOCAL_PROFILES, "low") is True
    assert env.controller.session is fresh
    assert env.controller.current_profiles() == _LOCAL_PROFILES
    assert env.controller.configured_model_tier == "low"
    assert persisted == [(_LOCAL_PROFILES, "low")]


async def test_a_failed_profile_rebuild_keeps_the_previous_session(tmp_path: Path) -> None:
    previous = ScriptedSession()
    persisted: list[AgentProfilesConfig] = []
    env = Env(
        tmp_path=tmp_path,
        session=previous,
        rebuild=lambda profiles, tier: None,
        persist_profiles=lambda profiles, tier: persisted.append(profiles),
    )
    assert env.controller.apply_profiles(_LOCAL_PROFILES, None) is False
    assert env.controller.session is previous
    # Nothing is written when nothing was applied: a restart must not
    # activate a configuration that never took effect.
    assert persisted == []


async def test_applying_profiles_is_refused_while_a_turn_is_running(tmp_path: Path) -> None:
    gate = asyncio.Event()
    previous = ScriptedSession(gate=gate)
    persisted: list[AgentProfilesConfig] = []
    env = Env(
        tmp_path=tmp_path,
        session=previous,
        rebuild=lambda profiles, tier: ScriptedSession(),
        persist_profiles=lambda profiles, tier: persisted.append(profiles),
    )
    env.controller.submit_prompt("hold")
    assert env.controller.busy is True
    assert env.controller.apply_profiles(_LOCAL_PROFILES, None) is False
    assert env.controller.session is previous
    assert persisted == []
    assert any("busy" in message for message in env.ui.messages())
    gate.set()
    await env.controller.wait_for_turn()
    await env.close()
```

`ScriptedSession` (including its `gate=` parameter), `fake_policy`, `Env` and
`wait_for_turn` already exist in that file; import `AgentProfileConfig` and
`AgentProfilesConfig` from `korvid.agent.model_profiles`, and `asyncio` is already
imported. The gate holds the turn open deterministically — no sleep — and `busy`
is read through the public property, because the real controller derives it from
the in-flight task (`self._task is not None and not self._task.done()`) and has no
settable flag.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/ui/test_agent_setup_profiles.py tests/ui/test_agent_ui_controller.py -q -k "profile or probe or editing or apply or unparsed"`
Expected: FAIL — `TypeError: AgentUiController.__init__() got an unexpected keyword argument 'profiles'`, and the wizard never dismisses with a collection.

Two things about that filter. `profile` is **singular**: `-k` matches substrings of the
whole node id, so the module name `test_agent_setup_profiles` already selects that file
wholesale, but in `test_agent_ui_controller.py` the plural would miss
`test_a_failed_profile_rebuild_keeps_the_previous_session`. And `apply or unparsed` is
redundant *today* — `test_a_refused_apply_does_not_dismiss_the_wizard`,
`test_saving_from_the_wizard_carries_the_unparsed_entries_through`,
`test_deleting_a_profile_keeps_the_unparsed_entries` and
`test_deleting_a_rejected_profile_drops_its_unparsed_twin` all live in
`test_agent_setup_profiles.py` and are already selected by its module name — but it
stops being redundant the moment one of them moves to the controller file, and a RED
filter that silently stops covering a test is worse than a slightly wide one.

Every test added in Step 1 is selected by this filter; confirm that with
`--collect-only -q` before reading the failure list, because a RED step that silently
collected fewer tests than it wrote proves nothing.

- [ ] **Step 3: Commit the draft atomically in the screen**

In `src/korvid/ui/widgets/agent_setup_screen.py`, replace `_choose_tier`, `_probe` and `action_retry`:

```python
    def _choose_tier(self, tier_id: str) -> None:
        self.query_one("#setup-tier", OptionList).display = False
        self._model_tier = None if tier_id == "automatic" else tier_id
        self._mark_done(f"Tier: {tier_id.capitalize()}")
        self._candidate = self._draft_profile(model_tag(self._chosen_model))
        self.run_worker(self._probe(), exclusive=True)

    def _committed_draft(self, profile: AgentProfileConfig) -> AgentProfilesConfig:
        """The draft collection with this profile stored and made active.

        `replace` rather than a fresh construction: `unparsed` (Task 1)
        carries the raw YAML of entries the parser could not model, and
        rebuilding the collection field-by-field would drop them, so
        saving from `:ai` would delete configuration korvid merely failed
        to understand.
        """
        profiles = dict(self._draft.profiles)
        profiles[self._profile_name] = profile
        return replace(self._draft, active=self._profile_name, profiles=profiles)

    async def _probe(self) -> None:
        candidate = self._candidate
        if candidate is None:
            return
        self._status("Testing connection…")
        try:
            await self._catalog.test(candidate)
        except Exception as exc:  # keep the wizard open on probe failure
            self._status(f"Test failed: {exc} — press Ctrl+R to retry, Esc to cancel")
            return
        committed = self._committed_draft(candidate)
        # The tier travels beside the collection: it is one app-wide agent
        # setting (`agent.model_tier`), not a per-profile field.
        if self._apply_profiles is not None and not self._apply_profiles(
            committed, self._model_tier
        ):
            # The app refused the swap (busy turn / rebuild failure): stay
            # open and do NOT persist, so a restart cannot silently activate
            # a configuration that never took effect.
            self._status("Apply failed — press Ctrl+R to retry, Esc to cancel")
            return
        self._draft = committed
        self.dismiss(committed)

    def action_retry(self) -> None:
        if self._candidate is None:
            return
        # Re-read the still-visible inputs so an edit after a failed probe is
        # actually tested (an interactive login is not repeated; a model
        # picked from the list is kept unless the fallback input is visible).
        endpoint_input = self.query_one("#setup-endpoint", Input)
        if endpoint_input.display:
            self._endpoint = endpoint_input.value.strip() or None
        model_input = self.query_one("#setup-model", Input)
        tag = model_tag(self._chosen_model)
        if model_input.display:
            tag = model_input.value.strip()
            if not tag:
                self._status("Model is required")
                return
        self._candidate = self._draft_profile(tag)
        self.run_worker(self._probe(), exclusive=True)
```

Persistence moved out of the screen: the controller's `apply_profiles` both swaps the runtime and persists, so the wizard no longer has a separate save step that can succeed after an apply failure.

- [ ] **Step 4: Give the controller profile-shaped apply**

In `src/korvid/ui/agent_ui_controller.py`, replace `apply_settings` with `apply_profiles`.
It keeps every guard the existing method has — an unavailable rebuild, a live turn, a
raising rebuild, and a rebuild that returns `None` — because each one exists to stop a
different way of losing the running session:

```python
    def current_profiles(self) -> AgentProfilesConfig:
        """The profile collection the wizard should start from."""
        return self._profiles

    def apply_profiles(self, profiles: AgentProfilesConfig, model_tier: str | None) -> bool:
        """Swap the running agent onto *profiles* and persist them.

        `model_tier` is one app-wide agent setting rather than a profile
        field, so it travels beside the collection and is written by the
        same call.

        Transactional, exactly as `apply_settings` was: on any failure
        the previous session and profiles are kept, nothing is written,
        and False is returned — so config.yaml can never describe a
        configuration that never took effect.
        """
        if self._rebuild is None:
            self._ui.notify(
                f"Agent rebuild unavailable — {isolated_install_hint(feature='agent')}",
                severity="warning",
                markup=False,
            )
            return False
        if self.busy:
            self._ui.notify(
                "Agent is busy — wait for the current turn to finish", severity="warning"
            )
            return False
        try:
            session = self._rebuild(profiles, model_tier)
        except Exception as exc:
            self._ui.notify(f"Agent rebuild failed: {exc}", severity="error", markup=False)
            return False
        if session is None:
            self._ui.notify(
                "Agent rebuild failed — check configuration; keeping previous agent",
                severity="error",
            )
            return False
        self._session = session
        self._session_closed = False
        self._disconnected = False
        self._model_name = session.policy.model.model
        self._profiles = profiles
        self._configured_tier = model_tier
        try:
            self._persist_profiles(profiles, model_tier)
        except OSError as exc:
            self._ui.notify(
                f"Applied, but saving config failed: {exc}", severity="warning", markup=False
            )
        self._refresh_status()
        self._panel.enable_input()
        if self._panel.expanded():
            self._render_header(session, self._model_name)
            self._panel.focus_input()
        return True
```

`__init__` takes `profiles: AgentProfilesConfig`, `persist_profiles:
Callable[[AgentProfilesConfig, str | None], None]` and `catalog:
ModelAdapterCatalog | None`, and its existing `rebuild` parameter is retyped to
`Callable[[AgentProfilesConfig, str | None], AgentSession | None] | None` — the
`configurator` parameter goes away. `persist_profiles` matches
`_persist_agent_profiles(profiles, model_tier)` from Task 3 exactly.

There is **no** `settings` parameter to remove: the controller takes
`config: Callable[[], KorvidConfig]` and snapshots it once in `__init__` as
`settings = config()` (`agent_ui_controller.py:534`). That parameter and that
snapshot both stay — `settings.agent_model_tier` still seeds `self._configured_tier`
(:539) and `settings.agent_follow` still seeds `self._follow` (:561), and neither is a
provider scalar. What goes away is the block in between: the
`if settings.agent_provider and settings.agent_model:` seeding of `self._settings`
(an `AgentSettings` built from `agent_base_url`, `agent_api_key_env`,
`agent_auth_method` and `agent_options`) is replaced by the injected `profiles`, and
`self._settings` is deleted with `AgentSettings` in Task 17. Read the tier through
`self._configured_tier`, which is the attribute that actually exists — there is no
`self._model_tier` on this class, and naming one would be an `AttributeError` the
first time an operator pressed `:ai`.

`_open_setup` becomes:

```python
    def _open_setup(self) -> None:
        if self._catalog is None:
            self._notify("Agent support is not installed — install korvid[agent]")
            return
        self._app.push_screen(
            AgentSetupScreen(
                catalog=self._catalog,
                profiles=self._profiles,
                apply_profiles=self.apply_profiles,
                current_tier=self._configured_tier,
            )
        )
```

and `handle_model_command` retargets the *active* profile only. Its real signature
takes the parsed argument **list** and already handles the no-argument and
too-many-arguments cases; only the branch that switches the model changes:

```python
    def handle_model_command(self, args: list[str]) -> None:
        """`:model` shows the current model; `:model <tag>` retargets the active profile."""
        if len(args) > 1:
            self._ui.notify("Usage: :model [name]", severity="warning")
            return
        if not args:
            # Report only a live model: at startup config may carry a model
            # name even though provider creation failed (session is None).
            if self._session is not None and self._model_name:
                self._ui.notify(f"Agent model: {self._model_name}")
            else:
                self._ui.notify("Agent not configured — run :ai first", severity="warning")
            return
        active = self._profiles.active_profile
        if active is None:
            self._ui.notify("Agent not configured — run :ai first", severity="warning")
            return
        retargeted = dataclasses.replace(active, model=with_model_tag(active.model, args[0]))
        profiles = dict(self._profiles.profiles)
        profiles[str(self._profiles.active)] = retargeted
        # `with_model_tag` keeps the current adapter for a bare tag and
        # honours an explicit `<adapter>:<model>` reference, so `:model
        # llama3.1` stays on this profile's adapter while a qualified
        # reference moves deliberately.
        if self.apply_profiles(
            dataclasses.replace(self._profiles, profiles=profiles),
            self._configured_tier,
        ):
            self._ui.notify(f"Agent model set to {retargeted.model}")
```

`dataclasses.replace` rather than `AgentProfilesConfig(active=…, profiles=…)`: `active` is
unchanged here anyway, and reconstructing the collection would discard `unparsed` (Task 1),
turning `:model` into a command that quietly deletes profiles korvid could not parse.

`apply_profiles` both applies and persists, so the old `_switch()` worker — which
applied, then awaited `configurator.save`, then notified — collapses into this
synchronous call; delete the worker and the `self._ui.run_worker(_switch(), …)`
line with it. `dataclasses` is already imported by the module; add
`AgentProfileConfig`, `AgentProfilesConfig`, `ModelAdapterCatalog` and
`with_model_tag` from `korvid.agent.model_profiles`.

Add the matching test to `tests/ui/test_agent_ui_controller.py` (the existing
`test_model_command_rejects_trailing_arguments` keeps passing unchanged):

```python
async def test_the_model_command_retargets_the_active_profile_only(tmp_path: Path) -> None:
    fresh = ScriptedSession(policy=fake_policy(model="llama3.1"))
    applied: list[AgentProfilesConfig] = []
    env = Env(
        tmp_path=tmp_path,
        session=ScriptedSession(),
        profiles=_LOCAL_PROFILES,
        rebuild=lambda profiles, tier: (applied.append(profiles), fresh)[1],
        persist_profiles=lambda profiles, tier: None,
    )
    env.controller.handle_model_command(["llama3.1"])
    assert applied[-1].profiles["local"].model == "ollama:llama3.1"
    assert applied[-1].active == "local"
```

- [ ] **Step 5: Rewire the composition root**

In `src/korvid/__main__.py`:

- Change `_make_rebuild_agent`'s `build_provider` parameter to
  `Callable[[AgentProfileConfig], LLMProvider | None]` and its returned closure to
  `def rebuild_agent(profiles: AgentProfilesConfig, model_tier: str | None) -> AgentSession | None`.
  It resolves `profiles.active_profile`, returns `None` when there is none, builds the
  provider from that profile, and passes `model_tier` (no longer `settings.model_tier`)
  to `compose` and into `tier_box[0]`. Task 14 replaces `build_provider`'s body, not this
  signature.
- Replace `_persist_agent_settings` with the `_persist_agent_profiles(profiles,
  model_tier)` added in Task 3, and pass it to the controller as `persist_profiles=`.
- Construct the controller with `profiles=cfg.agent_profiles` and
  `catalog=wiring.catalog`, dropping the `configurator=` argument.

**The interim `build_provider`, exactly.** Until Task 14 lands `create_profile_provider`,
`build_provider` is an adapter over the *existing* legacy builder — and it must make the
same decisions `load_config` already makes, or `:ai` will build a provider that startup
would have refused. Do not re-derive those decisions here; call the one function that owns
them:

```python
    def build_provider(profile: AgentProfileConfig) -> LLMProvider | None:
        """Build a provider from a profile using the pre-Task-14 transport.

        Task 14 Step 5 replaces this body with
        `create_profile_provider(profile, …)`; the signature is already the
        final one.

        `_derive_legacy_scalars` is the single place that knows how a
        profile projects onto `create_provider`'s arguments *and* which
        profiles the legacy transport must refuse (`config_error`, and the
        adapters in `_ADAPTERS_WITHOUT_LEGACY_TRANSPORT`). Re-deriving any
        of that here would let `:ai` activate a profile that `load_config`
        disables on the next start — the two paths would disagree about
        the same file.
        """
        warnings: list[str] = []
        legacy = _derive_legacy_scalars(
            AgentProfilesConfig(
                active=LEGACY_PROFILE_NAME, profiles={LEGACY_PROFILE_NAME: profile}
            ),
            warnings,
        )
        if not legacy.enabled:
            for warning in warnings:
                logger.warning("%s", warning)
            return None
        return create_provider(
            enabled=True,
            provider=legacy.provider,
            auth_method=legacy.auth_method,
            base_url=legacy.base_url,
            model=legacy.model,
            api_key_env=legacy.api_key_env,
            oauth_token=token_store.load("github-oauth"),
            ollama=ollama_options,
            ca_bundle=config.network_ca_bundle,
            plugin_registry=plugin_registry,
            options=legacy.options,
        )
```

This replaces the existing nested `build_provider(settings: AgentSettings)` closure at
`__main__.py:1053` in place — same name, same enclosing scope, so `token_store`,
`ollama_options`, `config` and `plugin_registry` stay captured exactly as they are today.
Only the parameter type and the six argument expressions change. `enabled=True` is kept
verbatim from that closure: `create_provider` declares it keyword-only *and* required, and
the profile-level "should this run at all" decision has already been made by
`legacy.enabled` above.

The synthetic single-profile `AgentProfilesConfig` is how a *profile* is fed to a function
that takes a *collection*; `LEGACY_PROFILE_NAME` is reused as its key so no new name is
invented for a structure that lives for one call. `warnings` is a local list drained to the
logger rather than to `startup_warnings`, because this runs on a `:ai` apply, long after
startup — the wizard's own failure path shows the operator the refusal.

Import `AgentProfilesConfig`, `LEGACY_PROFILE_NAME` and `_derive_legacy_scalars` from
`korvid.core.config` in `__main__.py`'s existing config import block; `__main__.py` is the
composition root and already imports other module-private legacy helpers there (Task 3).
Keep every argument name above — they are `create_provider`'s current keyword-only
parameters, and it accepts nothing positionally.

**Task 14 Step 5 replaces this body**, and removes `_ADAPTERS_WITHOUT_LEGACY_TRANSPORT` and its
guard test in `core/config.py`: once every adapter has a real transport, a function whose
whole purpose is "refuse the ones that do not" has nothing left to refuse.

`_make_retarget_agent` is **not** touched: it re-arms the agent for a new cluster
(`:ctx`) and takes `(session, resize_supported, cluster)` — it has nothing to do with
model profiles.

Update `tests/test_main_wiring.py` where it calls the rebuild closure: it now takes two
arguments, and its first is an `AgentProfilesConfig`.

- [ ] **Step 6: Migrate the legacy wizard test file**

`tests/ui/test_agent_setup_screen.py` is the existing coverage for this screen. Migrate it
here rather than deleting it: the behaviours it pins (Esc cancels, the checklist grows, a
failed probe keeps the wizard open, the tier stage dismisses) are still required, and
deleting the file would drop them silently.

For each test in that file:

1. Replace `AgentSetupScreen(configurator=…, current_settings=…)` with
   `AgentSetupScreen(catalog=FakeCatalog(…), profiles=AgentProfilesConfig(…))`, importing
   `FakeCatalog`, `_LOCAL` and `_OPENAI` from `tests.ui.test_agent_setup_profiles` so
   there is one stub catalog rather than two.
2. Replace `#setup-provider` with `#setup-adapter` and provider display names with
   descriptor display names.
3. Replace assertions on `AgentSettings` with assertions on `AgentProfilesConfig`.
4. Where a test asserted the *hardcoded* provider list (`OpenAI-compatible`, `Ollama`,
   `GitHub Copilot`, …), rewrite it to assert that the list equals the display names of the
   catalog's descriptors — the behaviour under test becomes "the wizard shows what the
   catalog offers", which is the invariant this migration introduces. Do not delete the
   test.

Run: `uv run pytest -p no:tach tests/ui/test_agent_setup_screen.py -q`
Expected: PASS.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/ui/ tests/test_main_wiring.py -q`
Expected: PASS.

- [ ] **Step 8: Lint, typecheck, layer check and commit**

```bash
uv run ruff check --fix src/korvid/ui/ src/korvid/__main__.py tests/ui/
uv run ruff format src/korvid/ui/ src/korvid/__main__.py tests/ui/
uv run mypy src/korvid/ui/ src/korvid/__main__.py
uv run tach check
git add src/korvid/ui src/korvid/__main__.py tests/ui
git commit -m "feat: apply and persist model profiles from the setup wizard" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Files** for this task therefore also include: Modify `tests/ui/test_agent_setup_screen.py` (migrated, not deleted).

---

## Commit group 4 — Pydantic AI model transport (Tasks 11–15)

### Task 11: Declare the transport dependencies and regenerate the lock

**Files:**
- Modify: `pyproject.toml`
- Modify: `tests/test_optional_extras.py`
- Modify: `uv.lock` (produced by the Relock workflow — never generated locally)

**Interfaces:**
- Produces: the `[agent]` floor `pydantic-ai-slim>=2.35.3,<3`, the `httpx2` floor it needs, and the four `provider-*` extras every later task's install hints name.

The extra names are not guesses. Read from the installed distribution metadata of
`pydantic-ai-slim` 2.35.3, its `Provides-Extra` list contains `openai`, `anthropic`,
`google` and `bedrock` (among ~38 others), and `pydantic-ai-slim[openai]` pulls in
`openai` 3.5.0, whose `Requires-Dist` is `httpx2<3,>=2.7.0` — the legacy `httpx` is not a
dependency of it at all. korvid's own `[agent]` floor is therefore `httpx2>=2.12`, above
the SDK's own minimum, because korvid constructs the clients it hands to Pydantic AI
providers and 2.12 is the version whose `Auth`/`MockTransport` behaviour this plan's tests
were written against.

- [ ] **Step 1: Write the failing dependency-declaration test**

Append to `tests/test_optional_extras.py`:

```python
def test_agent_extra_pins_the_pydantic_ai_floor() -> None:
    metadata = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    agent = metadata["project"]["optional-dependencies"]["agent"]
    assert "pydantic-ai-slim>=2.35.3,<3" in agent


@pytest.mark.parametrize(
    ("extra", "sdk"),
    [
        ("provider-openai", "openai"),
        ("provider-anthropic", "anthropic"),
        ("provider-google", "google"),
        ("provider-bedrock", "bedrock"),
    ],
)
def test_each_provider_extra_pins_its_sdk_group(extra: str, sdk: str) -> None:
    metadata = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    extras = metadata["project"]["optional-dependencies"]
    assert extras[extra] == [f"pydantic-ai-slim[{sdk}]>=2.35.3,<3"]


def test_the_all_extra_includes_every_provider_extra() -> None:
    metadata = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    all_extra = metadata["project"]["optional-dependencies"]["all"][0]
    for extra in ("provider-openai", "provider-anthropic", "provider-google", "provider-bedrock"):
        assert extra in all_extra


def test_the_agent_extra_pins_the_httpx2_floor() -> None:
    metadata = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    agent = metadata["project"]["optional-dependencies"]["agent"]
    assert "httpx2>=2.12" in agent


def test_deptry_maps_pydantic_ai_slim_to_its_module() -> None:
    metadata = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    mapping = metadata["tool"]["deptry"]["package_module_name_map"]
    assert mapping["pydantic-ai-slim"] == "pydantic_ai"


def test_the_base_install_imports_no_pydantic_ai_module() -> None:
    assert "pydantic_ai" in _AGENT_MODULES
    _assert_import_is_extra_free("korvid.__main__")
```

Add `import tomllib` and `_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"` (with `from pathlib import Path`), and extend the existing watched tuple so the subprocess probe fails if the base import pulls Pydantic AI in:

```python
_AGENT_MODULES = ("httpx", "httpx2", "keyring", "pydantic_ai")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/test_optional_extras.py -q`
Expected: FAIL — `KeyError: 'provider-openai'`.

- [ ] **Step 3: Declare the dependencies**

In `pyproject.toml`, replace the `agent` extra and add the provider extras:

```toml
# Embedded AI agent: the Pydantic AI model layer + OS-keychain credential
# storage. `pydantic-ai-slim` is the transport for every adapter; the
# per-vendor SDKs live in the `provider-*` extras below so an operator
# installs only the vendors they actually connect to.
#
# The `<3` ceiling is not caution: korvid drives `Model.request_stream`
# and translates `StreamedResponse` events itself, which is exactly the
# surface a major bump is entitled to change.
agent = [
    "httpx>=0.27",
    # `httpx2` is a separate distribution from `httpx`, not an upgrade of
    # it. Pydantic AI 2.35.3 and openai 3.5.0 take `httpx2` clients;
    # korvid's own connectors still use legacy `httpx`, so both are
    # declared and both are installed.
    "httpx2>=2.12",
    "keyring>=25.7.0",
    "pydantic-ai-slim>=2.35.3,<3",
]
provider-openai = ["pydantic-ai-slim[openai]>=2.35.3,<3"]
provider-anthropic = ["pydantic-ai-slim[anthropic]>=2.35.3,<3"]
provider-google = ["pydantic-ai-slim[google]>=2.35.3,<3"]
provider-bedrock = ["pydantic-ai-slim[bedrock]>=2.35.3,<3"]
```

and widen `all`:

```toml
all = [
    "korvid[agent,mcp,observability,provider-openai,provider-anthropic,provider-google,provider-bedrock]",
]
```

Leave the `entra` extra exactly as it is — it is permanent. Azure's `provider-default`
auth method builds its token provider from `EntraCredentialSource` (Task 14), so neither
the extra nor `providers/entra.py` is removed by the legacy deletion in Task 17.

deptry resolves an import to a distribution by name, and `pydantic_ai` does not match
`pydantic-ai-slim`. Add the mapping in the same edit:

```toml
[tool.deptry.package_module_name_map]
pydantic-ai-slim = "pydantic_ai"
```

If the table already exists, add the entry to it rather than declaring it twice.

- [ ] **Step 4: Check the requirement strings resolve, outside the repo**

The dev environment is installed `--frozen`, so `pyproject.toml` alone proves
nothing about whether these requirements resolve. Verify them in a throwaway
venv that never touches the repo's `uv.lock`, `pyproject.toml` or `.venv`:

```bash
uv venv --python 3.11 ~/.cache/korvid-extras-check
UV_PROJECT_ENVIRONMENT= uv pip install --python ~/.cache/korvid-extras-check/bin/python \
  'pydantic-ai-slim[openai,anthropic,google,bedrock]>=2.35.3,<3'
~/.cache/korvid-extras-check/bin/python -c "
from importlib.metadata import version
for dist in ('pydantic-ai-slim', 'openai', 'anthropic', 'google-genai', 'boto3', 'httpx2'):
    print(dist, version(dist))
"
rm -rf ~/.cache/korvid-extras-check
```
Expected: `pydantic-ai-slim` is ≥ 2.35.3 and < 3, and every SDK plus `httpx2`
resolves. `git status` must show no change to `uv.lock` after this step.

**This step has already been run once**, which is where the API baseline table above
comes from: `pydantic-ai-slim==2.35.3` and all four provider extras (`openai`,
`anthropic`, `google`, `bedrock`) installed successfully through this network's proxy into
a throwaway venv outside the repository. So the requirement strings in Step 3 are known to
resolve, and this step is a re-confirmation on the day of the change rather than a first
attempt that might fail. **Installing into a scratch venv through the proxy is a different
operation from `uv lock`**: the scratch venv is discarded and records nothing, whereas
`uv lock` writes ~1,700 mirror-scoped artefact URLs into a file that ships. That
distinction is the reason Step 4 is allowed here and Step 6 is not.

- [ ] **Step 5: Commit the manifest change before relocking**

```bash
uv run ruff check --fix tests/test_optional_extras.py
uv run ruff format tests/test_optional_extras.py
git add pyproject.toml tests/test_optional_extras.py
git commit -m "build: declare the pydantic-ai model transport dependencies" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git push
```

- [ ] **Step 6: Regenerate `uv.lock` on a runner with PyPI access**

This network cannot resolve public PyPI, and `uv lock` run here would rewrite every artefact URL to the mirror — which `no-private-index-in-lock` and `tests/test_lockfile.py` both reject. **Never run `uv lock` locally.** Dispatch the existing workflow instead:

```bash
# Anchor on a timestamp taken before the dispatch, so the poll below can
# only ever match a run this command started. `gh run list --limit 1`
# alone is a race: the API indexes a dispatched run a few seconds late,
# so it usually returns the *previous* relock run and `gh run watch`
# then reports a stale success.
SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
gh workflow run relock.yml \
  -f base=agents/provider-neutral-profiles \
  -f package=pydantic-ai-slim

RELOCK_RUN=""
for _ in $(seq 1 30); do
  RELOCK_RUN="$(gh run list --workflow=relock.yml --event=workflow_dispatch \
    --json databaseId,createdAt \
    --jq "[.[] | select(.createdAt >= \"$SINCE\")] | max_by(.createdAt) | .databaseId")"
  [ -n "$RELOCK_RUN" ] && [ "$RELOCK_RUN" != "null" ] && break
  sleep 5
done
if [ -z "$RELOCK_RUN" ] || [ "$RELOCK_RUN" = "null" ]; then
  echo "no relock run appeared within 150s; check gh run list --workflow=relock.yml" >&2
  exit 1
fi
echo "watching relock run $RELOCK_RUN"
gh run watch "$RELOCK_RUN" --exit-status
```
Expected: the run succeeds (`gh run watch --exit-status` returns non-zero if it does not, so a failed relock stops the task instead of being read past). It verifies the lock *inside the job* (lint, mypy, tach, pytest and the PyPI-only assertion) and then pushes a helper branch named `relock/<timestamp>` and opens a helper PR against `agents/provider-neutral-profiles`.

`createdAt` is compared as an ISO-8601 string, which sorts lexicographically in UTC, so
`max_by` picks the newest run and the `>= $SINCE` filter excludes every earlier one. The
loop bounds the wait at 150 seconds and fails loudly rather than watching nothing.

- [ ] **Step 7: Take the verified lock into this branch**

```bash
git fetch origin
RELOCK_BRANCH="$(git branch -r --list 'origin/relock/*' --sort=-committerdate | head -1 | tr -d ' ')"
if [ -z "$RELOCK_BRANCH" ]; then
  echo "no origin/relock/* branch found; the relock run did not push one" >&2
  exit 1
fi
echo "$RELOCK_BRANCH"
git checkout "$RELOCK_BRANCH" -- uv.lock
uv run pytest -p no:tach tests/test_lockfile.py -q
grep -c 'files.pythonhosted.org' uv.lock
```
Expected: `tests/test_lockfile.py` passes and every artefact URL points at `files.pythonhosted.org`. If any URL names an internal host, stop — the lock came from a mirror and must not be committed.

```bash
uv sync --frozen --dev --all-extras
git add uv.lock
git commit -m "build: lock pydantic-ai-slim for the model transport" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git push
```

- [ ] **Step 8: Close the helper PR — never merge it**

The helper PR exists only to carry a verified lock. Its content now lives on the feature branch, so it is closed and its branch deleted:

```bash
# Recomputed, not inherited: Step 7 ran in its own shell, and no shell
# variable survives between the plan's command blocks. Selecting the same
# branch the same way is what makes this step safe to run on its own.
git fetch origin
RELOCK_BRANCH="$(git branch -r --list 'origin/relock/*' --sort=-committerdate | head -1 | tr -d ' ')"
if [ -z "$RELOCK_BRANCH" ]; then
  echo "no origin/relock/* branch found; nothing to close" >&2
  exit 1
fi
RELOCK_PR="$(gh pr list --head "${RELOCK_BRANCH#origin/}" --json number --jq '.[0].number')"
if [ -z "$RELOCK_PR" ]; then
  echo "no PR found for ${RELOCK_BRANCH#origin/}; close it by hand" >&2
  exit 1
fi
gh pr close "$RELOCK_PR" --delete-branch \
  --comment "Lock applied to agents/provider-neutral-profiles; closing the helper PR unmerged."
```
Expected: the helper PR is `CLOSED` and `relock/<timestamp>` is deleted. Do not run `gh pr merge` on it, and do not enable auto-merge on it or on the feature PR.

Both guards fail loudly rather than letting an empty `--head ""` list every open PR and
`gh pr close ""` act on the wrong one.

- [ ] **Step 9: Verify the extras test now passes, and that nothing still skips**

```bash
uv run pytest -p no:tach tests/test_optional_extras.py -q
```

Expected: PASS.

The point of this task is that the adapter tests stop skipping, so check that directly
rather than inferring it from a green suite:

```bash
uv run pytest -p no:tach tests/core/test_config_profiles.py -q -rs \
  -k "azure_url or deployment_scoped"
```

Expected: `2 passed`, and the `-rs` skip report empty. Task 2 Step 3 wrote this Azure
MockTransport URL table against a real `openai` client and it has been skipping ever
since; this is the run that makes it evidence. If it still reports
`could not import 'openai'`, the sync in Step 7 did not install the `[agent]` extra —
stop and fix that before continuing, because every later task's contract test is behind
the same import.

Add the same no-skip assertion to the suite itself, so a future contributor cannot
regress it silently. In `tests/test_optional_extras.py` (which already imports `os` and
`pytest`):

```python
@pytest.mark.skipif(not os.environ.get("CI"), reason="CI installs every extra")
@pytest.mark.parametrize("module", ["openai", "httpx2", "pydantic_ai"])
def test_ci_installs_the_transport_extras(module: str) -> None:
    """A skipped adapter test is not a passing adapter test.

    CI runs `uv sync --locked --dev --all-extras`, so every
    `importorskip` in `tests/providers/` must be a no-op there. Without
    this, a lock that quietly dropped `openai` would turn the entire
    transport suite into silent skips and the pipeline would stay green.
    """
    assert importlib.import_module(module) is not None
```

Add `import importlib` to that module's imports if it is not already there.

---

### Task 12: Canonical message and tool conversion

**Files:**
- Create: `src/korvid/providers/pydantic_messages.py`
- Create: `tests/providers/test_pydantic_messages.py`

**Interfaces:**
- Consumes: `pydantic_ai.messages` (`ModelMessage`, `ModelRequest`, `ModelResponse`, `SystemPromptPart`, `UserPromptPart`, `TextPart`, `ToolCallPart`, `ToolReturnPart`), `pydantic_ai.tools.ToolDefinition`.
- Produces:
  - `MessageConversionError(Exception)`
  - `to_model_messages(messages: list[dict[str, Any]]) -> list[ModelMessage]`
  - `to_tool_definitions(tools: list[dict[str, Any]]) -> list[ToolDefinition]`

- [ ] **Step 1: Write the failing conversion tests**

Create `tests/providers/test_pydantic_messages.py`:

```python
"""Canonical korvid messages → Pydantic AI messages (conversion adds nothing)."""

from __future__ import annotations

import json

import pytest

pydantic_ai = pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import (  # noqa: E402  # after importorskip
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from korvid.providers.pydantic_messages import (  # noqa: E402  # after importorskip
    MessageConversionError,
    to_model_messages,
    to_tool_definitions,
)


def test_system_and_user_turns_convert_in_order() -> None:
    converted = to_model_messages(
        [
            {"role": "system", "content": "You are korvid."},
            {"role": "user", "content": "why is my pod pending"},
        ]
    )
    assert len(converted) == 1
    request = converted[0]
    assert isinstance(request, ModelRequest)
    assert isinstance(request.parts[0], SystemPromptPart)
    assert request.parts[0].content == "You are korvid."
    assert isinstance(request.parts[1], UserPromptPart)
    assert request.parts[1].content == "why is my pod pending"


def test_assistant_tool_calls_convert_with_their_json_arguments() -> None:
    converted = to_model_messages(
        [
            {"role": "user", "content": "check"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "list_resources",
                            "arguments": json.dumps({"kind": "Pod"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "3 pods"},
        ]
    )
    response = converted[1]
    assert isinstance(response, ModelResponse)
    call = response.parts[0]
    assert isinstance(call, ToolCallPart)
    assert call.tool_name == "list_resources"
    assert call.tool_call_id == "call_1"
    assert call.args_as_dict() == {"kind": "Pod"}
    result = converted[2]
    assert isinstance(result, ModelRequest)
    assert isinstance(result.parts[0], ToolReturnPart)
    assert result.parts[0].tool_call_id == "call_1"
    # Correlated from the preceding call: `agent/conversation.py` stores
    # tool results with no name at all.
    assert result.parts[0].tool_name == "list_resources"


def test_a_tool_result_uses_the_tool_name_key_korvid_actually_writes() -> None:
    """`agent/outbound.py::provider_prepared_messages` emits `tool_name`, not `name`."""
    converted = to_model_messages(
        [
            {"role": "user", "content": "check"},
            {
                "role": "tool",
                "tool_call_id": "call_9",
                "tool_name": "get_logs",
                "content": "…",
            },
        ]
    )
    request = converted[0]
    assert isinstance(request, ModelRequest)
    part = request.parts[1]
    assert isinstance(part, ToolReturnPart)
    assert part.tool_name == "get_logs"


def test_a_legacy_name_key_is_still_honoured() -> None:
    converted = to_model_messages(
        [{"role": "tool", "tool_call_id": "c", "name": "legacy_tool", "content": "x"}]
    )
    part = converted[0].parts[0]
    assert isinstance(part, ToolReturnPart)
    assert part.tool_name == "legacy_tool"


def test_an_empty_assistant_turn_is_skipped_not_faked() -> None:
    """A synthetic empty `TextPart` is a reply the assistant never sent."""
    converted = to_model_messages(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "user", "content": "still there?"},
        ]
    )
    assert [type(message).__name__ for message in converted] == ["ModelRequest", "ModelRequest"]
    assert not any(
        isinstance(part, TextPart) for message in converted for part in message.parts
    )


def test_assistant_text_converts_to_a_text_part() -> None:
    converted = to_model_messages(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    )
    response = converted[1]
    assert isinstance(response, ModelResponse)
    assert isinstance(response.parts[0], TextPart)
    assert response.parts[0].content == "hello"


def test_conversion_preserves_content_exactly() -> None:
    redacted = "token=[REDACTED] · 한국어 · <script>"
    converted = to_model_messages([{"role": "user", "content": redacted}])
    part = converted[0].parts[0]
    assert isinstance(part, UserPromptPart)
    assert part.content == redacted


def test_an_unknown_role_is_rejected_rather_than_dropped() -> None:
    with pytest.raises(MessageConversionError, match="unsupported message role"):
        to_model_messages([{"role": "developer", "content": "x"}])


def test_a_tool_result_without_a_call_id_is_rejected() -> None:
    with pytest.raises(MessageConversionError, match="tool result without tool_call_id"):
        to_model_messages([{"role": "tool", "content": "x"}])


def test_malformed_tool_call_arguments_are_rejected() -> None:
    with pytest.raises(MessageConversionError, match="tool call arguments"):
        to_model_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c", "function": {"name": "n", "arguments": "{not json"}}
                    ],
                }
            ]
        )


def test_tool_schemas_convert_without_alteration() -> None:
    schema = {
        "type": "object",
        "properties": {"kind": {"type": "string"}},
        "required": ["kind"],
    }
    definitions = to_tool_definitions(
        [
            {
                "type": "function",
                "function": {
                    "name": "list_resources",
                    "description": "List resources",
                    "parameters": schema,
                },
            }
        ]
    )
    assert len(definitions) == 1
    assert definitions[0].name == "list_resources"
    assert definitions[0].description == "List resources"
    assert definitions[0].parameters_json_schema == schema


def test_an_empty_tool_list_converts_to_an_empty_definition_list() -> None:
    assert to_tool_definitions([]) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/providers/test_pydantic_messages.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'korvid.providers.pydantic_messages'`.

- [ ] **Step 3: Write the converter**

Create `src/korvid/providers/pydantic_messages.py`:

```python
"""Canonical korvid messages → Pydantic AI messages.

This is the last step before transmission and it is deliberately dumb:
it re-encodes what `OutboundPolicy` already redacted, canonicalized and
size-checked. It never adds a system prompt, never injects a tool, and
never rewrites content — the snapshot the audit log recorded and the
bytes on the wire must describe the same request.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.tools import ToolDefinition


class MessageConversionError(Exception):
    """Raised when a canonical message cannot be represented faithfully.

    Conversion fails loudly rather than dropping a turn: silently
    discarding a tool result would change the model's view of the
    conversation without anything in the audit trail saying so.
    """


def _assistant_response(
    message: dict[str, Any], tool_names: dict[str, str]
) -> ModelResponse | None:
    """Convert one assistant turn, or None when it carries nothing.

    Records every tool call's `tool_call_id → tool_name` in *tool_names*
    so a later tool result can be labelled even when the canonical
    message omits the name.

    An assistant turn with neither text nor tool calls is dropped rather
    than replaced with an empty `TextPart`: a synthetic empty text part
    is a message the assistant never sent, and models are entitled to
    treat it as a real (empty) reply.
    """
    parts: list[TextPart | ToolCallPart] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        parts.append(TextPart(content=content))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            raise MessageConversionError("assistant tool call is not a mapping")
        function = call.get("function")
        if not isinstance(function, dict):
            raise MessageConversionError("assistant tool call has no function payload")
        raw_arguments = function.get("arguments", "{}")
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except (TypeError, ValueError) as exc:
            raise MessageConversionError("assistant tool call arguments are not valid JSON") from exc
        name = str(function.get("name", ""))
        call_id = str(call.get("id", ""))
        if call_id:
            tool_names[call_id] = name
        parts.append(ToolCallPart(tool_name=name, args=arguments, tool_call_id=call_id))
    if not parts:
        return None
    return ModelResponse(parts=parts)


def _tool_result_name(message: dict[str, Any], call_id: str, tool_names: dict[str, str]) -> str:
    """The tool name for a canonical tool result.

    korvid's own harness writes `tool_name` (see
    `agent/outbound.py::provider_prepared_messages`), while
    `agent/conversation.py` stores tool results as
    `{"role", "tool_call_id", "content"}` with no name at all. Reading
    `"name"` first — as an earlier draft of this plan did — produced an
    empty `ToolReturnPart.tool_name` for every korvid tool result, which
    breaks Anthropic and Google, whose wire formats key results by name.
    """
    for key in ("tool_name", "name"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return tool_names.get(call_id, "")


def to_model_messages(messages: list[dict[str, Any]]) -> list[ModelMessage]:
    """Convert canonical korvid messages into Pydantic AI messages.

    Consecutive `system`/`user`/`tool` turns collapse into one
    `ModelRequest`, matching how Pydantic AI models a turn; assistant
    turns become `ModelResponse`s. Ordering is preserved exactly.

    Raises:
        MessageConversionError: On an unknown role or a malformed turn.
    """
    converted: list[ModelMessage] = []
    pending: list[ModelRequestPart] = []
    tool_names: dict[str, str] = {}

    def flush() -> None:
        if pending:
            converted.append(ModelRequest(parts=list(pending)))
            pending.clear()

    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            pending.append(SystemPromptPart(content=str(content or "")))
        elif role == "user":
            pending.append(UserPromptPart(content=str(content or "")))
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id:
                raise MessageConversionError("tool result without tool_call_id")
            pending.append(
                ToolReturnPart(
                    tool_name=_tool_result_name(message, str(call_id), tool_names),
                    content=str(content or ""),
                    tool_call_id=str(call_id),
                )
            )
        elif role == "assistant":
            flush()
            response = _assistant_response(message, tool_names)
            if response is not None:
                converted.append(response)
        else:
            raise MessageConversionError(f"unsupported message role: {role!r}")
    flush()
    return converted


def to_tool_definitions(tools: list[dict[str, Any]]) -> list[ToolDefinition]:
    """Convert canonical tool schemas into Pydantic AI tool definitions.

    The JSON schema is passed through verbatim — the harness owns the
    contract the model is shown, and a transport that edited it would
    silently change what the model may ask korvid to do.
    """
    definitions: list[ToolDefinition] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            raise MessageConversionError("tool schema has no function payload")
        parameters = function.get("parameters")
        definitions.append(
            ToolDefinition(
                name=str(function.get("name", "")),
                description=str(function.get("description", "")),
                parameters_json_schema=parameters if isinstance(parameters, dict) else {},
            )
        )
    return definitions
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/providers/test_pydantic_messages.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck, layer check and commit**

```bash
uv run ruff check --fix src/korvid/providers/pydantic_messages.py tests/providers/test_pydantic_messages.py
uv run ruff format src/korvid/providers/pydantic_messages.py tests/providers/test_pydantic_messages.py
uv run mypy src/korvid/providers/pydantic_messages.py
uv run tach check
git add src/korvid/providers/pydantic_messages.py tests/providers/test_pydantic_messages.py
git commit -m "feat: convert canonical messages to the pydantic-ai model layer" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 13: The `LLMProvider` over a Pydantic AI `Model`

**Files:**
- Create: `src/korvid/providers/pydantic_model.py`
- Create: `tests/providers/test_pydantic_model.py`

**Interfaces:**
- Consumes: `LLMProvider`, `REQUEST_SENT` from `korvid.agent.provider`; `ModelDescriptor`, `ModelCapabilities` from `korvid.agent.model_policy`; `to_model_messages`, `to_tool_definitions` (Task 12); from Pydantic AI 2.35.3 exactly: `pydantic_ai.models.Model`, `pydantic_ai.models.ModelRequestParameters`, `pydantic_ai.models.StreamedResponse`, and `pydantic_ai.messages.{PartStartEvent, PartDeltaEvent, TextPart, TextPartDelta, ToolCallPart}`.
- Produces: `PydanticModelProvider(LLMProvider)` with `__init__(self, model: Model, descriptor: ModelDescriptor, *, capabilities: ModelCapabilities | None = None, model_settings: ModelSettings | None = None) -> None` — `ModelSettings` is `pydantic_ai.settings.ModelSettings`, a `total=False` TypedDict, carried through untyped-free (no `cast`, no `Any`).

**The two 2.35.3 behaviours this task is built around** (both reproduced against the installed package, not inferred):

1. A `PartStartEvent` for a tool call carries only the *first fragment* of `args`. A stream that reports `ToolCallPart(args='{"kind":')` and then `ToolCallPartDelta(args_delta='"Pod"}')` is normal, and only `StreamedResponse.get().parts` (or `PartEndEvent`) holds the assembled `args='{"kind":"Pod"}'`. Therefore **korvid emits tool calls only from the assembled parts after the stream drains** — never from `PartStartEvent`. Emitting on start would hand the harness truncated JSON.
2. `StreamedResponse.usage` is a **property** returning `RequestUsage`, whose `input_tokens`/`output_tokens` are `int` and default to `0` — never `None`. A `is not None` check therefore always passes and would emit `usage 0/0` for every provider that reports nothing. korvid keeps its existing "both counts known or no event" contract by emitting only when both counts are truthy, and documents that `0` is indistinguishable from "not reported".

- [ ] **Step 1: Write the failing streaming-contract tests**

The doubles below subclass the **real** `StreamedResponse` and drive the library's own
`ModelResponsePartsManager`, so the assembly semantics under test are Pydantic AI's,
not a hand-written imitation of them.

Create `tests/providers/test_pydantic_model.py`:

```python
"""PydanticModelProvider reproduces korvid's provider event contract exactly."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import (  # noqa: E402  # after importorskip
    ModelMessage,
    ModelResponseStreamEvent,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models import (  # noqa: E402  # after importorskip
    Model,
    ModelRequestParameters,
    StreamedResponse,
)
from pydantic_ai.settings import ModelSettings  # noqa: E402  # after importorskip
from pydantic_ai.usage import RequestUsage  # noqa: E402  # after importorskip

from korvid.agent.model_policy import ModelDescriptor  # noqa: E402
from korvid.agent.provider import REQUEST_SENT  # noqa: E402
from korvid.providers.pydantic_model import PydanticModelProvider  # noqa: E402


@dataclass(frozen=True, slots=True)
class Chunk:
    """One wire fragment: either text, or a piece of a tool call."""

    text: str | None = None
    tool_name: str | None = None
    args: str | None = None
    tool_call_id: str | None = None


class ScriptedStream(StreamedResponse):
    """A real `StreamedResponse` fed by scripted fragments.

    Fragments go through `self._parts_manager`, the same assembler every
    genuine Pydantic AI model uses, so a test that splits a tool call's
    JSON across two chunks exercises the library's real reassembly.
    """

    def __init__(self, chunks: list[Chunk], usage: RequestUsage | None = None) -> None:
        super().__init__(model_request_parameters=ModelRequestParameters())
        self._chunks = chunks
        self.closed = False
        if usage is not None:
            self._usage = usage

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        for chunk in self._chunks:
            if chunk.text is not None:
                for event in self._parts_manager.handle_text_delta(
                    vendor_part_id="text", content=chunk.text
                ):
                    yield event
                continue
            event = self._parts_manager.handle_tool_call_delta(
                vendor_part_id="tool",
                tool_name=chunk.tool_name,
                args=chunk.args,
                tool_call_id=chunk.tool_call_id,
            )
            if event is not None:
                yield event

    async def close_stream(self) -> None:
        self.closed = True

    @property
    def model_name(self) -> str:
        return "fake-model"

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def provider_url(self) -> str:
        return "https://fake.invalid"

    @property
    def timestamp(self) -> datetime:
        return datetime(2026, 9, 5, tzinfo=UTC)


class FakeModel(Model):
    """A `Model` whose `request_stream` matches 2.35.3's exact signature."""

    def __init__(
        self,
        stream: ScriptedStream | None = None,
        error: Exception | None = None,
        enter_delay: float = 0.0,
        client: object | None = None,
    ) -> None:
        self._stream = stream if stream is not None else ScriptedStream([])
        self._error = error
        self._enter_delay = enter_delay
        self._client = client
        self.requests: list[tuple[Any, Any, ModelRequestParameters]] = []
        self.exited = False

    @property
    def model_name(self) -> str:
        return "fake-model"

    @property
    def system(self) -> str:
        return "fake"

    @property
    def client(self) -> object | None:
        return self._client

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> Any:
        raise NotImplementedError("korvid only streams")

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any = None,
    ) -> AsyncIterator[ScriptedStream]:
        self.requests.append((messages, model_settings, model_request_parameters))
        if self._enter_delay:
            await asyncio.sleep(self._enter_delay)
        if self._error is not None:
            raise self._error
        try:
            yield self._stream
        finally:
            self.exited = True


def _provider(model: FakeModel) -> PydanticModelProvider:
    return PydanticModelProvider(
        model=model, descriptor=ModelDescriptor(provider="openai", model="gpt-4o")
    )


async def _events(model: FakeModel, tools: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    provider = _provider(model)
    return [
        event
        async for event in provider.complete([{"role": "user", "content": "hi"}], tools or [])
    ]


async def test_text_deltas_are_yielded_after_request_sent() -> None:
    model = FakeModel(ScriptedStream([Chunk(text="Hel"), Chunk(text="lo")]))
    events = await _events(model)
    assert events[0] == {"type": REQUEST_SENT}
    assert [event["text"] for event in events if event["type"] == "text_delta"] == ["Hel", "lo"]
    assert events[-1] == {"type": "done"}


async def test_a_tool_call_split_across_chunks_is_emitted_once_and_complete() -> None:
    """The regression this design exists for.

    Pydantic AI 2.35.3 reports the first `ToolCallPart` with a *partial*
    `args` string; the rest arrives as `ToolCallPartDelta`s. Emitting at
    `PartStartEvent` would hand korvid's harness `{"kind":` — invalid
    JSON — and the assembled call would then be dropped as a duplicate id.
    """
    model = FakeModel(
        ScriptedStream(
            [
                Chunk(tool_name="list_resources", args='{"kind":', tool_call_id="c1"),
                Chunk(args='"Pod"}'),
            ]
        )
    )
    events = await _events(model)
    calls = [event for event in events if event["type"] == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["id"] == "c1"
    assert calls[0]["name"] == "list_resources"
    assert json.loads(calls[0]["arguments"]) == {"kind": "Pod"}


async def test_tool_call_arguments_are_always_a_json_string() -> None:
    """A dict here would diverge from every other adapter and from the audit shape."""
    model = FakeModel(
        ScriptedStream([Chunk(tool_name="get_logs", args='{"pod":"web-1"}', tool_call_id="c2")])
    )
    call = next(event for event in await _events(model) if event["type"] == "tool_call")
    assert isinstance(call["arguments"], str)
    assert json.loads(call["arguments"]) == {"pod": "web-1"}


async def test_usage_is_emitted_only_when_both_counts_are_reported() -> None:
    """`RequestUsage` counts default to `0`, so `0` means "not reported"."""
    with_usage = FakeModel(
        ScriptedStream([Chunk(text="ok")], RequestUsage(input_tokens=12, output_tokens=5))
    )
    assert {"type": "usage", "input_tokens": 12, "output_tokens": 5} in await _events(with_usage)

    for empty in (RequestUsage(), RequestUsage(input_tokens=12), RequestUsage(output_tokens=5)):
        events = await _events(FakeModel(ScriptedStream([Chunk(text="ok")], empty)))
        assert not any(event["type"] == "usage" for event in events)


async def test_request_sent_is_not_emitted_when_the_transport_never_accepts() -> None:
    model = FakeModel(error=ConnectionError("no route to host"))
    provider = _provider(model)
    events: list[dict[str, Any]] = []

    async def _consume() -> None:
        async for event in provider.complete([{"role": "user", "content": "hi"}], []):
            events.append(event)

    with pytest.raises(ConnectionError, match="no route to host"):
        await _consume()
    assert events == []


async def test_cancelling_mid_request_abandons_it_without_emitting_anything() -> None:
    model = FakeModel(ScriptedStream([Chunk(text="x")]), enter_delay=5.0)
    provider = _provider(model)
    seen: list[dict[str, Any]] = []

    async def consume() -> None:
        async for event in provider.complete([{"role": "user", "content": "hi"}], []):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == []
    assert len(model.requests) == 1


async def test_cancelling_a_live_stream_closes_it_and_marks_it_interrupted() -> None:
    """`StreamedResponse.cancel()` is the library's own teardown path."""
    stream = ScriptedStream([Chunk(text="a"), Chunk(text="b")])
    iterator = stream.__aiter__()
    assert await iterator.__anext__() is not None
    await stream.cancel()
    assert stream.closed is True
    assert stream.get().state == "interrupted"


async def test_tools_are_passed_as_function_tools_and_nothing_is_added() -> None:
    model = FakeModel(ScriptedStream([Chunk(text="ok")]))
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_resources",
                "description": "List resources",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    await _events(model, tools)
    _, _, parameters = model.requests[0]
    assert [definition.name for definition in parameters.function_tools] == ["list_resources"]
    assert parameters.function_tools[0].parameters_json_schema == {
        "type": "object",
        "properties": {},
    }
    assert parameters.allow_text_output is True
    assert parameters.output_tools == []


async def test_capabilities_stay_unknown_without_direct_evidence() -> None:
    capabilities = _provider(FakeModel()).capabilities
    assert capabilities.context_window_tokens is None
    assert capabilities.supports_tools is None
    assert capabilities.supports_reasoning is None


async def test_descriptor_reports_the_configured_provider_and_model() -> None:
    provider = _provider(FakeModel())
    assert provider.descriptor.provider == "openai"
    assert provider.descriptor.model == "gpt-4o"


async def test_aclose_awaits_an_async_client_close() -> None:
    """`AsyncOpenAI`/`AsyncAnthropic` expose `close()` as a coroutine, not `aclose()`."""

    class _AsyncClient:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    client = _AsyncClient()
    await _provider(FakeModel(client=client)).aclose()
    assert client.closed is True


async def test_aclose_calls_a_synchronous_client_close() -> None:
    """Bedrock's botocore client and `google.genai.Client` close synchronously."""

    class _SyncClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    client = _SyncClient()
    await _provider(FakeModel(client=client)).aclose()
    assert client.closed is True


async def test_aclose_survives_a_client_that_cannot_close() -> None:
    class _Stubborn:
        async def close(self) -> None:
            raise RuntimeError("transport already gone")

    await _provider(FakeModel(client=_Stubborn())).aclose()
    assert _provider(FakeModel(client=object())) is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/providers/test_pydantic_model.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'korvid.providers.pydantic_model'`.

- [ ] **Step 3: Write the provider**

Create `src/korvid/providers/pydantic_model.py`:

```python
"""korvid's `LLMProvider` implemented over a Pydantic AI `Model`.

Everything above this boundary — the agentic loop, the tool harness, the
approval gate, the audit log, the outbound policy — is unchanged. Only
the bytes-on-the-wire step is delegated. Pydantic AI's agent, its own
tool execution and its retry machinery are deliberately unused: korvid
owns those decisions.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai.messages import (
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings

from korvid.agent.model_policy import ModelCapabilities, ModelDescriptor
from korvid.agent.provider import REQUEST_SENT, LLMProvider
from korvid.providers.pydantic_messages import to_model_messages, to_tool_definitions

logger = logging.getLogger(__name__)


def _tool_call_event(part: ToolCallPart) -> dict[str, Any]:
    """A korvid `tool_call` event: arguments are always a JSON string.

    The harness parses this string; emitting a dict here would diverge
    from every other adapter and from the audit record's shape.
    """
    arguments = part.args
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {})
    return {
        "type": "tool_call",
        "id": part.tool_call_id or "",
        "name": part.tool_name,
        "arguments": arguments,
    }


class PydanticModelProvider(LLMProvider):
    """Stream completions from a Pydantic AI `Model` as korvid events."""

    def __init__(
        self,
        model: Model,
        descriptor: ModelDescriptor,
        *,
        capabilities: ModelCapabilities | None = None,
        model_settings: ModelSettings | None = None,
    ) -> None:
        self._model = model
        self._descriptor = descriptor
        # Capabilities are only ever facts the *configuration* asserted;
        # nothing here is inferred from the provider or model name.
        self._capabilities = capabilities if capabilities is not None else ModelCapabilities.unknown()
        # `ModelSettings` is a `total=False` TypedDict, so it is stored
        # and forwarded as one: copying it into a `dict[str, object]`
        # would force a `cast` at the `request_stream` call and lose the
        # key-level typing that makes an unsupported key a mypy error.
        # An empty mapping becomes None so Pydantic AI applies the
        # model's own defaults instead of an empty override.
        self._model_settings: ModelSettings | None = model_settings or None

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    @property
    def model(self) -> Model:
        """The underlying Pydantic AI model.

        Read-only, and the seam wire-level tests drive: asserting which
        header and URL a profile produces requires sending a real request
        through the model korvid actually built, not a reconstruction.
        """
        return self._model

    @property
    def model_settings(self) -> ModelSettings | None:
        """The settings forwarded on every request, or None for defaults."""
        return self._model_settings

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        converted = to_model_messages(messages)
        parameters = ModelRequestParameters(
            function_tools=to_tool_definitions(tools),
            allow_text_output=True,
        )
        emitted_text = False
        async with self._model.request_stream(
            converted,
            self._model_settings,
            parameters,
        ) as response:
            # Only now is the request demonstrably on the wire: the
            # context manager entered, so the transport accepted it.
            yield {"type": REQUEST_SENT}
            async for event in response:
                text = _streamed_text(event)
                if text:
                    emitted_text = True
                    yield {"type": "text_delta", "text": text}
            # Tool calls are read from the *assembled* parts, never from
            # `PartStartEvent`: 2.35.3 starts a tool part as soon as the
            # first bytes of `args` arrive, so a streamed call's start
            # event routinely carries a truncated JSON fragment.
            final = response.get()
            for part in final.parts:
                if isinstance(part, ToolCallPart):
                    yield _tool_call_event(part)
                elif isinstance(part, TextPart) and not emitted_text and part.content:
                    # A model that answered in one shot emits no deltas.
                    yield {"type": "text_delta", "text": part.content}
            usage = response.usage
            # `RequestUsage` counts are ints defaulting to 0, so an
            # unreported count is indistinguishable from a real zero.
            # korvid's contract is "both counts or no event", and a
            # `usage 0/0` line would be a lie about a real request.
            if usage.input_tokens and usage.output_tokens:
                yield {
                    "type": "usage",
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                }
        yield {"type": "done"}

    async def aclose(self) -> None:
        """Release provider-owned transport resources.

        Pydantic AI models own their SDK client. The close call is not
        uniform: `AsyncOpenAI` and `AsyncAnthropic` expose an awaitable
        `close()`, `google.genai.Client` and Bedrock's botocore client a
        synchronous one, and a model may expose no client at all — so the
        result is awaited only when it is awaitable.
        """
        client = getattr(self._model, "client", None)
        closer = getattr(client, "aclose", None) or getattr(client, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception:  # closing must never mask the real error
            logger.debug("closing the model transport failed", exc_info=True)


def _streamed_text(event: object) -> str:
    """The text a stream event contributes, or `""` for a non-text event."""
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    return ""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/providers/test_pydantic_model.py -q -rs`
Expected: `14 passed`, `0 skipped`. This module is behind
`pytest.importorskip("pydantic_ai")`, and the scripted stream it drives is the only
executable evidence in the plan for the streaming contract — fragmented tool-call
assembly, text deltas, the usage record, no `request_sent` when the transport refuses,
and cancellation. `14 skipped` reads as a green run in the summary line and proves
nothing, so treat a skip here as a failure: it means Task 11's `[agent]` extra is not
installed in this environment, and Step 5's mutation check below cannot be trusted
either (a skipped test cannot fail when you break the code it covers).

- [ ] **Step 5: Prove the fragmented-tool-call test is load-bearing (mutation)**

The whole point of reading tool calls from the assembled parts is invisible
unless the test fails when that decision is reversed. Temporarily replace the
tool-call emission in `complete` with the naive version:

```python
            async for event in response:
                if isinstance(event, PartStartEvent) and isinstance(event.part, ToolCallPart):
                    yield _tool_call_event(event.part)  # MUTATION — do not keep
```

Run: `uv run pytest -p no:tach tests/providers/test_pydantic_model.py -q -k split_across_chunks`
Expected: FAIL — `json.decoder.JSONDecodeError: Expecting value` on `'{"kind":'`
(the truncated fragment `PartStartEvent` carried). Revert the mutation and
re-run the file: PASS.

- [ ] **Step 6: Lint, typecheck, layer check and commit**

```bash
uv run ruff check --fix src/korvid/providers/pydantic_model.py tests/providers/test_pydantic_model.py
uv run ruff format src/korvid/providers/pydantic_model.py tests/providers/test_pydantic_model.py
uv run mypy src/korvid/providers/pydantic_model.py
uv run tach check
git add src/korvid/providers/pydantic_model.py tests/providers/test_pydantic_model.py
git commit -m "feat: stream completions through the pydantic-ai model layer" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 14: Profile-driven model construction

**Files:**
- Create: `src/korvid/providers/pydantic_factory.py`
- Modify: `src/korvid/providers/entra.py` (add the Entra token accessor Azure needs)
- Modify: `src/korvid/providers/registry.py`
- Create: `tests/providers/test_pydantic_factory.py`
- Modify: `tests/providers/test_entra.py`

**Interfaces:**
- Consumes: `AgentProfileConfig`, `adapter_id`, `model_tag` (Task 5); `PydanticModelProvider` (Task 13); `ADAPTER_EXTRAS`/`install_hint` (Task 6); `CredentialSource` from `korvid.agent.credentials`, `StaticHeaderSource` from `korvid.providers.static_creds`, `EntraCredentialSource` from `korvid.providers.entra`, `TokenStore` from `korvid.providers.token_store`, `build_verify` from `korvid.providers.net`; `ProviderPluginRegistry`, `ProviderPluginConfig`.
- Produces:
  - `AdapterExtraMissing(Exception)` with `install_hint: str`
  - `AdapterNotConnected(Exception)` — an adapter whose transport this branch has not landed yet
  - `build_model(profile: AgentProfileConfig, *, ca_bundle: str | None = None, oauth_token: str | None = None, http_client: httpx2.AsyncClient | None = None) -> Model`
  - `build_model_provider(profile: AgentProfileConfig, *, ca_bundle: str | None = None, oauth_token: str | None = None, http_client: httpx2.AsyncClient | None = None) -> PydanticModelProvider`
  - `resolve_credential(profile: AgentProfileConfig) -> CredentialSource | None`
  - `EntraCredentialSource.access_token(self) -> str` (async) — the bare token an Azure `azure_ad_token_provider` must return
  - `create_profile_provider(profile: AgentProfileConfig, *, oauth_token: str | None = None, ca_bundle: str | None = None, plugin_registry: ProviderPluginRegistry | None = None) -> LLMProvider | None` in `registry.py`

The `http_client` parameter is the wire-test seam. It exists in production code on
purpose: which header carries a credential and which URL it goes to are security
invariants, and the only honest way to assert them is to look at the bytes. Tests pass an
`httpx2.AsyncClient(transport=httpx2.MockTransport(...))` and read the captured
`httpx2.Request`. **No test in this plan touches a private SDK attribute**
(`AsyncAzureOpenAI._build_request`, `._prepare_options`, `._api_version`, or the
`openai` base client's `auth_headers`): those are unversioned internals that can change in
a patch release, and a security assertion built on them can start passing for the wrong
reason.

**Azure is built with Pydantic AI's own Azure provider, over an explicitly constructed
`AsyncAzureOpenAI`.** Both auth paths construct the client directly and hand it to
`AzureProvider(openai_client=...)`, because only the client constructor accepts
`azure_deployment` — the deployment name Task 2's migration extracted out of the legacy
`base_url`. Verified on the wire with `httpx2.MockTransport` against openai 3.5.0 and
pydantic-ai-slim 2.35.3:

| Construction | Request URL | Auth header |
|---|---|---|
| `AsyncAzureOpenAI(azure_endpoint="https://example.openai.azure.com", azure_deployment="my-dep", api_version="2024-10-21", api_key="sk-azure")` | `https://example.openai.azure.com/openai/deployments/my-dep/chat/completions?api-version=2024-10-21` | `api-key` only |
| the same with `azure_ad_token_provider=<async () -> str>` instead of `api_key` | identical | `authorization` only, `Bearer <token>` |

Routing Azure through `OpenAIProvider` would send a bearer token in every case and break
key-based deployments, which is exactly what this adapter exists to prevent.

- [ ] **Step 1: Write the failing factory tests**

Create `tests/providers/test_pydantic_factory.py`:

```python
"""Profile → Pydantic AI model construction and credential resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic_ai")

import httpx2  # noqa: E402  # after importorskip
from openai import AsyncAzureOpenAI  # noqa: E402  # after importorskip
from pydantic_ai.messages import ModelRequest, UserPromptPart  # noqa: E402  # after importorskip
from pydantic_ai.models import Model, ModelRequestParameters  # noqa: E402  # after importorskip
from pydantic_ai.settings import ModelSettings  # noqa: E402  # after importorskip

from korvid.agent.model_profiles import (  # noqa: E402  # after importorskip
    AgentAuthConfig,
    AgentProfileConfig,
)
from korvid.providers.adapter_extras import (  # noqa: E402  # after importorskip
    KEYLESS_API_KEY_SENTINEL,
)
from korvid.providers.pydantic_factory import (  # noqa: E402  # after importorskip
    AdapterExtraMissing,
    AdapterNotConnected,
    build_model,
    build_model_provider,
    resolve_credential,
)

_CHAT_COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "test",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class WireCapture:
    """Records the requests a model actually puts on the wire.

    Uses `httpx2.MockTransport`, the SDK's own public transport seam, so
    the assertions below survive an SDK patch release that renames a
    private helper.
    """

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def client(self) -> httpx2.AsyncClient:
        def _handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return httpx2.Response(200, json=_CHAT_COMPLETION)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(_handle))

    @property
    def last(self) -> httpx2.Request:
        assert self.requests, "no request reached the transport"  # noqa: S101  # test helper
        return self.requests[-1]

    def auth_header_names(self) -> list[str]:
        return sorted(
            name.lower()
            for name in self.last.headers
            if name.lower() in {"api-key", "authorization"}
        )

    def body(self) -> dict[str, Any]:
        decoded: dict[str, Any] = json.loads(self.last.content)
        return decoded


async def _drive(model: Model, settings: ModelSettings | None = None) -> None:
    """Send one request through Pydantic AI so the transport sees real bytes.

    `Model.request(messages, model_settings, model_request_parameters)` is
    the public entry point every adapter implements, so the same helper
    drives OpenAI, Azure and Ollama without reaching for an SDK-specific
    client attribute.
    """
    await model.request(
        [ModelRequest(parts=[UserPromptPart(content="hi")])],
        settings,
        ModelRequestParameters(),
    )


def test_a_missing_provider_extra_raises_an_actionable_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "korvid.providers.pydantic_factory._adapter_installed", lambda adapter: False
    )
    profile = AgentProfileConfig(
        model="anthropic:claude-sonnet-4-5",
        auth=AgentAuthConfig(method="environment", settings={"key": "ANTHROPIC_API_KEY"}),
    )
    with pytest.raises(AdapterExtraMissing, match="provider-anthropic") as excinfo:
        build_model_provider(profile)
    assert "provider-anthropic" in excinfo.value.install_hint


def test_a_missing_extra_never_falls_back_to_another_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "korvid.providers.pydantic_factory._adapter_installed", lambda adapter: False
    )
    profile = AgentProfileConfig(model="google:gemini-2.5-pro")
    with pytest.raises(AdapterExtraMissing, match="provider-google"):
        build_model_provider(profile)


def test_an_unknown_adapter_is_rejected() -> None:
    with pytest.raises(AdapterExtraMissing, match="unknown model adapter"):
        build_model_provider(AgentProfileConfig(model="nope:v1"))


def test_environment_auth_resolves_to_an_env_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORVID_TEST_KEY", "sk-test")
    profile = AgentProfileConfig(
        model="openai:gpt-4o",
        endpoint="https://api.openai.com/v1",
        auth=AgentAuthConfig(method="environment", settings={"key": "KORVID_TEST_KEY"}),
    )
    credential = resolve_credential(profile)
    assert credential is not None


async def test_environment_auth_renders_a_bearer_header_for_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORVID_TEST_KEY", "sk-test")
    profile = AgentProfileConfig(
        model="openai:gpt-4o",
        auth=AgentAuthConfig(method="environment", settings={"key": "KORVID_TEST_KEY"}),
    )
    credential = resolve_credential(profile)
    assert credential is not None
    assert await credential.headers() == {"Authorization": "Bearer sk-test"}


def test_keyring_auth_reads_the_named_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """`keyring` means: the *secret itself* lives in the OS keyring.

    `auth.settings.key` is the keyring **entry name**, not a value and not
    an environment variable name. korvid reads it through the same
    `TokenStore` it uses for the Copilot OAuth token, under korvid's own
    service name, so a plugin never sees the raw keyring API.
    """
    monkeypatch.setattr(
        "korvid.providers.pydantic_factory.TokenStore.load",
        lambda self, key: "sk-keyring" if key == "korvid-openai" else None,
    )
    profile = AgentProfileConfig(
        model="openai:gpt-4o",
        auth=AgentAuthConfig(method="keyring", settings={"key": "korvid-openai"}),
    )
    assert resolve_credential(profile) is not None


async def test_a_keyring_secret_is_what_reaches_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "korvid.providers.pydantic_factory.TokenStore.load",
        lambda self, key: "sk-keyring" if key == "korvid-openai" else None,
    )
    capture = WireCapture()
    profile = AgentProfileConfig(
        model="openai:gpt-4o",
        endpoint="https://llm.internal.example/v1",
        auth=AgentAuthConfig(method="keyring", settings={"key": "korvid-openai"}),
    )
    await _drive(build_model(profile, http_client=capture.client()))
    assert capture.last.headers["authorization"] == "Bearer sk-keyring"


def test_a_missing_keyring_entry_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korvid.providers.pydantic_factory.TokenStore.load", lambda self, key: None
    )
    profile = AgentProfileConfig(
        model="openai:gpt-4o",
        auth=AgentAuthConfig(method="keyring", settings={"key": "absent"}),
    )
    with pytest.raises(ValueError, match="no keyring entry named 'absent'"):
        resolve_credential(profile)


def test_no_auth_resolves_to_no_credential() -> None:
    profile = AgentProfileConfig(model="ollama:llama3", endpoint="http://localhost:11434")
    assert resolve_credential(profile) is None


def test_an_unsupported_auth_method_is_rejected() -> None:
    profile = AgentProfileConfig(
        model="openai:gpt-4o", auth=AgentAuthConfig(method="mystery", settings={})
    )
    with pytest.raises(ValueError, match="unsupported auth method"):
        resolve_credential(profile)


def test_the_descriptor_reports_the_profile_adapter_and_model_tag() -> None:
    profile = AgentProfileConfig(
        model="openai:gpt-4o-mini",
        endpoint="http://localhost:8000/v1",
        auth=AgentAuthConfig(method="none"),
    )
    provider = build_model_provider(profile)
    assert provider.descriptor.provider == "openai"
    assert provider.descriptor.model == "gpt-4o-mini"


def test_an_ollama_endpoint_gets_the_openai_compatible_suffix() -> None:
    profile = AgentProfileConfig(model="ollama:llama3", endpoint="http://localhost:11434")
    model = build_model(profile)
    assert str(model.base_url).rstrip("/") == "http://localhost:11434/v1"


async def test_a_keyless_custom_endpoint_sends_the_explicit_sentinel() -> None:
    """Never `api_key=None`: the OpenAI SDK would read `OPENAI_API_KEY`.

    A self-hosted vLLM or Ollama server needs no credential, but leaving
    the key unset makes the SDK silently attach whatever real OpenAI key
    happens to be exported in the operator's shell.
    """
    capture = WireCapture()
    profile = AgentProfileConfig(
        model="openai:local-model",
        endpoint="http://localhost:8000/v1",
        auth=AgentAuthConfig(method="none"),
    )
    await _drive(build_model(profile, http_client=capture.client()))
    assert capture.last.headers["authorization"] == f"Bearer {KEYLESS_API_KEY_SENTINEL}"


async def test_an_ambient_openai_key_never_reaches_a_keyless_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-user-key")
    capture = WireCapture()
    profile = AgentProfileConfig(
        model="openai:local-model",
        endpoint="http://localhost:8000/v1",
        auth=AgentAuthConfig(method="none"),
    )
    await _drive(build_model(profile, http_client=capture.client()))
    assert "sk-real-user-key" not in capture.last.headers["authorization"]


@pytest.mark.parametrize("endpoint", [None, "https://api.openai.com/v1"])
def test_openai_proper_is_never_sentinel_authenticated(endpoint: str | None) -> None:
    """The sentinel is for keyless self-hosted endpoints, never for OpenAI."""
    profile = AgentProfileConfig(
        model="openai:gpt-4o", endpoint=endpoint, auth=AgentAuthConfig(method="none")
    )
    with pytest.raises(ValueError, match="requires an API key"):
        build_model(profile)


@pytest.mark.parametrize("adapter", ["anthropic", "google"])
def test_a_vendor_adapter_without_a_credential_is_refused(adapter: str) -> None:
    """`api_key=None` would let the SDK read its own vendor env var."""
    profile = AgentProfileConfig(
        model=f"{adapter}:some-model", auth=AgentAuthConfig(method="none")
    )
    with pytest.raises(ValueError, match="requires an API key"):
        build_model(profile)


def test_bedrock_builds_on_the_aws_credential_chain_alone() -> None:
    """Bedrock's only offered auth method *is* the AWS credential chain.

    Using it is therefore the operator's explicit choice rather than an
    ambient fallback korvid slipped in, which is why this is the one
    adapter built without an explicit key argument. `network.ca_bundle`
    is deliberately not applied either: botocore takes its trust from
    `AWS_CA_BUNDLE`, so passing korvid's httpx client would be ignored
    rather than honoured.
    """
    profile = AgentProfileConfig(
        model="bedrock:anthropic.claude-3-5-sonnet-20241022-v2:0",
        auth=AgentAuthConfig(method="provider-default"),
        options={"region_name": "us-east-1"},
    )
    provider = build_model_provider(profile)
    assert provider.descriptor.provider == "bedrock"
    # `model_tag` partitions on the first colon, so a Bedrock model id
    # that contains its own `:0` version suffix survives intact.
    assert provider.descriptor.model == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert str(provider.model.base_url) == "https://bedrock-runtime.us-east-1.amazonaws.com"


def test_bedrock_without_a_region_names_the_setting_to_fix() -> None:
    """The SDK's own error does not mention korvid's option name."""
    profile = AgentProfileConfig(
        model="bedrock:anthropic.claude-3-5-sonnet-20241022-v2:0",
        auth=AgentAuthConfig(method="provider-default"),
    )
    with pytest.raises(ValueError, match=r"options\.region_name"):
        build_model(profile)


async def test_azure_with_an_api_key_sends_the_raw_api_key_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this adapter exists to prevent.

    An `openai:` profile would send `Authorization: Bearer ...`, which an
    Azure OpenAI deployment rejects.
    """
    monkeypatch.setenv("KORVID_AZURE_KEY", "sk-azure")
    capture = WireCapture()
    profile = AgentProfileConfig(
        model="azure:gpt-4o",
        endpoint="https://example.openai.azure.com",
        auth=AgentAuthConfig(method="environment", settings={"key": "KORVID_AZURE_KEY"}),
        options={"api_version": "2024-10-21"},
    )
    model = build_model(profile, http_client=capture.client())
    assert model.system == "azure"
    assert isinstance(model.client, AsyncAzureOpenAI)
    await _drive(model)
    assert capture.auth_header_names() == ["api-key"]
    assert capture.last.headers["api-key"] == "sk-azure"


async def test_azure_with_provider_default_sends_an_entra_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _token(self: Any) -> str:
        return "entra-token"

    monkeypatch.setattr("korvid.providers.entra.EntraCredentialSource.access_token", _token)
    capture = WireCapture()
    profile = AgentProfileConfig(
        model="azure:gpt-4o",
        endpoint="https://example.openai.azure.com",
        auth=AgentAuthConfig(method="provider-default"),
        options={"api_version": "2024-10-21"},
    )
    await _drive(build_model(profile, http_client=capture.client()))
    assert capture.auth_header_names() == ["authorization"]
    assert capture.last.headers["authorization"] == "Bearer entra-token"


async def test_azure_targets_the_migrated_deployment_not_the_model_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 2 moved the deployment out of `base_url` into this option."""
    monkeypatch.setenv("KORVID_AZURE_KEY", "sk-azure")
    capture = WireCapture()
    profile = AgentProfileConfig(
        model="azure:gpt-4o",
        endpoint="https://example.openai.azure.com",
        auth=AgentAuthConfig(method="environment", settings={"key": "KORVID_AZURE_KEY"}),
        options={"api_version": "2024-10-21", "azure_deployment": "my-dep"},
    )
    await _drive(build_model(profile, http_client=capture.client()))
    assert str(capture.last.url) == (
        "https://example.openai.azure.com/openai/deployments/my-dep"
        "/chat/completions?api-version=2024-10-21"
    )


async def test_azure_falls_back_to_the_model_tag_as_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORVID_AZURE_KEY", "sk-azure")
    capture = WireCapture()
    profile = AgentProfileConfig(
        model="azure:gpt-4o",
        endpoint="https://example.openai.azure.com",
        auth=AgentAuthConfig(method="environment", settings={"key": "KORVID_AZURE_KEY"}),
    )
    await _drive(build_model(profile, http_client=capture.client()))
    assert "/openai/deployments/gpt-4o/chat/completions" in str(capture.last.url)


@pytest.mark.parametrize("method", ["none", "device-login"])
def test_azure_refuses_an_auth_method_it_cannot_perform(method: str) -> None:
    """An Azure OpenAI deployment always authenticates; `none` is not a mode."""
    profile = AgentProfileConfig(
        model="azure:gpt-4o",
        endpoint="https://example.openai.azure.com",
        auth=AgentAuthConfig(method=method),
    )
    with pytest.raises(ValueError, match="azure profiles require"):
        build_model(profile)


def test_azure_without_an_endpoint_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the SDK silently pick up `AZURE_OPENAI_ENDPOINT` from the environment."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://leaked.openai.azure.com")
    monkeypatch.setenv("KORVID_AZURE_KEY", "sk-azure")
    profile = AgentProfileConfig(
        model="azure:gpt-4o",
        auth=AgentAuthConfig(method="environment", settings={"key": "KORVID_AZURE_KEY"}),
    )
    with pytest.raises(ValueError, match="azure profiles require an endpoint"):
        build_model(profile)


async def test_azure_uses_the_default_api_version_when_none_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_VERSION", raising=False)
    monkeypatch.setenv("KORVID_AZURE_KEY", "sk-azure")
    capture = WireCapture()
    profile = AgentProfileConfig(
        model="azure:gpt-4o",
        endpoint="https://example.openai.azure.com",
        auth=AgentAuthConfig(method="environment", settings={"key": "KORVID_AZURE_KEY"}),
    )
    await _drive(build_model(profile, http_client=capture.client()))
    assert capture.last.url.params["api-version"] == "2024-10-21"


async def test_every_legacy_ollama_knob_reaches_the_request_body() -> None:
    """No operator loses a setting `agent.ollama` used to provide.

    `temperature` and `seed` are `ModelSettings` keys; `num_ctx`,
    `num_predict`, `think` and `keep_alive` have no `ModelSettings`
    equivalent and travel in `extra_body`, which Pydantic AI merges into
    the request JSON verbatim. `num_predict` goes inside Ollama's native
    `options` bag rather than through `max_tokens`, because the OpenAI
    model serializes `max_tokens` as `max_completion_tokens`.
    """
    capture = WireCapture()
    profile = AgentProfileConfig(
        model="ollama:llama3",
        endpoint="http://localhost:11434",
        options={
            "num_ctx": 16384,
            "num_predict": 512,
            "temperature": 0.2,
            "seed": 7,
            "think": True,
            "keep_alive": "5m",
        },
    )
    provider = build_model_provider(profile, http_client=capture.client())
    await _drive(provider.model, provider.model_settings)
    body = capture.body()
    assert body["temperature"] == 0.2
    assert body["seed"] == 7
    assert body["options"] == {"num_ctx": 16384, "num_predict": 512}
    assert body["think"] is True
    assert body["keep_alive"] == "5m"


async def test_a_keyless_ollama_endpoint_uses_the_sentinel() -> None:
    capture = WireCapture()
    profile = AgentProfileConfig(model="ollama:llama3", endpoint="http://localhost:11434")
    await _drive(build_model(profile, http_client=capture.client()))
    assert capture.last.headers["authorization"] == f"Bearer {KEYLESS_API_KEY_SENTINEL}"


def test_github_copilot_is_explicitly_disabled_until_task_16() -> None:
    """Deleted in Task 16, which lands the real token-exchange client.

    The interim alternative — an `OpenAIProvider` holding the *OAuth*
    token — would send the wrong bearer to the Copilot API on every
    request. A visible refusal is recoverable; a wrong credential looks
    like an expired login.
    """
    profile = AgentProfileConfig(
        model="github-copilot:gpt-4o", auth=AgentAuthConfig(method="device-login")
    )
    with pytest.raises(AdapterNotConnected, match="github-copilot"):
        build_model(profile)


def test_a_declared_context_window_option_becomes_a_known_capability() -> None:
    profile = AgentProfileConfig(
        model="ollama:llama3",
        endpoint="http://localhost:11434",
        options={"num_ctx": 16384},
    )
    provider = build_model_provider(profile)
    assert provider.capabilities.context_window_tokens == 16384


def test_capabilities_are_unknown_without_a_declared_option() -> None:
    profile = AgentProfileConfig(model="openai:gpt-4o", endpoint="http://localhost:8000/v1")
    provider = build_model_provider(profile)
    assert provider.capabilities.context_window_tokens is None
    assert provider.capabilities.supports_tools is None


def test_a_configured_ca_bundle_is_carried_into_the_transport(tmp_path: Path) -> None:
    """`network.ca_bundle` must reach the SDK's HTTP client, not be dropped."""
    bogus = tmp_path / "ca.pem"
    bogus.write_text("not a certificate", encoding="utf-8")
    profile = AgentProfileConfig(model="openai:gpt-4o", endpoint="http://localhost:8000/v1")
    with pytest.raises(ValueError, match="could not be loaded"):
        build_model(profile, ca_bundle=str(bogus))
```

Append to `tests/providers/test_entra.py`:

```python
async def test_access_token_returns_the_bare_token_for_azure() -> None:
    """Azure's `azure_ad_token_provider` wants the token, not a header."""
    source = EntraCredentialSource(credential=_FakeCredential(token="abc", expires_on=9_999_999_999))
    assert await source.access_token() == "abc"
    assert await source.headers() == {"Authorization": "Bearer abc"}
```

(`_FakeCredential` is the existing fake in that file; reuse it rather than adding another.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/providers/test_pydantic_factory.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'korvid.providers.pydantic_factory'`.

- [ ] **Step 3: Add the Entra token accessor**

`EntraCredentialSource` already refreshes and caches a token behind a lock; Azure needs
the same token *without* the `Authorization:` wrapper. Extract the refresh so both
callers share one code path — in `src/korvid/providers/entra.py`:

```python
    async def access_token(self) -> str:
        """The current Entra access token, refreshed if it is near expiry.

        Azure OpenAI's `azure_ad_token_provider` takes a bare token, while
        korvid's own header sources take a rendered header; both must come
        from the same cache and the same refresh lock, or a rebuild would
        double the token calls.
        """
        if self._needs_refresh():
            async with self._refresh_lock:
                if self._needs_refresh():
                    access = await self._get_credential().get_token(ENTRA_SCOPE)
                    self._token = str(access.token)
                    self._expires_on = float(access.expires_on)
        return self._token or ""

    async def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.access_token()}"}
```

- [ ] **Step 4: Write the factory**

Create `src/korvid/providers/pydantic_factory.py`:

```python
"""Build a Pydantic AI model (and korvid provider) from a profile.

Adapter selection is table-driven: a missing optional extra produces an
actionable install hint and stops. korvid never silently substitutes a
different adapter — a profile that names `anthropic` either talks to
Anthropic or fails loudly, and a profile that names `azure` never
degrades into a generic OpenAI bearer-token connection.

No SDK client is ever constructed with an implicit credential. Every
`api_key` argument is passed explicitly, because every one of these SDKs
falls back to its own environment variable when the argument is None —
which would let an unrelated key in the operator's shell reach an
endpoint the profile never authorised.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Final
from urllib.parse import urlsplit

import httpx2
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from korvid.agent.credentials import CredentialSource
from korvid.agent.model_policy import (
    CapabilitySource,
    ModelCapabilities,
    ModelDescriptor,
)
from korvid.agent.model_profiles import AgentProfileConfig, adapter_id, model_tag
from korvid.providers.adapter_extras import (
    ADAPTER_EXTRAS,
    KEYLESS_API_KEY_SENTINEL,
    adapter_available,
    install_hint,
)
from korvid.providers.net import build_verify
from korvid.providers.pydantic_model import PydanticModelProvider
from korvid.providers.static_creds import StaticHeaderSource
from korvid.providers.token_store import TokenStore

#: Azure's data-plane API version when a profile does not pin one. Not
#: read from the environment: an operator who did not choose a version in
#: `:ai` should get a known-good one, not whatever `OPENAI_API_VERSION`
#: happens to hold in this shell.
DEFAULT_AZURE_API_VERSION: Final[str] = "2024-10-21"

_HTTP_TIMEOUT_S: Final[float] = 120.0

#: Hosts that are OpenAI proper. A profile pointing here must carry a
#: real credential: the keyless sentinel exists for self-hosted
#: OpenAI-compatible servers, and sending it to OpenAI would produce a
#: confusing 401 instead of an actionable configuration error.
_OPENAI_VENDOR_HOSTS: Final[frozenset[str]] = frozenset({"api.openai.com"})

#: Adapters whose Pydantic AI transport lands in a later task on this
#: branch. Constructing an approximate client for one of these would send
#: the wrong credential to a real endpoint, so they refuse instead.
#: `github-copilot` is removed from this set in Task 16.
_NOT_CONNECTED_YET: Final[frozenset[str]] = frozenset({"github-copilot"})


class AdapterExtraMissing(Exception):
    """Raised when a profile names an adapter korvid cannot construct."""

    def __init__(self, message: str, install_hint: str = "") -> None:
        super().__init__(message)
        self.install_hint = install_hint


class AdapterNotConnected(Exception):
    """Raised when an adapter's transport has not landed yet on this branch."""


def _adapter_installed(adapter: str) -> bool:
    """Whether the extra backing *adapter* is installed (test seam).

    Delegates to the shared table so the wizard's availability answer and
    the factory's refusal can never disagree.
    """
    return adapter_available(adapter)


def _secret(profile: AgentProfileConfig) -> str:
    """The API key *profile* refers to, or `""` when it refers to none.

    Both reference kinds resolve here — an environment variable name and a
    keyring entry name — so every adapter reads a key the same way.

    `keyring` means the secret itself is stored in the OS keyring under
    korvid's own service name; `auth.settings.key` is the **entry name**,
    never a value and never an environment variable name.

    Raises:
        ValueError: When the reference is malformed or resolves to nothing.
    """
    method = profile.auth.method
    if method not in ("environment", "keyring"):
        return ""
    key = profile.auth.settings.get("key")
    if not isinstance(key, str) or not key:
        raise ValueError(f"{method} auth requires a name in auth.settings.key")
    if method == "environment":
        value = os.environ.get(key, "")
        if not value:
            raise ValueError(f"environment variable {key!r} is unset or empty")
        return value
    token = TokenStore().load(key)
    if not token:
        raise ValueError(f"no keyring entry named {key!r}")
    return token


def resolve_credential(profile: AgentProfileConfig) -> CredentialSource | None:
    """The credential source for *profile*, or None when it needs none.

    Only *references* are read from config: an environment variable name
    or a keyring entry name. A secret value never appears in a profile.
    This is the source handed to *plugins*; built-in adapters let their
    SDK client carry the credential instead.

    Raises:
        ValueError: On an auth method korvid does not implement.
    """
    method = profile.auth.method
    if method in ("none", "provider-default", "device-login"):
        # `provider-default` and `device-login` are handled by the SDK or
        # the adapter's own auth hook, not by a korvid header source.
        return None
    if method in ("environment", "keyring"):
        return StaticHeaderSource(_secret(profile))
    raise ValueError(f"unsupported auth method: {method!r}")


def _http_client(
    ca_bundle: str | None, override: httpx2.AsyncClient | None
) -> httpx2.AsyncClient | None:
    """An SDK HTTP client honouring `network.ca_bundle`, or None for defaults.

    `httpx2` is the client flavour the OpenAI/Anthropic SDKs and Pydantic
    AI expect; handing them a legacy `httpx.AsyncClient` raises
    `PydanticAIDeprecationWarning`, which this repo treats as an error.

    *override* is the wire-test seam described in this task's Interfaces
    section; production callers never pass it.

    Raises:
        ValueError: When the configured bundle cannot be loaded (raised by
            `build_verify`, naming the path).
    """
    if override is not None:
        return override
    if ca_bundle is None:
        return None
    return httpx2.AsyncClient(verify=build_verify(ca_bundle), timeout=_HTTP_TIMEOUT_S)


def _is_vendor_openai(endpoint: str | None) -> bool:
    """Whether *endpoint* is OpenAI proper (or absent, which defaults to it)."""
    if not endpoint:
        return True
    return (urlsplit(endpoint).hostname or "").lower() in _OPENAI_VENDOR_HOSTS


def _openai_api_key(profile: AgentProfileConfig) -> str:
    """The key an OpenAI-compatible client must be constructed with.

    Never returns None: `OpenAIProvider(api_key=None)` reads
    `OPENAI_API_KEY` from the ambient environment, which would attach an
    unrelated real credential to a self-hosted endpoint.

    Raises:
        ValueError: When a profile that talks to OpenAI proper declares no
            credential. The keyless sentinel is only for custom endpoints.
    """
    secret = _secret(profile)
    if secret:
        return secret
    if _is_vendor_openai(profile.endpoint):
        raise ValueError(
            "an openai profile without a custom endpoint requires an API key; "
            "set auth.method to environment or keyring"
        )
    return KEYLESS_API_KEY_SENTINEL


def _vendor_api_key(profile: AgentProfileConfig, adapter: str) -> str:
    """The key a single-vendor SDK must be constructed with.

    Raises:
        ValueError: When the profile declares no credential. Passing None
            would let the SDK read `ANTHROPIC_API_KEY`/`GEMINI_API_KEY`.
    """
    secret = _secret(profile)
    if not secret:
        raise ValueError(
            f"a {adapter} profile requires an API key; auth.method 'none' would let the "
            f"{adapter} SDK read its own environment variable instead"
        )
    return secret


def _build_openai_model(
    profile: AgentProfileConfig,
    base_url: str | None,
    ca_bundle: str | None,
    api_key: str,
    http_client: httpx2.AsyncClient | None,
) -> Model:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(
        base_url=base_url,
        api_key=api_key,
        http_client=_http_client(ca_bundle, http_client),
    )
    return OpenAIChatModel(model_tag(profile.model), provider=provider)


def _build_ollama_model(
    profile: AgentProfileConfig, ca_bundle: str | None, http_client: httpx2.AsyncClient | None
) -> Model:
    """Ollama through Pydantic AI's own OpenAI-compatible Ollama provider."""
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.ollama import OllamaProvider

    from korvid.providers.ollama import normalize_base_url

    base = normalize_base_url(profile.endpoint) if profile.endpoint else None
    provider = OllamaProvider(
        base_url=f"{base}/v1" if base else None,
        api_key=_secret(profile) or KEYLESS_API_KEY_SENTINEL,
        http_client=_http_client(ca_bundle, http_client),
    )
    return OpenAIChatModel(model_tag(profile.model), provider=provider)


def _azure_api_version(profile: AgentProfileConfig) -> str:
    configured = profile.options.get("api_version")
    return configured if isinstance(configured, str) and configured else DEFAULT_AZURE_API_VERSION


def _azure_deployment(profile: AgentProfileConfig) -> str:
    """The deployment to call: the migrated option, else the model tag.

    Task 2 moves a legacy deployment-scoped `base_url` into
    `options.azure_deployment`; a profile written by the wizard sets it
    directly. Falling back to the model tag matches what `AzureProvider`
    would have done on its own.
    """
    declared = profile.options.get("azure_deployment")
    if isinstance(declared, str) and declared:
        return declared
    return model_tag(profile.model)


def _build_azure_model(
    profile: AgentProfileConfig, ca_bundle: str | None, http_client: httpx2.AsyncClient | None
) -> Model:
    """Azure OpenAI, with Azure's own credential handling preserved.

    An API key travels in the `api-key` header (never `Authorization`),
    and `provider-default` means an Entra ID token from
    `DefaultAzureCredential`, supplied as `azure_ad_token_provider` so the
    SDK refreshes it on its own schedule. Both paths construct
    `AsyncAzureOpenAI` explicitly, because only its constructor accepts
    `azure_deployment`.

    Raises:
        ValueError: When the profile has no endpoint, or names an auth
            method an Azure OpenAI deployment cannot perform. korvid will
            not fall back to `AZURE_OPENAI_ENDPOINT`: the profile is the
            source of truth for which resource the agent talks to.
    """
    from openai import AsyncAzureOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.azure import AzureProvider

    if not profile.endpoint:
        raise ValueError("azure profiles require an endpoint (the Azure OpenAI resource URL)")
    method = profile.auth.method
    if method not in ("environment", "keyring", "provider-default"):
        raise ValueError(
            "azure profiles require an api key (environment or keyring) or Entra ID "
            f"(provider-default); {method!r} is not an Azure OpenAI authentication mode"
        )
    endpoint = profile.endpoint
    deployment = _azure_deployment(profile)
    api_version = _azure_api_version(profile)
    client_transport = _http_client(ca_bundle, http_client)
    if method == "provider-default":
        from korvid.providers.entra import EntraCredentialSource

        source = EntraCredentialSource()
        # `AsyncAzureOpenAI` calls this on every request that needs a
        # token and awaits the result; the source's own lock and cache
        # keep that to one real token call per expiry window.
        token_provider: Callable[[], Awaitable[str]] = source.access_token
        client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            azure_deployment=deployment,
            api_version=api_version,
            azure_ad_token_provider=token_provider,
            http_client=client_transport,
        )
    else:
        client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            azure_deployment=deployment,
            api_version=api_version,
            api_key=_secret(profile),
            http_client=client_transport,
        )
    return OpenAIChatModel(model_tag(profile.model), provider=AzureProvider(openai_client=client))


def _build_anthropic_model(
    profile: AgentProfileConfig, ca_bundle: str | None, http_client: httpx2.AsyncClient | None
) -> Model:
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    return AnthropicModel(
        model_tag(profile.model),
        provider=AnthropicProvider(
            api_key=_vendor_api_key(profile, "anthropic"),
            base_url=profile.endpoint,
            http_client=_http_client(ca_bundle, http_client),
        ),
    )


def _build_google_model(
    profile: AgentProfileConfig, ca_bundle: str | None, http_client: httpx2.AsyncClient | None
) -> Model:
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider

    return GoogleModel(
        model_tag(profile.model),
        provider=GoogleProvider(
            api_key=_vendor_api_key(profile, "google"),
            base_url=profile.endpoint,
            http_client=_http_client(ca_bundle, http_client),
        ),
    )


def _build_bedrock_model(profile: AgentProfileConfig) -> Model:
    """Bedrock authenticates through the AWS SDK's own credential chain.

    `ca_bundle` is deliberately not applied: botocore takes its trust from
    `AWS_CA_BUNDLE`/its own config, and passing korvid's httpx client here
    would be ignored rather than honoured — a silent half-measure. The
    ambient AWS chain is not a fallback here either: `provider-default` is
    the only auth method the `bedrock` descriptor offers, so using it is
    the operator's explicit choice.

    Raises:
        ValueError: When the profile names no region. For Bedrock the
            region *is* the endpoint, and the SDK's own message
            ("You must provide a `region_name` or a boto3 client for
            Bedrock Runtime") does not say which korvid setting to fix.
    """
    from pydantic_ai.models.bedrock import BedrockConverseModel
    from pydantic_ai.providers.bedrock import BedrockProvider

    region = profile.options.get("region_name")
    if not isinstance(region, str) or not region:
        raise ValueError(
            "a bedrock profile requires options.region_name (for example us-east-1)"
        )
    return BedrockConverseModel(
        model_tag(profile.model), provider=BedrockProvider(region_name=region)
    )


def build_model(
    profile: AgentProfileConfig,
    *,
    ca_bundle: str | None = None,
    oauth_token: str | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> Model:
    """Construct the Pydantic AI model *profile* names.

    Args:
        profile: The connection profile to build.
        ca_bundle: `network.ca_bundle`, applied to every HTTP-based adapter.
        oauth_token: The GitHub OAuth token, consumed from Task 16 onward.
        http_client: Wire-test seam; production callers leave this None.

    Raises:
        AdapterExtraMissing: When the adapter is unknown or its optional
            extra is not installed.
        AdapterNotConnected: When the adapter's transport lands in a later
            task on this branch.
        ValueError: When the profile's endpoint or credential reference is
            unusable for the adapter it names.
    """
    adapter = adapter_id(profile.model)
    if adapter not in ADAPTER_EXTRAS:
        raise AdapterExtraMissing(f"unknown model adapter: {adapter!r}")
    if not _adapter_installed(adapter):
        hint = install_hint(adapter)
        raise AdapterExtraMissing(hint, install_hint=hint)
    if adapter in _NOT_CONNECTED_YET:
        raise AdapterNotConnected(
            f"the {adapter} adapter is not connected yet; the agent stays disabled "
            "rather than sending an unusable credential"
        )
    if adapter == "azure":
        return _build_azure_model(profile, ca_bundle, http_client)
    if adapter == "anthropic":
        return _build_anthropic_model(profile, ca_bundle, http_client)
    if adapter == "google":
        return _build_google_model(profile, ca_bundle, http_client)
    if adapter == "bedrock":
        return _build_bedrock_model(profile)
    if adapter == "ollama":
        return _build_ollama_model(profile, ca_bundle, http_client)
    return _build_openai_model(
        profile, profile.endpoint, ca_bundle, _openai_api_key(profile), http_client
    )


def _declared_capabilities(profile: AgentProfileConfig) -> ModelCapabilities:
    """Capabilities korvid can prove from explicit configuration only."""
    declared = profile.options.get("num_ctx")
    if isinstance(declared, int) and declared > 0:
        return ModelCapabilities(
            context_window_tokens=declared,
            provenance={"context_window_tokens": CapabilitySource.USER},
        )
    return ModelCapabilities.unknown()


#: Profile option → Ollama native request key. These have no
#: `ModelSettings` equivalent, so they travel in `extra_body`, which
#: Pydantic AI merges into the request JSON verbatim.
_OLLAMA_NATIVE_OPTIONS: Final[frozenset[str]] = frozenset({"num_ctx", "num_predict"})
_OLLAMA_TOP_LEVEL_OPTIONS: Final[frozenset[str]] = frozenset({"think", "keep_alive"})


def _model_settings(profile: AgentProfileConfig) -> ModelSettings | None:
    """Turn profile options into a typed `ModelSettings`.

    Built key by key rather than by copying a `dict`, so mypy checks every
    assignment against the `total=False` TypedDict and an unsupported key
    is a type error rather than a silently ignored one. No `cast`, no
    `Any`.

    Ollama's `num_predict` deliberately does *not* become `max_tokens`:
    the OpenAI model serializes `max_tokens` as `max_completion_tokens`,
    which Ollama's compatibility route does not map back to `num_predict`.
    It goes into the native `options` bag instead.
    """
    options: Mapping[str, object] = profile.options
    settings: ModelSettings = {}
    temperature = options.get("temperature")
    if isinstance(temperature, float | int) and not isinstance(temperature, bool):
        settings["temperature"] = float(temperature)
    seed = options.get("seed")
    if isinstance(seed, int) and not isinstance(seed, bool):
        settings["seed"] = seed
    max_tokens = options.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
        settings["max_tokens"] = max_tokens
    if adapter_id(profile.model) == "ollama":
        extra_body = _ollama_extra_body(options)
        if extra_body:
            settings["extra_body"] = extra_body
    return settings or None


def _ollama_extra_body(options: Mapping[str, object]) -> dict[str, object]:
    """Ollama tuning that has no `ModelSettings` key, in Ollama's own shape."""
    extra_body: dict[str, object] = {}
    native: dict[str, int] = {}
    for key in sorted(_OLLAMA_NATIVE_OPTIONS):
        value = options.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            native[key] = value
    if native:
        extra_body["options"] = native
    for key in sorted(_OLLAMA_TOP_LEVEL_OPTIONS):
        value = options.get(key)
        if value is not None:
            extra_body[key] = value
    return extra_body


def build_model_provider(
    profile: AgentProfileConfig,
    *,
    ca_bundle: str | None = None,
    oauth_token: str | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> PydanticModelProvider:
    """Build the korvid provider for *profile*."""
    model = build_model(
        profile, ca_bundle=ca_bundle, oauth_token=oauth_token, http_client=http_client
    )
    return PydanticModelProvider(
        model=model,
        descriptor=ModelDescriptor(
            provider=adapter_id(profile.model), model=model_tag(profile.model)
        ),
        capabilities=_declared_capabilities(profile),
        model_settings=_model_settings(profile),
    )
```

`CapabilitySource.USER` is the correct provenance here: the context window is a fact the *operator* asserted in `options.num_ctx`, not one the provider reported. Never widen this to a name-based guess, and never add a new enum member to make this call read better.

`build_model` is a flat dispatch of seven adapters plus two guards; keep each adapter's construction in its own `_build_*` helper so the function stays under ruff's C901 ceiling of 10 as adapters are added.

- [ ] **Step 5: Route profiles through the factory in the registry**

Add to `src/korvid/providers/registry.py`:

```python
def create_profile_provider(
    profile: AgentProfileConfig,
    *,
    oauth_token: str | None = None,
    ca_bundle: str | None = None,
    plugin_registry: ProviderPluginRegistry | None = None,
) -> LLMProvider | None:
    """Build the provider for one profile, or None when it cannot be built.

    A built-in adapter goes through the Pydantic AI factory; anything
    else is a registered plugin. An unknown adapter returns None so the
    TUI can start with the agent disabled and a visible warning rather
    than crashing.

    A built-in adapter this branch has not connected yet, or one whose
    optional extra is missing, also returns None with a warning naming the
    reason. It never falls through to another adapter: silently building
    an OpenAI client for an `anthropic` profile would send an OpenAI
    bearer token to Anthropic's endpoint. Visibly disabled beats
    misrouted.

    A profile carrying a `config_error` is refused outright. That flag
    means the bounded validator emptied its `options` or `auth.settings`,
    so connecting would use silently discarded configuration — an
    operator who wrote a proxy, a header or a credential reference that
    korvid rejected would get a *working-looking* connection that ignores
    it. This check is the last one before a socket is opened, and it does
    not depend on any caller remembering to make it.

    Raises:
        ProviderPluginError: On any plugin discovery, validation or
            construction failure — the caller decides whether that is a
            startup warning or a failed rebuild.
    """
    if profile.config_error is not None:
        logger.warning(
            "the active profile was rejected: %s — agent disabled", profile.config_error
        )
        return None
    adapter = adapter_id(profile.model)
    if not adapter or not model_tag(profile.model):
        return None
    if adapter in BUILTIN_ADAPTERS:
        try:
            return build_model_provider(profile, ca_bundle=ca_bundle, oauth_token=oauth_token)
        except AdapterNotConnected as exc:
            logger.warning("%s — agent disabled", exc)
            return None
        except AdapterExtraMissing as exc:
            logger.warning("adapter %r unavailable: %s — agent disabled", adapter, exc)
            return None
        except ValueError as exc:
            # `build_model` raises a plain ValueError for every "this
            # profile cannot produce a client" case: an `azure` profile
            # with no endpoint, an auth method the adapter does not
            # declare, a missing API key, a `bedrock` profile without
            # `options.region_name`, an unreadable CA bundle. Those are
            # operator configuration mistakes, not korvid bugs, and the
            # set is bounded — `build_model` raises no other exception
            # type of its own. Letting one escape would abort startup with
            # a traceback for a typo in config.yaml.
            logger.warning("adapter %r cannot be built: %s — agent disabled", adapter, exc)
            return None
    if plugin_registry is None:
        logger.warning("unknown model adapter %r — agent disabled", adapter)
        return None

    from korvid.agent.provider_plugin import ProviderPluginConfig

    # `load_selected` validates the plugin and caches it; it raises
    # ProviderPluginError, which propagates by design.
    plugin_registry.load_selected(adapter)
    key = profile.auth.settings.get("key")
    api_key_env = key if isinstance(key, str) and profile.auth.method == "environment" else None
    credentials = resolve_credential(profile)
    config = ProviderPluginConfig(
        base_url=profile.endpoint,
        model=model_tag(profile.model),
        auth_method=_PLUGIN_AUTH_BACK.get(profile.auth.method, profile.auth.method),
        api_key_env=api_key_env,
        options=profile.options,
    )
    try:
        # Exact 3-argument signature of ProviderPluginRegistry.create:
        # create(name: str, config: ProviderPluginConfig,
        #        credentials: CredentialSource | None) -> LLMProvider
        return plugin_registry.create(adapter, config, credentials)
    except Exception:
        # Mirrors `_create_via_plugin`: a plugin that fails to construct
        # must not leave a live credential source behind.
        _close_credentials(credentials)
        raise
```

with the module constant, and `BUILTIN_ADAPTERS` imported from the leaf module rather than
redefined (a second copy in `registry.py` would silently drift the day an adapter is added):

```python
from korvid.providers.adapter_extras import BUILTIN_ADAPTERS

#: Common auth ids → the v2 plugin contract's vocabulary.
_PLUGIN_AUTH_BACK: Final[dict[str, str]] = {
    "environment": "api_key",
    "keyring": "api_key",
    "provider-default": "entra",
}
```

`adapter_extras` is deliberately import-safe: it reads `importlib.metadata` and imports no
SDK. `pydantic_factory` is not — it imports `pydantic_ai` and `httpx2` at module scope —
so `create_profile_provider` imports it **inside the function**, as the first statement
after the `config_error` refusal (both the built-in branch and Task 17's `native_api`
branch use names from it, so it cannot sit lower down beside the function-local
`ProviderPluginConfig` import):

```python
    from korvid.providers.pydantic_factory import (
        AdapterExtraMissing,
        AdapterNotConnected,
        build_model_provider,
        resolve_credential,
    )
```

`registry.py` is on `korvid.__main__`'s eager import path, so a module-scope import here
would drag the whole `[agent]` stack into a base install and break
`tests/test_optional_extras.py`. Task 19 mutates exactly this line to prove that guard is
load-bearing.

**Retire the interim builder in this same commit.** In `src/korvid/__main__.py`, replace
the body of the nested `build_provider(profile: AgentProfileConfig)` closure (Task 10
Step 5) with:

```python
    def build_provider(profile: AgentProfileConfig) -> LLMProvider | None:
        return create_profile_provider(
            profile,
            oauth_token=token_store.load("github-oauth"),
            ca_bundle=config.network_ca_bundle,
            plugin_registry=plugin_registry,
        )
```

The parameter type and the name are already correct from Task 10, so `_make_rebuild_agent`
and its `Callable[[AgentProfileConfig], LLMProvider | None]` annotation do not move.
Deleting the body removes the last `create_provider` call site outside `registry.py`, the
last use of `_derive_legacy_scalars` in `__main__.py`, and the last reader of the captured
`ollama_options` on this path. In `src/korvid/core/config.py`, delete
`_ADAPTERS_WITHOUT_LEGACY_TRANSPORT`, the branch in `_derive_legacy_scalars` that reads
it, and `test_an_adapter_without_a_transport_disables_the_agent` in
`tests/core/test_config_profiles.py`. Leaving that guard in place after this task would
disable `anthropic`, `google` and `bedrock` profiles that now have a real transport, which
is the exact failure the guard was built to make impossible in the other direction.

Note that `keyring` maps to the plugin contract's `api_key`: the v2 contract has no
keyring member, and `resolve_credential` has already read the secret out of the keyring by
the time the plugin sees it, so `api_key` is the honest description of what the plugin
receives. `none` and `device-login` are deliberately absent — `_PLUGIN_AUTH_BACK.get`
passes them through verbatim so a plugin that does not declare them refuses the profile
instead of being handed a method it never advertised.

`_close_credentials` already exists in `registry.py` (it backs `_create_via_plugin`); reuse
it rather than adding a second closer.

Add to `tests/providers/test_registry.py`. Because `create_profile_provider` now imports
`pydantic_factory` on every call, add `pytest.importorskip("pydantic_ai")` at the top of
that module (with `# noqa: E402  # after importorskip` on the imports below it, matching
the convention the other transport test modules in this plan already use). Without it, a
base-install `pytest` run would fail here instead of skipping — the file tests a function
that is now unconditionally part of the `[agent]` surface:

```python
def test_create_profile_provider_passes_the_exact_plugin_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any, Any]] = []

    class _Registry:
        def load_selected(self, name: str) -> object:
            return object()

        def create(self, name: str, config: Any, credentials: Any) -> Any:
            calls.append((name, config, credentials))
            return _RecordingProvider()

    profile = AgentProfileConfig(
        model="company-llm:v2",
        endpoint="https://llm.corp.invalid",
        auth=AgentAuthConfig(method="environment", settings={"key": "CORP_KEY"}),
    )
    monkeypatch.setenv("CORP_KEY", "sk-corp")
    provider = create_profile_provider(profile, plugin_registry=cast(Any, _Registry()))
    assert provider is not None
    name, config, credentials = calls[0]
    assert name == "company-llm"
    assert config.model == "v2"
    assert config.base_url == "https://llm.corp.invalid"
    assert config.auth_method == "api_key"
    assert config.api_key_env == "CORP_KEY"
    assert credentials is not None
```

The keyring case is the one the plugin contract has no word for, so pin its semantics
explicitly rather than leaving them to be inferred:

```python
async def test_keyring_backed_plugin_gets_the_secret_not_an_env_var_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`keyring` is reported as `api_key` with **no** `api_key_env`.

    The v2 plugin contract has no keyring member. `resolve_credential` has
    already read the secret out of the OS keyring by the time the plugin is
    constructed, so the plugin receives it through `credentials`, and
    `api_key_env` stays `None` because there is no environment variable to
    name. A plugin that ignores `credentials` and reads `api_key_env`
    therefore fails loudly instead of reading an unrelated variable.
    """
    calls: list[tuple[str, Any, Any]] = []

    class _Registry:
        def load_selected(self, name: str) -> object:
            return object()

        def create(self, name: str, config: Any, credentials: Any) -> Any:
            calls.append((name, config, credentials))
            return _RecordingProvider()

    monkeypatch.setattr(
        "korvid.providers.pydantic_factory.TokenStore",
        lambda: SimpleNamespace(load=lambda entry: "sk-from-keyring"),
    )
    profile = AgentProfileConfig(
        model="company-llm:v2",
        endpoint="https://llm.corp.invalid",
        auth=AgentAuthConfig(method="keyring", settings={"key": "corp-llm"}),
    )
    provider = create_profile_provider(profile, plugin_registry=cast(Any, _Registry()))
    assert provider is not None
    _name, config, credentials = calls[0]
    assert config.auth_method == "api_key"
    assert config.api_key_env is None
    assert await credentials.headers() == {"Authorization": "Bearer sk-from-keyring"}
```

(`SimpleNamespace` comes from `types`; add the import at the top of the test module.)

```python
def test_create_profile_provider_disables_rather_than_misroutes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An adapter this branch has not connected yet must not fall through.

    `github-copilot` is a built-in adapter whose transport lands in
    Task 16. Until then the honest answer is "agent disabled", not an
    OpenAI client carrying a Copilot OAuth token.
    """
    profile = AgentProfileConfig(
        model="github-copilot:gpt-4o",
        auth=AgentAuthConfig(method="device-login"),
    )
    with caplog.at_level(logging.WARNING, logger="korvid.providers.registry"):
        assert create_profile_provider(profile) is None
    assert "github-copilot" in caplog.text
    assert "agent disabled" in caplog.text


def test_create_profile_provider_refuses_a_profile_with_a_config_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected block means discarded configuration; do not connect.

    This test is deliberately written against `create_profile_provider`
    rather than against `load_config`, so it survives Task 17's deletion
    of the legacy scalars: after that task, this function is the *only*
    place the refusal can happen, and nothing above it is required to
    remember to check.
    """
    profile = AgentProfileConfig(
        model="ollama:llama3",
        endpoint="http://localhost:11434",
        options={"blob": "x" * 4096},
    )
    assert profile.config_error is not None
    with caplog.at_level(logging.WARNING, logger="korvid.providers.registry"):
        assert create_profile_provider(profile) is None
    assert "rejected" in caplog.text
    assert "agent disabled" in caplog.text
    assert "x" * 4096 not in caplog.text


def test_create_profile_provider_turns_a_build_failure_into_a_disabled_agent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A configuration mistake must not abort startup with a traceback.

    An `azure` profile with no endpoint is the cheapest reproduction:
    `build_model` raises a plain `ValueError` for it, and every other
    "this profile cannot produce a client" case in that function raises
    the same type.
    """
    profile = AgentProfileConfig(
        model="azure:gpt-4o",
        auth=AgentAuthConfig(method="environment", settings={"key": "AZURE_OPENAI_API_KEY"}),
    )
    with caplog.at_level(logging.WARNING, logger="korvid.providers.registry"):
        assert create_profile_provider(profile) is None
    assert "azure" in caplog.text
    assert "agent disabled" in caplog.text


@pytest.mark.parametrize(
    "profile",
    [
        AgentProfileConfig(model="azure:gpt-4o"),
        AgentProfileConfig(
            model="azure:gpt-4o",
            endpoint="https://x.openai.azure.com",
            auth=AgentAuthConfig(method="device-login"),
        ),
        AgentProfileConfig(model="bedrock:anthropic.claude-3-5-sonnet-20241022-v2:0"),
    ],
)
def test_every_build_refusal_is_a_value_error(profile: AgentProfileConfig) -> None:
    """Pins the bounded exception set `create_profile_provider` catches.

    If `build_model` ever grows a refusal that raises something else, this
    fails here rather than as an unhandled traceback during startup.
    """
    with pytest.raises(ValueError, match=r".+"):
        build_model(profile)
```

The last test belongs in `tests/providers/test_pydantic_factory.py`, next to the other
`build_model` tests, not in `test_registry.py`: it calls `build_model` directly, and that
module is behind this task's `pytest.importorskip("pydantic_ai")` with all four
`provider-*` extras installed (Task 11). The three `create_profile_provider` tests stay in
`test_registry.py`. `_RecordingProvider` is the existing minimal `LLMProvider` stub in that
file; `logging` and `AgentAuthConfig` are already imported there as of Task 7.

**The composition root surfaces the refusal.** `create_profile_provider` returning `None`
is the whole contract: `__main__._build_agent_wiring` appends the reason to
`startup_warnings` and hands the app an agent-unavailable wiring, so an operator sees
"the agent is disabled and here is why" in the status bar instead of either a traceback or
a silently mis-configured connection. Task 17 Step 3 spells out the call site.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/providers/ -q`
Expected: PASS, including `tests/providers/test_adapter_catalog.py`, whose `AdapterCatalog.test()` and `AdapterCatalog._auth_headers` paths now resolve (both call into `create_profile_provider`/`resolve_credential`, which exist as of this task).

- [ ] **Step 7: Lint, typecheck, layer check and commit**

```bash
uv run ruff check --fix src/korvid/providers/ tests/providers/
uv run ruff format src/korvid/providers/ tests/providers/
uv run mypy src/korvid/providers/
uv run tach check
git add src/korvid/providers tests/providers
git commit -m "feat: build model providers from profiles" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 15: Outbound, instrumentation and cancellation contracts

**Files:**
- Create: `tests/providers/test_pydantic_contracts.py`
- Modify: `src/korvid/providers/pydantic_model.py` (instrumentation opt-out)

**Interfaces:**
- Consumes: `PydanticModelProvider` (Task 13), `OutboundPolicy.prepare(...) -> PreparedOutbound` (`.messages`, `.tools`, `.snapshot`), `provider_prepared_messages`; the `Chunk`/`ScriptedStream`/`FakeModel` doubles from `tests/providers/test_pydantic_model.py`.
- Produces: `PydanticModelProvider.instrumentation_enabled: bool`, plus the guarantee that the audited snapshot and the transmitted request describe the same payload, that instrumentation is off, and that a cancelled turn leaves no open transport.

- [ ] **Step 1: Write the failing contract tests**

Create `tests/providers/test_pydantic_contracts.py`:

```python
"""Cross-cutting guarantees the model transport must not weaken."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import UserPromptPart  # noqa: E402  # after importorskip

from korvid.agent.model_policy import ModelDescriptor  # noqa: E402
from korvid.agent.outbound import OutboundPolicy, provider_prepared_messages  # noqa: E402
from korvid.agent.provider import REQUEST_SENT  # noqa: E402
from korvid.providers.pydantic_model import PydanticModelProvider  # noqa: E402
from tests.providers.test_pydantic_model import Chunk, FakeModel, ScriptedStream  # noqa: E402


def _provider(model: FakeModel) -> PydanticModelProvider:
    return PydanticModelProvider(
        model=model, descriptor=ModelDescriptor(provider="openai", model="gpt-4o")
    )


async def test_the_transmitted_payload_matches_the_audited_snapshot() -> None:
    model = FakeModel(ScriptedStream([Chunk(text="ok")]))
    provider = _provider(model)
    policy = OutboundPolicy(max_request_chars=4096)
    prepared = policy.prepare(
        "gpt-4o",
        provider_prepared_messages(provider, [{"role": "user", "content": "token=sk-live-secret"}]),
        [],
        iteration=1,
    )
    async for _ in provider.complete(prepared.messages, prepared.tools):
        pass
    sent_messages, _, parameters = model.requests[0]
    prompts = [
        part.content
        for message in sent_messages
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert prompts == [message["content"] for message in prepared.messages]
    assert "sk-live-secret" not in " ".join(prompts)
    assert parameters.function_tools == []


async def test_the_transport_adds_no_message_or_tool_of_its_own() -> None:
    model = FakeModel(ScriptedStream([Chunk(text="ok")]))
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "question"},
    ]
    async for _ in _provider(model).complete(messages, []):
        pass
    sent_messages, _, parameters = model.requests[0]
    assert sum(len(message.parts) for message in sent_messages) == 2
    assert parameters.function_tools == []
    assert parameters.output_tools == []
    # `ModelRequestParameters` defaults this to None, not []. Asserting
    # `== []` here would fail against pydantic-ai-slim 2.35.3.
    assert parameters.instruction_parts is None


async def test_pydantic_ai_instrumentation_is_disabled() -> None:
    from pydantic_ai.models.instrumented import InstrumentedModel

    model = FakeModel(ScriptedStream([Chunk(text="ok")]))
    provider = _provider(model)
    async for _ in provider.complete([{"role": "user", "content": "x"}], []):
        pass
    assert not isinstance(model, InstrumentedModel)
    assert provider.instrumentation_enabled is False


async def test_an_abandoned_stream_closes_the_transport() -> None:
    """A cancelled turn must exit the `request_stream` context.

    The stream stalls *after* the context is entered, so `REQUEST_SENT`
    is emitted and the cancellation has to unwind through the context
    manager — which is what sets `FakeModel.exited`.
    """

    class StallingStream(ScriptedStream):
        async def _get_event_iterator(self) -> Any:
            await asyncio.sleep(5)
            yield None  # pragma: no cover - never reached

    model = FakeModel(StallingStream([]))
    provider = _provider(model)
    seen: list[dict[str, Any]] = []
    # An Event, not a sleep: the cancellation must happen after the first
    # event is observed, and no wall-clock delay can promise that.
    first_event = asyncio.Event()

    async def consume() -> None:
        async for event in provider.complete([{"role": "user", "content": "x"}], []):
            seen.append(event)
            first_event.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_event.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [{"type": REQUEST_SENT}]
    assert model.exited is True


async def test_request_sent_precedes_every_completion_event() -> None:
    model = FakeModel(ScriptedStream([Chunk(text="ok")]))
    events = [
        event async for event in _provider(model).complete([{"role": "user", "content": "x"}], [])
    ]
    assert events[0]["type"] == REQUEST_SENT
    assert all(event["type"] != REQUEST_SENT for event in events[1:])
```

`tests/__init__.py` and `tests/providers/__init__.py` both exist, so importing the
doubles from `tests.providers.test_pydantic_model` is a package import, not a path hack.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/providers/test_pydantic_contracts.py -q`
Expected: FAIL — `AttributeError: 'PydanticModelProvider' object has no attribute 'instrumentation_enabled'`.

- [ ] **Step 3: Make the instrumentation opt-out explicit**

In `src/korvid/providers/pydantic_model.py`, add to `PydanticModelProvider`:

```python
    @property
    def instrumentation_enabled(self) -> bool:
        """Whether Pydantic AI instrumentation wraps this model.

        Always False in korvid: the factory constructs bare models and
        never wraps them in `InstrumentedModel`, so no prompt, tool
        argument, response or secret can reach an OpenTelemetry or
        Logfire exporter. korvid's own audit log remains the only record
        of a request. This property exists so that invariant is asserted
        rather than assumed.
        """
        from pydantic_ai.models.instrumented import InstrumentedModel

        return isinstance(self._model, InstrumentedModel)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/providers/ tests/agent/ -q`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck and commit**

```bash
uv run ruff check --fix src/korvid/providers/pydantic_model.py tests/providers/test_pydantic_contracts.py
uv run ruff format src/korvid/providers/pydantic_model.py tests/providers/test_pydantic_contracts.py
uv run mypy src/korvid/providers/pydantic_model.py
git add src/korvid/providers/pydantic_model.py tests/providers/test_pydantic_contracts.py
git commit -m "test: pin the outbound, instrumentation and cancellation contracts" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Commit group 5 — GitHub Copilot as a catalog extension (Task 16)

### Task 16: Copilot device login and token exchange through the catalog

**Files:**
- Modify: `src/korvid/providers/github_copilot.py`
- Modify: `src/korvid/providers/pydantic_factory.py`
- Create: `tests/providers/test_copilot_catalog.py`

**Interfaces:**
- Consumes: `GitHubDeviceFlow`, `CopilotCredentialSource`, `COPILOT_CHAT_BASE_URL`, `TokenStore`; `build_model` (Task 14); `ProviderModelCatalog` (Task 6).
- Produces:
  - `CopilotAuth` — an `httpx2.Auth` subclass that injects the exchanged Copilot session bearer + required Copilot headers on every request
  - `build_copilot_model(profile: AgentProfileConfig, oauth_token: str) -> Model` in `pydantic_factory.py`

- [ ] **Step 1: Write the failing Copilot tests**

Create `tests/providers/test_copilot_catalog.py`:

```python
"""GitHub Copilot is an ordinary catalog adapter with a device-login method."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

# `httpx2` (not the legacy `httpx`) is the client flavour the OpenAI SDK
# and Pydantic AI 2.35.3 use; handing a legacy client to a provider
# raises `PydanticAIDeprecationWarning`, which `filterwarnings =
# ["error"]` turns into a test failure.
import httpx2  # noqa: E402  # after importorskip

from korvid.agent.model_profiles import (  # noqa: E402  # after importorskip
    AgentAuthConfig,
    AgentProfileConfig,
    DeviceLoginPrompt,
)
from korvid.providers.adapter_catalog import (  # noqa: E402  # after importorskip
    ProviderModelCatalog,
)
from korvid.providers.github_copilot import (  # noqa: E402  # after importorskip
    COPILOT_CHAT_BASE_URL,
    CopilotAuth,
    DeviceCodePrompt,
)
from korvid.providers.pydantic_factory import (  # noqa: E402  # after importorskip
    AdapterExtraMissing,
    build_model,
)
from korvid.providers.token_store import TokenStore  # noqa: E402  # after importorskip

_PROFILE = AgentProfileConfig(
    model="github-copilot:gpt-4o", auth=AgentAuthConfig(method="device-login")
)


class _MemoryTokenStore(TokenStore):
    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def save(self, key: str, value: str) -> None:
        self._tokens[key] = value

    def load(self, key: str) -> str | None:
        return self._tokens.get(key)

    def delete(self, key: str) -> None:
        self._tokens.pop(key, None)


class _FakeFlow:
    def __init__(self) -> None:
        self.closed = False

    async def start(self) -> DeviceCodePrompt:
        return DeviceCodePrompt(
            device_code="dc", user_code="ABCD-1234", verification_uri="https://github.com/login/device", interval=1
        )

    async def poll(self, prompt: DeviceCodePrompt) -> str:
        return "gho_token"

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_device_login_stores_the_oauth_token_and_closes_the_flow() -> None:
    store = _MemoryTokenStore()
    flow = _FakeFlow()
    catalog = ProviderModelCatalog(token_store=store, flow_factory=lambda: flow)
    prompt = await catalog.begin_auth(_PROFILE)
    assert isinstance(prompt, DeviceLoginPrompt)
    assert prompt.user_code == "ABCD-1234"
    await catalog.finish_auth(_PROFILE)
    assert store.load("github-oauth") == "gho_token"
    assert flow.closed


@pytest.mark.asyncio
async def test_begin_auth_is_a_no_op_for_a_non_device_login_profile() -> None:
    catalog = ProviderModelCatalog(token_store=_MemoryTokenStore())
    profile = AgentProfileConfig(model="openai:gpt-4o", auth=AgentAuthConfig(method="none"))
    assert await catalog.begin_auth(profile) is None


@pytest.mark.asyncio
async def test_copilot_auth_injects_the_exchanged_bearer_and_headers() -> None:
    class _Source:
        async def headers(self) -> dict[str, str]:
            return {
                "Authorization": "Bearer copilot-session-token",
                "Editor-Version": "korvid/0.3.0",
                "Copilot-Integration-Id": "vscode-chat",
            }

        async def aclose(self) -> None:
            return None

    auth = CopilotAuth(_Source())
    request = httpx2.Request("POST", f"{COPILOT_CHAT_BASE_URL}/chat/completions")
    flow = auth.async_auth_flow(request)
    prepared = await flow.__anext__()
    assert prepared.headers["Authorization"] == "Bearer copilot-session-token"
    assert prepared.headers["Copilot-Integration-Id"] == "vscode-chat"
    await flow.aclose()


@pytest.mark.asyncio
async def test_the_oauth_token_is_never_sent_as_the_chat_bearer() -> None:
    class _Source:
        async def headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer copilot-session-token"}

        async def aclose(self) -> None:
            return None

    auth = CopilotAuth(_Source())
    request = httpx2.Request("POST", f"{COPILOT_CHAT_BASE_URL}/chat/completions")
    flow = auth.async_auth_flow(request)
    prepared = await flow.__anext__()
    assert "gho_" not in prepared.headers["Authorization"]
    await flow.aclose()


def test_copilot_descriptor_offers_only_device_login() -> None:
    catalog = ProviderModelCatalog(token_store=_MemoryTokenStore())
    descriptor = catalog.descriptor("github-copilot")
    assert descriptor is not None
    assert [method.id for method in descriptor.auth_methods] == ["device-login"]
    assert descriptor.auth_methods[0].fields == ()


def test_copilot_is_no_longer_refused_by_the_factory() -> None:
    """Task 14 disabled Copilot on purpose; this task connects it.

    Until this task, `build_model` raised `AdapterNotConnected` for
    `github-copilot` so the agent stayed visibly disabled instead of
    sending a Copilot OAuth token to an OpenAI-shaped endpoint. With the
    real transport in place that refusal must be gone, and the remaining
    failure mode must be the honest one: no device login yet.
    """
    with pytest.raises(AdapterExtraMissing, match="device login"):
        build_model(_PROFILE)
    assert build_model(_PROFILE, oauth_token="gho_token") is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:tach tests/providers/test_copilot_catalog.py -q`
Expected: FAIL — `ImportError: cannot import name 'CopilotAuth' from 'korvid.providers.github_copilot'`.

- [ ] **Step 3: Add the httpx auth hook**

Append to `src/korvid/providers/github_copilot.py`:

```python
class CopilotAuth(httpx2.Auth):
    """Inject Copilot's exchanged session bearer on every request.

    Copilot's chat endpoint does not accept the OAuth device token: that
    token is exchanged for a short-lived session token, and only the
    session token is ever sent as the chat bearer. Because the exchange
    can happen mid-conversation, the header is produced per request
    rather than baked into the client at construction time.
    """

    def __init__(self, source: CredentialSource) -> None:
        self._source = source

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        for name, value in (await self._source.headers()).items():
            request.headers[name] = value
        yield request
```

Add `from collections.abc import AsyncGenerator` and `import httpx2` to the module's
imports. `httpx2.Auth.async_auth_flow(self, request: Request) -> AsyncGenerator[Request,
Response]` is the exact 2.12 signature this overrides. korvid's existing Copilot
discovery client keeps using legacy `httpx` — that client never reaches Pydantic AI, so
the two flavours coexist without a deprecation warning.

- [ ] **Step 4: Build the Copilot model with that auth**

In `src/korvid/providers/pydantic_factory.py`, replace the `github-copilot` branch of `build_model` with a call to a dedicated builder:

```python
def build_copilot_model(profile: AgentProfileConfig, oauth_token: str) -> Model:
    """A Copilot chat model authenticated by the exchanged session token.

    `CopilotCredentialSource(oauth_token, client=None, clock=time.time)`
    exchanges the device-flow OAuth token for a short-lived session token
    and refreshes it before expiry; only that session token is ever sent
    as the chat bearer.
    """
    import httpx2
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from korvid.providers.github_copilot import (
        COPILOT_CHAT_BASE_URL,
        CopilotAuth,
        CopilotCredentialSource,
    )

    # `httpx2.Auth.async_auth_flow(self, request: Request) ->
    # AsyncGenerator[Request, Response]` is the exact hook httpx2 2.12
    # calls per request, which is why the exchanged token is always
    # current even when it is refreshed mid-conversation.

    # Copilot's endpoint is public: it uses default trust, matching the
    # pre-existing adapter, so a private-only CA bundle cannot break it.
    client = httpx2.AsyncClient(
        auth=CopilotAuth(CopilotCredentialSource(oauth_token)), timeout=60.0
    )
    provider = OpenAIProvider(base_url=COPILOT_CHAT_BASE_URL, api_key="unused", http_client=client)
    return OpenAIChatModel(model_tag(profile.model), provider=provider)
```

and in `build_model`, add the branch alongside the other adapters:

```python
    if adapter == "github-copilot":
        if not oauth_token:
            raise AdapterExtraMissing("GitHub Copilot requires a device login — run :ai")
        return build_copilot_model(profile, oauth_token)
```

In the same edit, empty the refusal set Task 14 introduced, because `github-copilot` was
its only member:

```python
#: Adapters whose Pydantic AI transport has not landed yet. Empty now
#: that Copilot is connected; kept as the documented mechanism for the
#: next adapter that is catalogued before it is wired.
_NOT_CONNECTED_YET: Final[frozenset[str]] = frozenset()
```

Leaving `github-copilot` in `_NOT_CONNECTED_YET` would make the new branch dead code and
keep the agent disabled; leaving the set behind but populated would be worse still, since
`build_model` checks it before the adapter dispatch. `test_copilot_is_no_longer_refused_by_the_factory`
fails while either mistake stands.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -p no:tach tests/providers/ -q`
Expected: PASS.

- [ ] **Step 6: Lint, typecheck and commit**

```bash
uv run ruff check --fix src/korvid/providers/ tests/providers/
uv run ruff format src/korvid/providers/ tests/providers/
uv run mypy src/korvid/providers/
git add src/korvid/providers tests/providers
git commit -m "feat: serve GitHub Copilot as a catalog adapter" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Commit group 6 — Legacy removal, documentation and gates (Tasks 17–20)

### Task 17: Delete the legacy provider configuration path

**Files:**
- Modify: `src/korvid/core/config.py` (remove the legacy scalars and `_LegacyAgentScalars`)
- Delete: `src/korvid/agent/setup.py`, `src/korvid/providers/configurator.py`, `src/korvid/providers/openai_compat.py`
- Create: `src/korvid/providers/errors.py` (`ProviderError`, rehomed out of the deleted `openai_compat.py`)
- Modify: `src/korvid/providers/ollama.py:25` and `tests/providers/test_ollama.py:20` — the `ProviderError` import only. The module and its tests are **kept whole**; see Step 3.
- **Keep:** `src/korvid/providers/entra.py`, `tests/providers/test_entra.py` and the `entra` extra — the `azure` adapter's `provider-default` method calls `EntraCredentialSource.access_token` (Task 14), so deleting them would remove a shipped auth method.
- **Keep:** `tests/ui/test_agent_setup_screen.py` — Task 10 Step 6 migrated it to the profile-driven screen.
- Modify: `src/korvid/providers/registry.py` (remove `create_provider`, `build_credentials`; add the `native_api` branch), `src/korvid/providers/plugin_registry.py` (remove `OPENAI_COMPAT_ALIASES`, keep its names reserved), `src/korvid/__main__.py`
- Modify: `tests/test_agent_replacement_guard.py`, `tests/providers/test_plugin_registry.py`, `tests/test_main_wiring.py`, `tests/providers/test_registry.py`
- Modify: `tests/core/test_config.py`, `tests/ui/test_agent_wiring.py`, `tests/ui/test_impact_security.py` — the three files that still read a legacy scalar. Step 4 gives the exact rule for each.

**Interfaces:**
- Consumes: everything Tasks 1–16 produced.
- Produces: exactly one configuration path. No module in the guarded set names an LLM vendor; no module at all reads `cfg.agent_provider`.

- [ ] **Step 1: Write the failing retirement guard**

In `tests/test_agent_replacement_guard.py`, extend the retired lists:

```python
_RETIRED_MODULES = (
    # …existing entries…
    "korvid.agent.setup",
    "korvid.providers.configurator",
    "korvid.providers.openai_compat",
)

_RETIRED_SYMBOLS = (
    # …existing entries…
    "AgentSettings",
    "AgentConfigurator",
    "ProviderConfigurator",
    "create_provider(",
    "build_credentials(",
    "_DEFAULTS",
    "_PROVIDER_LABELS",
    "_OPENAI_COMPAT_ALIASES",
    "_canonical_provider",
    "agent_provider",
    "agent_base_url",
    "agent_api_key_env",
    "agent_auth_method",
    "agent_ollama_num_ctx",
    "save_agent_config(",
    # The provider-shaped half of `KorvidConfig`, replaced by
    # `profile.options` and `profile.config_error` (Step 4).
    "agent_options",
    "agent_options_error",
)
```

Add a **narrowly scoped** vendor-leak guard. A tree-wide sweep for vendor words is the
wrong instrument: `korvid/k8s/csp.py` maps node labels to cloud service providers and must
say `azure`, `aws` and `gcp`, `korvid/agent/prompt_harness.py` names clouds in evaluation
prompts about clusters, and `korvid/tools/registry.py` says "OpenAI-style tool schema"
because that is the wire format's name. A guard that fires on all of those gets widened
until it is meaningless. So the guard covers exactly the modules this migration is
responsible for, and every exclusion is named with its reason:

```python
#: LLM adapter vendor words. `google` is deliberately absent: it is the
#: single most collision-prone word in a Kubernetes codebase
#: (`cloud.google.com/...` node labels), and the `google` adapter is
#: already covered by `gemini`/`vertex`.
_ADAPTER_VENDOR_RE = re.compile(
    r"\b(openai|anthropic|bedrock|ollama|copilot|azure|aws|gcp|vertex|gemini)\b",
    re.IGNORECASE,
)

#: The modules that used to choose an adapter, and must not any more.
#: Scoped by hand: a guard is only useful if firing means something
#: specific, and "some file somewhere said azure" does not.
_VENDOR_GUARDED = (
    "korvid/core/config.py",
    "korvid/__main__.py",
    "korvid/agent/model_profiles.py",
    "korvid/ui/agent_ui_controller.py",
    "korvid/ui/widgets/agent_setup_screen.py",
)

#: Explicitly out of scope, each for a reason that is not "it was
#: inconvenient". These are asserted to be *outside* `_VENDOR_GUARDED`
#: below, so nobody can quietly guard them later and then exempt them.
_VENDOR_OUT_OF_SCOPE = {
    # Detects which cloud a Kubernetes *node* runs on, from provider node
    # labels. Nothing to do with LLM adapters; renaming these strings
    # would break cluster detection.
    "korvid/k8s/csp.py": "cloud service provider detection for Kubernetes nodes",
    # A capability catalog *keyed by* adapter id: `MODEL_CATALOG` holds
    # entries like `provider="ollama", model="qwen3:8b"` that are looked
    # up by a profile's own adapter id. The vendor words are data being
    # matched against, not a decision about which adapter to use, and
    # deleting them would delete korvid's knowledge of shipped models.
    "korvid/agent/model_catalog.py": "capability catalog keyed by adapter id",
    # Names clouds inside evaluation prompts about clusters.
    "korvid/agent/prompt_harness.py": "cloud vocabulary inside evaluation prompts",
    # "OpenAI-style tool schema" is the wire format's name, not a choice
    # of adapter.
    "korvid/tools/registry.py": "the tool-schema dialect's proper name",
    "korvid/tools/executor.py": "the tool-schema dialect's proper name",
    "korvid/agent/conversation.py": "the message dialect's proper name",
    "korvid/agent/model_policy.py": "the tokeniser family's proper name",
    "korvid/agent/outbound.py": "native-dialect validation names the dialect",
    "korvid/agent/provider.py": "the provider ABC's docstring names examples",
    "korvid/agent/prompt_packs.py": "prompt packs are keyed by model family",
    "korvid/mcp/server.py": "tool-schema dialect and client names",
    "korvid/evals/__main__.py": "the eval harness pins concrete models by name",
    "korvid/evals/serving.py": "the eval harness pins concrete models by name",
    "korvid/ui/app.py": "an unrelated word boundary in prose",
}


def test_the_vendor_guard_and_its_exclusions_do_not_overlap() -> None:
    """An exclusion must never silence a guarded module.

    Without this, the cheapest way to make the guard below pass is to add
    the offending file to `_VENDOR_OUT_OF_SCOPE`, which is precisely the
    failure mode the guard exists to prevent.
    """
    overlap = sorted(set(_VENDOR_GUARDED) & set(_VENDOR_OUT_OF_SCOPE))
    assert overlap == [], f"{overlap} are both guarded and excused"
    assert "korvid/k8s/csp.py" not in _VENDOR_GUARDED


#: `core/config.py` alone reads files written by an older korvid, and an
#: old file names providers. The exemption is bounded to that module.
_LEGACY_MIGRATION_MODULE = "korvid/core/config.py"
#: Top-level definitions whose whole body is legacy translation: anything
#: named `_migrate_*` and anything whose name contains `legacy`.
_LEGACY_REGION_NAME_RE = re.compile(r"^_migrate|legacy", re.IGNORECASE)


def _legacy_region_nodes(tree: ast.Module) -> list[ast.stmt]:
    """The top-level statements that make up the legacy migration region."""
    nodes: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if _LEGACY_REGION_NAME_RE.search(node.name):
                nodes.append(node)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and _LEGACY_REGION_NAME_RE.search(target.id)
                for target in targets
            ):
                nodes.append(node)
    return nodes


def legacy_migration_lines(source: str) -> frozenset[int]:
    """The 1-based line numbers of `core/config.py`'s legacy migration region.

    The exemption has to be *regional*, not per line. A per-line rule
    ("the line says legacy") exempts the header of
    `_LEGACY_OPENAI_COMPAT_NAMES` but not the continuation lines that hold
    its actual contents, and exempts `def _migrate_azure_endpoint` but not
    the `azure` inside its body — so a correct implementation of this
    plan's own Task 2 would fail its own guard.

    The region is computed from the module's AST: every top-level
    function or class named `_migrate*` or containing `legacy`, every
    top-level assignment to such a name, their decorators, and the comment
    block directly above each — comments are where a migration explains
    which old vendor spelling it accepts.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    exempt: set[int] = set()
    for node in _legacy_region_nodes(tree):
        start = min(
            [node.lineno, *(decorator.lineno for decorator in getattr(node, "decorator_list", []))]
        )
        while start > 1 and lines[start - 2].strip().startswith("#"):
            start -= 1
        end = node.end_lineno or node.lineno
        exempt.update(range(start, end + 1))
    return frozenset(exempt)


def _vendor_exempt_lines(relative: str, source: str) -> frozenset[int]:
    if relative != _LEGACY_MIGRATION_MODULE:
        return frozenset()
    return legacy_migration_lines(source)


def test_the_legacy_exemption_is_a_region_not_the_whole_module() -> None:
    """The exemption must cover the migration and nothing else."""
    source = (_SRC / _LEGACY_MIGRATION_MODULE).read_text(encoding="utf-8")
    exempt = legacy_migration_lines(source)
    total = len(source.splitlines())
    assert exempt, "the legacy migration region was not found"
    assert len(exempt) < total // 2, "the exemption swallowed most of the module"


def test_a_vendor_line_outside_the_migration_region_is_not_exempt() -> None:
    """A new vendor reference elsewhere in the module still fails."""
    source = (
        "#: Old provider spellings this migration accepts.\n"
        '_LEGACY_NAMES = {\n    "openai-compat",\n    "azure",\n}\n'
        "\n\n"
        "def _migrate_legacy_agent(raw: dict[str, object]) -> str:\n"
        '    return "azure" if raw.get("provider") == "azure" else "ollama"\n'
        "\n\n"
        "def load_config() -> str:\n"
        '    return "azure"\n'
    )
    exempt = legacy_migration_lines(source)
    offending = [
        number
        for number, line in enumerate(source.splitlines(), start=1)
        if _ADAPTER_VENDOR_RE.search(line) and number not in exempt
    ]
    assert offending == [13], f"expected only the load_config body, got {offending}"


@pytest.mark.parametrize("relative", _VENDOR_GUARDED)
def test_a_guarded_module_never_names_an_llm_vendor(relative: str) -> None:
    """Adapter selection lives in `korvid/providers/`, nowhere else."""
    source = (_SRC / relative).read_text(encoding="utf-8")
    exempt = _vendor_exempt_lines(relative, source)
    offending = [
        f"{relative}:{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), start=1)
        if _ADAPTER_VENDOR_RE.search(line) and number not in exempt
    ]
    report = "\n".join(offending)
    assert offending == [], f"adapter names belong in korvid/providers:\n{report}"
```

Add `import ast` and `import re` to the file's imports. Reuse the file's existing `_SRC`
constant rather than defining a new one. If a *docstring* in a guarded module trips the
guard, rewrite the prose to say "adapter" — do not add the module to
`_VENDOR_OUT_OF_SCOPE`.

**Why the region is computed and not matched.** The AST region built above covers, from
this plan's own Task 2 and Task 3 source text: `LEGACY_PROFILE_NAME`,
`_LEGACY_OPENAI_COMPAT_NAMES`, `_LEGACY_REVIEW_NAMES`, `_LEGACY_OLLAMA_KEYS`,
`_LEGACY_AUTH_METHODS`, `LEGACY_AGENT_KEYS`, `_LEGACY_AUTH_BACK`,
`_ADAPTERS_WITHOUT_LEGACY_TRANSPORT`, `_LegacyAgentScalars`, `_legacy_model_reference`,
`_legacy_auth`, `_legacy_options`, `_legacy_ollama_options`, `_LEGACY_OLLAMA_NUMERIC_KEYS`,
`_legacy_azure_base_url`, `_derive_legacy_scalars`,
`_migrate_azure_endpoint` and `_migrate_legacy_agent`. Every vendor word Tasks 2 and 3 add
to `core/config.py` falls inside one of those nodes — this was checked by running the
helper above over the plan's own code blocks, which reports zero offending lines for them.
The guard as written therefore goes green against the source this plan prescribes, rather
than against source someone has to invent. The two shape tests are what keep that honest:
the first fails if someone widens the region until it covers the module, the second fails
if the region stops being a region. `_canonicalize_provider_name` and `_parse_agent_profiles`
deliberately do **not** match — they survive Task 17, and neither names a vendor.

`korvid/agent/model_catalog.py` moves *out* of the guarded set in this task for the same
class of reason `k8s/csp.py` was never in it. Its `MODEL_CATALOG` is a table of shipped
model capabilities whose rows are keyed by adapter id (`provider="ollama"`); the lookup key
comes from a profile's own `adapter_id(profile.model)`. Guarding it would force korvid to
either delete its knowledge of which models support tool calling or spell the keys in an
obfuscated way, and neither makes adapter *selection* any more neutral. The exclusion is
recorded with that reason in `_VENDOR_OUT_OF_SCOPE`, where the overlap test can see it.
The Global Constraints at the top of this plan name the five guarded modules; keep the two
lists identical.

- [ ] **Step 2: Run the guard to verify it fails**

Run: `uv run pytest -p no:tach tests/test_agent_replacement_guard.py -q`
Expected: FAIL, on exactly these assertions:

- the retired-module check, because `korvid.agent.setup`, `korvid.providers.configurator`
  and `korvid.providers.openai_compat` are all still importable;
- the retired-symbol check, because `KorvidConfig` still carries `agent_provider`,
  `agent_base_url`, `agent_api_key_env`, `agent_auth_method`, `agent_ollama_num_ctx`,
  `agent_options` and `agent_options_error`;
- `test_a_guarded_module_never_names_an_llm_vendor[korvid/core/config.py]`, on eight
  lines outside the exempt migration region — the `agent_ollama_*` field block
  (`config.py:196`), the `network_ca_bundle` docstring that says "Azure"/"OpenAI"
  (`config.py:256`), and six lines belonging to five of the legacy Ollama coercion
  helpers, whose docstrings and one warning string name the `agent.ollama.*` keys they
  parse: `_parse_num_ctx` (`:846`), `_parse_num_predict` (`:869` and its warning at
  `:884`), `_parse_seed` (`:890`), `_parse_temperature` (`:905`) and `_parse_keep_alive`
  (`:917`). The sixth helper, `_parse_positive_int` (`:851`), names no vendor and so does
  not fail the guard — it is deleted in Step 3 anyway, as dead code once `_parse_num_ctx`,
  its sole caller, goes;
- `test_a_guarded_module_never_names_an_llm_vendor[korvid/__main__.py]`, on the five
  remaining legacy regions, four of which Step 5 already removes as part of its own work:
  the `ollama=ollama_options` argument and the deferred
  `from korvid.providers.ollama import OllamaOptions` inside `_create_initial_provider`
  and `_build_agent_wiring`; the
  `oauth = token_store.load("github-oauth") if config.agent_provider == "github-copilot"`
  conditional; the "GitHub Copilot discovery" comment inside the `ProviderConfigurator`
  construction; and the `openai-compat:gpt-4o` example in `_persist_agent_settings`'s
  comment (Task 3 Step 5). The **fifth** is the one no other step touches, and Step 3's
  prose table below is where it is fixed: the provider-ownership comment in `_wire_agent`,
  `# (a GitHub Copilot provider eagerly holds a credential HTTP client).`

Two things that *look* like guard failures in `__main__.py` are not. `_AGENT_INSTALL_HINT`
and `_agent_unavailable_wiring` contain no word in `_ADAPTER_VENDOR_RE` at all — the
install hint says "the embedded agent is enabled (agent.provider in config.yaml)", which
is *stale* after this migration (fixed in Step 5) but is not a vendor leak, and the
unavailable-wiring body names no adapter. Nor does the bare identifier `ollama_options`:
`\bollama\b` does not match inside `ollama_options`, because `_` is a word character —
only the keyword argument `ollama=` does. Run
`rg -n '(?i)\b(openai|anthropic|bedrock|ollama|copilot|azure|aws|gcp|vertex|gemini)\b' src/korvid/__main__.py src/korvid/core/config.py`
before the guard and reconcile its output against the two lists above; a line in neither
means an earlier task was left half-applied, and the fix belongs there rather than here.

`test_a_guarded_module_never_names_an_llm_vendor[korvid/agent/model_profiles.py]`,
`[korvid/ui/agent_ui_controller.py]` and `[korvid/ui/widgets/agent_setup_screen.py]` pass
already: Tasks 5–10 rebuilt those three against catalog descriptors, so their adapter names
now arrive as data at runtime rather than as literals in the source.
`test_the_vendor_guard_and_its_exclusions_do_not_overlap`,
`test_the_legacy_exemption_is_a_region_not_the_whole_module` and
`test_a_vendor_line_outside_the_migration_region_is_not_exempt` also pass from the start —
they constrain the guard's own shape, not the source tree.

Steps 3–5 clear those failures by deleting the `agent_ollama_*` fields and the six coercion
helpers that read them, deleting `_persist_agent_settings` and the `ProviderConfigurator`
wiring, rebuilding `_create_initial_provider` from the active profile, and rewriting the
two comments that no deletion reaches:

| Prose to rewrite | Current text | Replacement |
|---|---|---|
| `core/config.py`, the `network_ca_bundle` docstring (`config.py:256`) | `#: korvid-owned agent HTTPS clients (OpenAI-compatible, native Ollama,` | `#: korvid-owned agent HTTPS clients (every model adapter's transport,` — the next line already reads `#: and the :ai wizard's connection test).` and stays as it is |
| `__main__.py`, the provider-ownership comment in `_wire_agent` (`__main__.py:996`) | `# (a GitHub Copilot provider eagerly holds a credential HTTP client).` | `# (an adapter may eagerly hold a credential HTTP client).` |

Rewrite them exactly as written above rather than paraphrasing: the guard is a substring
match, so "Copilot" or "OpenAI" surviving anywhere on those lines — including inside a
longer sentence that explains why they were removed — fails the same test.

The install hint is a *correctness* fix rather than a guard fix, and it belongs in this
task because the key it names stops existing here. In `__main__.py`, change
`_AGENT_INSTALL_HINT`'s `"the embedded agent is enabled (agent.provider in config.yaml)"`
to `"the embedded agent is enabled (agent.active in config.yaml)"`, and update the
matching docstring in `tests/test_main_wiring.py` that quotes the old key. Nothing asserts
on the hint's exact text — `tests/agent/test_install_hint.py` asserts on the
`korvid[all,entra]==<version>` requirement string, which does not change.

- [ ] **Step 3: Delete the legacy modules and fields**

```bash
git rm src/korvid/agent/setup.py src/korvid/providers/configurator.py src/korvid/providers/openai_compat.py
git rm tests/agent/test_setup.py tests/providers/test_configurator.py tests/providers/test_openai_compat.py
```

`openai_compat.py` also holds `ProviderError`, which two *surviving* files import. Create
`src/korvid/providers/errors.py` (below) in this same step before running anything —
between the `git rm` and Step 4's test run the tree does not import cleanly, and that is
the only window in which it does not.

`tests/ui/test_agent_setup_screen.py` is **not** deleted. Task 10 Step 6 rewrote it against
the profile-driven screen, so it now covers live behaviour; deleting it here would throw
away the coverage that migration just bought.

**`src/korvid/providers/ollama.py` and `tests/providers/test_ollama.py` stay whole.** This
is a deliberate, bounded exception to "one path", and it is *not* a trim: an earlier draft
of this plan said to strip the module to `normalize_base_url`, `OLLAMA_NATIVE_ENDPOINT` and
"the `thinking` round-trip helpers". That instruction was not executable and is withdrawn.
Three facts, each checked against the tree at the time of writing, say why:

- `OLLAMA_NATIVE_ENDPOINT` does not exist. The module's module-level names are
  `_MAX_THINKING_ENTRIES`, `OllamaOptions`, `normalize_base_url`, `OllamaProvider`,
  `_usage_from_chunk` and `_parse_arguments`.
- The `thinking` round-trip is not a free function. `_remember_thinking`
  (`providers/ollama.py:254`) and `prepare_messages` (`:268`) are `OllamaProvider` methods
  over `self._thinking_by_call_id`; there is nothing to keep without keeping the class.
- Five call sites that survive this task consume `OllamaProvider`/`OllamaOptions`
  directly. Deleting the class breaks each of them:

| Surviving consumer | Line | Uses |
|---|---|---|
| `src/korvid/evals/__main__.py` | 52, 79, 82 | `OllamaProvider` as the eval harness's local provider |
| `tests/evals/test_cli.py` | 38, 88 | asserts the harness built an `OllamaProvider` |
| `tests/providers/test_net.py` | 196, 202 | CA-bundle plumbing through `OllamaProvider` |
| `tests/agent/test_offline_local_session.py` | 56, 207, 211 | the offline end-to-end session |
| `tests/agent/test_outbound.py` | 1260, 1262 | outbound-policy checks on a native provider |

The consumers that *do* disappear are exactly the legacy ones, and they go with their
owners: `providers/registry.py:20,45,85,89` and `providers/configurator.py:20` are deleted
modules or deleted functions, `__main__.py:975,983` goes with `_build_agent_wiring`'s
legacy branch, and the `OllamaProvider` assertions in `tests/providers/test_registry.py`
(65, 70, 80, 108, 633, 648–650) and `tests/test_main_wiring.py` (1914–1958) are deleted
together with `create_provider` and the `agent_ollama_*` fields they assert on.

So this task changes exactly one line of `providers/ollama.py` and one of
`tests/providers/test_ollama.py`. Both import `ProviderError` from the module being
deleted (`providers/ollama.py:25`, `tests/providers/test_ollama.py:20`), so that exception
is rehomed first:

```bash
# new leaf module: stdlib only, no provider SDK, importable from anywhere in providers/
cat > src/korvid/providers/errors.py <<'PY'
"""Errors raised by korvid's own provider transports."""

from __future__ import annotations


class ProviderError(Exception):
    """An upstream model endpoint returned something korvid cannot use."""
PY
```

Then repoint the two imports at `korvid.providers.errors` and delete the class from
`openai_compat.py` along with the rest of that module. `tests/providers/test_ollama.py` is
otherwise untouched — its transport tests are the only coverage the native `/api/chat`
route has, and this task does not delete that route.

The behaviour table below is unchanged in substance; only the last row's "where it lives"
column is corrected:

| Legacy `providers/ollama.py` behaviour | Where it lives after this task | Verified |
|---|---|---|
| `num_ctx`, `num_predict` | `ModelSettings["extra_body"]["options"]` (Task 14 `_ollama_extra_body`) | Yes — captured on the wire in `test_ollama_tuning_reaches_the_request_body` |
| `think`, `keep_alive` | `ModelSettings["extra_body"]` top level | Yes — same test |
| `temperature`, `seed` | `ModelSettings` proper | Yes — same test |
| per-tool-call `thinking` round-trip (`_remember_thinking`) | **no OpenAI-compatible equivalent** — the whole `OllamaProvider` is retained | n/a |

The OpenAI chat schema has no assistant-message `thinking` field, so Ollama's
`/v1/chat/completions` route cannot carry the reasoning text back out on a tool call the
way `/api/chat` does.

**The retained module is still reachable through the profile contract**, which is what the
design's delivery group 6 requires — a surviving provider must be *selected* the same way
every other adapter is, not through a second configuration path. The `ollama` adapter
descriptor therefore gains one bounded option field, `native_api` (a boolean, default
false), and `create_profile_provider` honours it. Insert this **immediately above**
`if adapter in BUILTIN_ADAPTERS:` in the Task 14 body — `ollama` *is* a built-in adapter,
so a branch placed after that check would never run:

```python
    if adapter == "ollama" and profile.options.get("native_api") is True:
        # Function-local: keeps `registry.py` importable without the agent
        # extra, which `test_the_registry_does_not_eagerly_import_the_agent_stack`
        # (Task 19) pins.
        from korvid.providers.ollama import OllamaOptions, OllamaProvider

        return OllamaProvider(
            base_url=profile.endpoint or "",
            model=model_tag(profile.model),
            credentials=resolve_credential(profile),
            options=OllamaOptions(**_ollama_native_options(profile.options)),
            ca_bundle=ca_bundle,
        )
```

It sits below the `config_error` refusal and the empty-adapter guard, so a rejected profile
still cannot reach a socket through this route either. `OllamaProvider.__init__` calls
`normalize_base_url` on `base_url` itself, so the caller passes the endpoint through
unchanged; `credentials` and `ca_bundle` are keyword-only there. `resolve_credential` is
imported at the top of `create_profile_provider` (Task 14 Step 5) and is what the plugin
branch below uses too, so an `environment`- or `keyring`-authenticated Ollama endpoint keeps
working on this route.

**This branch takes `create_profile_provider` to a ruff C901 complexity of exactly 10** —
measured, not estimated, against `lint.mccabe.max-complexity = 10`, which passes because
the rule fires on `> 10`. There is no headroom left. The *next* branch anyone adds must
extract a helper first; `_ollama_native_options` is already one, and the built-in and
plugin arms are the natural extraction points.

`_ollama_native_options` maps the already-bounded `profile.options` onto `OllamaOptions`
fields and ignores `native_api` itself; unknown keys are dropped rather than passed
through, because `OllamaOptions(**raw)` on operator-controlled input would be a
`TypeError` at connect time. Concretely:

```python
def _ollama_native_options(options: Mapping[str, object]) -> dict[str, object]:
    """Project profile options onto `OllamaOptions`' own fields.

    `OllamaOptions` is a dataclass, so its field names are the whole
    allow-list; anything else — `native_api`, a stale key, a typo — is
    dropped rather than forwarded into a constructor that would raise
    `TypeError` at connect time.
    """
    from dataclasses import fields

    from korvid.providers.ollama import OllamaOptions

    allowed = {field.name for field in fields(OllamaOptions)}
    return {key: value for key, value in options.items() if key in allowed}
```

Add `native_api` to the `ollama` descriptor's `option_fields` in Task 6 and to
`test_the_ollama_adapter_keeps_every_legacy_tuning_knob`'s expected set in the same task
(both already specified there), and add here:

```python
def test_the_native_ollama_route_is_selected_by_a_profile_option() -> None:
    """The surviving native transport is reached through the profile contract."""
    from korvid.providers.ollama import OllamaProvider

    profile = AgentProfileConfig(
        model="ollama:qwen3:8b",
        endpoint="http://localhost:11434",
        options={"native_api": True, "num_ctx": 16384},
    )
    provider = create_profile_provider(profile)
    assert isinstance(provider, OllamaProvider)


def test_an_ollama_profile_without_the_option_uses_the_common_adapter() -> None:
    from korvid.providers.ollama import OllamaProvider

    profile = AgentProfileConfig(model="ollama:qwen3:8b", endpoint="http://localhost:11434")
    provider = create_profile_provider(profile)
    assert provider is not None
    assert not isinstance(provider, OllamaProvider)
```

**Compilation and import check.** Because the survivors above are enumerated rather than
guessed, pin them so a later trim cannot silently break them:

```python
@pytest.mark.parametrize(
    "module",
    [
        "korvid.providers.ollama",
        "korvid.providers.errors",
        "korvid.evals.__main__",
    ],
)
def test_the_retained_native_modules_still_import(module: str) -> None:
    """Nothing this task deleted was a dependency of the retained route."""
    assert importlib.import_module(module) is not None
```

Add `import importlib` to that test module. Step 4's `uv run pytest -q` covers the test
files in the table by running them; this test covers the `src/` side, which no test would
otherwise import if the eval harness happened to be skipped.

**Deletion criteria (a follow-up, not this branch).** Delete `providers/ollama.py`
when a test against a live Ollama shows that `/v1/chat/completions`
returns the reasoning text for a tool call, or when korvid stops surfacing Ollama reasoning
at all. Record that criterion in the module docstring so the next reader does not have to
rediscover it:

```python
"""Ollama's native `/api/chat` transport.

Everything Ollama shares with the OpenAI dialect goes through
`providers/pydantic_factory.py`, and an `ollama:` profile uses that route
by default. This module is the part the shared route cannot express:
`/api/chat` returns per-tool-call reasoning in a `thinking` field, and the
OpenAI chat schema has no place to put it. A profile opts in with
`options.native_api: true`; there is no second configuration path.

Delete this module once `/v1/chat/completions` is shown to carry that
field, or once korvid no longer surfaces Ollama reasoning.
"""
```

`src/korvid/providers/entra.py` and `tests/providers/test_entra.py` **stay**. Azure's `provider-default` method builds `AsyncAzureOpenAI(azure_ad_token_provider=EntraCredentialSource().access_token)` (Task 14); the module is no longer legacy code, it is the `azure` adapter's Entra implementation.

In `src/korvid/core/config.py`, delete `_LegacyAgentScalars`, `_derive_legacy_scalars`, `_LEGACY_AUTH_BACK`, `save_agent_config`, and every `agent_*` scalar field on `KorvidConfig` except `agent_model_tier`, `agent_rules`, `agent_follow`, `agent_disable_in_protected` and the other non-provider agent settings. That deleted set explicitly includes **`agent_enabled`**, `agent_provider`, `agent_model`, `agent_base_url`, `agent_api_key_env`, `agent_auth_method` and every `agent_ollama_*` field. (`_ADAPTERS_WITHOUT_LEGACY_TRANSPORT` is already gone — Task 14 Step 5 removed it with the interim builder.) `agent_profiles` is the only provider-shaped field that remains. Keep `_migrate_legacy_agent` — reading an old file must keep working; only *writing* the old shape is gone.

Deleting the `agent_ollama_*` fields orphans six module-private coercion helpers whose
**only** call sites are `_derive_legacy_scalars`' six keyword arguments (Task 3 Step 4).
Delete them in the same edit, or they stay behind as dead code whose docstrings are
exactly the `korvid/core/config.py` guard failures listed in Step 2:

| Helper | Line today | What it did |
|---|---|---|
| `_parse_num_ctx` | `config.py:845` | positive int, falling back to `16384` |
| `_parse_positive_int` | `config.py:851` | permissive positive-int coercion; `_parse_num_ctx` is its sole caller |
| `_parse_num_predict` | `config.py:868` | strictly-positive `int`, warning on anything else |
| `_parse_seed` | `config.py:889` | non-negative int, or None |
| `_parse_temperature` | `config.py:904` | finite non-negative float, falling back to `0.0` |
| `_parse_keep_alive` | `config.py:916` | duration string or integer seconds passthrough |

`from math import isfinite` **stays** — `_parse_temperature` was not its only user
(`config.py:543` and `config.py:1086` still call it). Nothing outside `core/config.py`
imports any of the six; the only other mention is the docstring of
`test_ollama_num_ctx_still_accepts_a_numeric_string`
(`tests/core/test_config.py:1100`), which names `_parse_positive_int` as the reason
`num_ctx` is permissive. Step 4 deletes that test — Task 2's
`test_legacy_ollama_numbers_keep_the_old_parser_s_coercion[numeric-string-int]` asserts
the same coercion at the boundary that now performs it — so the stale reference goes
with it rather than being reworded.

Their behaviour is not silently lost. Every value they coerced now arrives through
`_legacy_options` (Task 2), which applies the same coercion for `num_ctx`, `seed` and
`temperature` and the same strict rejection for `num_predict`, and every *fallback* they
applied is the field default on `OllamaOptions` itself — which a
migrated profile still reaches, because `_legacy_options` marks it `native_api: True`
and `_ollama_native_options` drops the keys the profile does not carry. `num_ctx: 16384`,
`temperature: 0.0` and `num_predict: None` therefore still apply to an operator who never
configured them, or who configured them with a value the old parser refused.
`tests/providers/test_ollama.py` (kept whole) is what pins those defaults from here on.

In `src/korvid/providers/registry.py`, delete `create_provider` and `build_credentials`. In
`plugin_registry.py`, delete `OPENAI_COMPAT_ALIASES` — but **keep every name it held
reserved**. `RESERVED_PROVIDER_NAMES` becomes:

```python
#: Names retired with the legacy configuration path. They stay reserved:
#: a third-party plugin registering `openai` or `azure` would shadow a
#: built-in adapter for anyone who still has that word in a config file,
#: and the failure would look like "my Azure profile silently changed
#: behaviour" rather than "a plugin was rejected".
_RETIRED_PROVIDER_ALIASES: Final[frozenset[str]] = frozenset(
    {"openai-compat", "openai", "azure", "vllm", "github", "anthropic", "claude"}
)

RESERVED_PROVIDER_NAMES: Final[frozenset[str]] = (
    frozenset(BUILTIN_ADAPTERS) | _RETIRED_PROVIDER_ALIASES
)
```

with a test that pins it, so the reservation cannot be dropped by accident:

```python
@pytest.mark.parametrize(
    "name", sorted({"openai-compat", "openai", "azure", "vllm", "github", "anthropic", "claude"})
)
def test_a_retired_alias_stays_reserved(name: str) -> None:
    """Retiring an alias must not open it up to plugins."""
    assert name in RESERVED_PROVIDER_NAMES
```

`tests/test_main_wiring.py::_stub_providers` monkeypatches
`korvid.providers.registry.create_provider`; repoint it at
`korvid.providers.registry.create_profile_provider` in the same commit, and change its
`_create(**kwargs)` body to take `(profile, **kwargs)` and read the model from
`model_tag(profile.model)`. Leaving it patching a deleted symbol would make
`monkeypatch.setattr` raise `AttributeError` in every wiring test.

`pyproject.toml` needs no change here: the `entra` extra survives, and Task 11 already added the provider extras. `src/korvid/agent/install_hint.py` keeps its `f"korvid[all,entra]=={__version__}"` requirement string, and the `korvid[all,entra]` expectations in `tests/ui/test_agent_wiring.py` and `tests/agent/test_install_hint.py` stay as they are. Nothing in this step touches the extras.

In `src/korvid/__main__.py`, delete `_persist_agent_settings` and the
`ProviderConfigurator` wiring, and rewrite the three places that still read a legacy
scalar. Each is spelled out, because "update `__main__`" is where a migration like this
usually loses a behaviour:

1. **The install-hint decision is derived from the active profile.** `_agent_unavailable_wiring`
   currently branches on `config.agent_enabled` (`__main__.py:682`) to decide whether a
   missing `[agent]` extra is a hard `SystemExit` or a silent degrade. That field no longer
   exists; the active profile is the same signal, spelled once:

   ```python
       # An operator who configured a profile *asked* for the agent, so a
       # missing extra is a startup failure with an install hint. Nobody
       # who never configured one should be stopped by it.
       if config.agent_profiles.active_profile is not None:
           raise SystemExit(f"korvid: {_AGENT_INSTALL_HINT}")
   ```

   Keep the rest of `_agent_unavailable_wiring` — its parameters
   (`config, missing, ui_proxy, agent_ui_proxy, provider_box, session_box`), its
   `logger.info`, its `_retarget_noop` and its `AgentWiring(...)` — exactly as they are.
   Only the one condition changes.

   `agent.enabled: false` is retired as a concept: `agent.active: null` is the off switch,
   and `_migrate_legacy_agent` already translates the old key into it (Task 2). Task 18
   documents that for operators.

2. **The GitHub OAuth token is loaded unconditionally.** Today
   `token_store.load("github-oauth")` is guarded by
   `if config.agent_provider == "github-copilot"` (`__main__.py:982`) — a vendor test in
   the composition root, which is exactly what this task removes. The composition root
   loads the token and passes it down; the *provider layer* decides whether an adapter
   wants it:

   ```python
       # Loaded for every profile. Which adapters use an OAuth token is a
       # provider-layer question, and `create_profile_provider` answers it
       # from the adapter descriptor. Reading a token that turns out to be
       # unused costs one keyring lookup; deciding here would put adapter
       # knowledge back in the composition root.
       oauth = token_store.load("github-oauth")
   ```

   The key `"github-oauth"` is a *credential store key*, not an adapter id, and does not
   match `_ADAPTER_VENDOR_RE` (neither `github` nor `oauth` is in the alternation), so the
   guard stays green with it in place. `create_profile_provider` already takes
   `oauth_token` (Task 14) and ignores it for adapters whose descriptor declares no
   `device-login` method.

3. **The provider is built from the profile.** Replace `_create_initial_provider`'s body —
   which today unpacks five legacy scalars plus `OllamaOptions` into `create_provider` —
   with the profile call, and delete the `OllamaOptions` construction above it along with
   the `agent_ollama_*` fields it reads:

   ```python
   def _create_initial_provider(
       config: KorvidConfig,
       oauth: str | None,
       plugin_registry: Any,
       startup_warnings: list[str] | None,
   ) -> LLMProvider | None:
       """Build the initial LLM provider from the active profile."""
       from korvid.providers.plugin_registry import ProviderPluginError
       from korvid.providers.registry import create_profile_provider

       profile = config.agent_profiles.active_profile
       if profile is None:
           return None
       try:
           return create_profile_provider(
               profile,
               oauth_token=oauth,
               ca_bundle=config.network_ca_bundle,
               plugin_registry=plugin_registry,
           )
       except ProviderPluginError as exc:
           # Unchanged from the legacy body: a plugin failure at startup
           # degrades to a warning, and the TUI stays usable with
           # provider=None so `:ai` can reconfigure.
           if startup_warnings is not None:
               startup_warnings.append(f"Provider plugin failed: {exc}")
           logger.warning("provider plugin failed at startup: %s — agent disabled", exc)
           return None
   ```

   The signature loses only `ollama_options`; `startup_warnings` and the
   `ProviderPluginError` handler stay exactly as they are, because
   `create_profile_provider` still raises that one exception by design (Task 14). Its
   *other* refusals return `None` after logging — including the `config_error` refusal —
   which is why there is no second `except` here and why "the agent is disabled" is always
   reachable rather than a traceback.

   At the call site in `_build_agent_wiring`, delete the six-field `OllamaOptions(...)`
   construction and drop `from korvid.providers.ollama import OllamaOptions` and
   `from korvid.providers.registry import create_provider` from the deferred import block.
   Also drop `from korvid.providers.configurator import ProviderConfigurator` and pass
   `_persist_agent_profiles` plus the catalog to the controller in its place. The
   `token_store = TokenStore()` line stays — it is still needed for the OAuth load in (2),
   and `create_profile_provider` resolves credentials itself.

Pin (1) and (2) with wiring tests in `tests/test_main_wiring.py`, following that file's
existing convention of `cast("Any", object())` for the unused `kube` argument:

```python
def test_a_configured_profile_without_the_extra_is_a_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who configured a profile asked for the agent.

    This is the `agent_enabled` branch's replacement, so it has to keep
    failing loudly rather than degrading to "unavailable".
    """
    import korvid.__main__ as main_module

    monkeypatch.setattr(main_module, "_missing_extra_packages", lambda roots: ["pydantic_ai"])
    config = KorvidConfig(
        agent_profiles=AgentProfilesConfig(
            active="local", profiles={"local": AgentProfileConfig(model="ollama:llama3")}
        )
    )
    with pytest.raises(SystemExit, match="korvid:"):
        main_module._build_agent_wiring(config, cast("Any", object()), {})


def test_no_active_profile_degrades_instead_of_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`agent.active: null` is the off switch; there is no `enabled` field."""
    import korvid.__main__ as main_module

    monkeypatch.setattr(main_module, "_missing_extra_packages", lambda roots: ["pydantic_ai"])
    wiring = main_module._build_agent_wiring(
        KorvidConfig(agent_profiles=AgentProfilesConfig()), cast("Any", object()), {}
    )
    assert wiring.session is None
    assert wiring.rebuild is None


def test_the_oauth_token_is_loaded_for_every_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Which adapter wants a token is the provider layer's decision."""
    import korvid.__main__ as main_module

    loaded: list[str] = []
    monkeypatch.setattr(
        "korvid.providers.token_store.TokenStore.load",
        lambda self, key: loaded.append(key) or None,
    )
    _stub_providers(monkeypatch)
    config = KorvidConfig(
        agent_profiles=AgentProfilesConfig(
            active="local", profiles={"local": AgentProfileConfig(model="ollama:llama3")}
        )
    )
    main_module._build_agent_wiring(config, cast("Any", object()), {})
    assert "github-oauth" in loaded
```

`_stub_providers(...)` is the helper this file already has at line 3001 (Task 7 repointed it
at `create_profile_provider`); `KorvidConfig(...)` constructed inline and
`cast("Any", object())` for the unused `kube` argument are this file's existing
conventions. `cast` and `pytest` are already imported there; add `AgentProfileConfig` and
`AgentProfilesConfig` to its `korvid.core.config` import.

- [ ] **Step 4: Retire the three test files the legacy scalars were pinned in**

Task 3 Step 6 kept `tests/core/test_config.py` honest while the scalars still existed;
this step removes the scalars, so those same tests now have nothing to assert. Two more
files break for a different reason. Work through them in this order — the rules are
exact, and anything they do not cover is a real regression, not a test to delete.

**`tests/core/test_config.py`.** Every legacy scalar is gone from `KorvidConfig`, so a
test that reads one no longer compiles as a meaningful assertion. Retarget with this
table rather than deleting:

| Deleted field | Assert instead |
|---|---|
| `cfg.agent_provider` + `cfg.agent_model` | `cfg.agent_profiles.active_profile.model == "<adapter>:<tag>"` |
| `cfg.agent_base_url` | `…active_profile.endpoint` |
| `cfg.agent_api_key_env` | `…active_profile.auth.settings["key"]` |
| `cfg.agent_auth_method` | `…active_profile.auth.method` (`api_key`→`environment`, `entra`→`provider-default`, `device-login` and `none` unchanged) |
| `cfg.agent_enabled is True/False` | `cfg.agent_profiles.active_profile is not None` / `is None` |
| `cfg.agent_options` | `…active_profile.options` |
| `cfg.agent_options_error` | `…active_profile.config_error` |
| `cfg.agent_ollama_<knob>` | `…active_profile.options["<knob>"]` |

Two of those rows are not pure renames and must be stated explicitly, because the
retirement guard added in Step 1 lists `agent_provider`, `agent_base_url`,
`agent_api_key_env`, `agent_auth_method` and `agent_ollama_num_ctx` but *not*
`agent_options`/`agent_options_error` — they go with the same commit and for the same
reason (they are the provider-shaped half of `KorvidConfig`, replaced by
`profile.options` and `profile.config_error`), so add both names to `_RETIRED_SYMBOLS`
in Step 1 as well:

- `agent_options` becomes `profile.options`, which for a migrated Ollama profile also
  carries `native_api: True`. The `test_agent_options_*` fixtures already use
  `openai-compat` after Task 3 Step 6, so their exact-mapping assertions transfer
  unchanged apart from the attribute path.
- `agent_options_error` becomes `profile.config_error`, whose message root is
  `"options"` rather than `"agent.options"`. Every assertion in these tests is a
  substring check, so this is invisible — do not tighten them.

The seventeen `test_ollama_*` cases split cleanly, and the split is worth doing by name
because fourteen of them are deletions and a plan that says "delete some tests" is how
coverage quietly leaves.

**Delete fourteen**, each because Task 2 already asserts the same value at the boundary
that now owns it:

| Deleted | Superseded by |
|---|---|
| `…_defaults_when_unconfigured` | `tests/providers/test_ollama.py` (the `OllamaOptions` field defaults) |
| `…_invalid_values_fall_back` | Task 2 `…_dropped_with_a_warning[not-a-number]`, `[wrong-shape]` |
| `…_inf_and_overflow_values_fall_back` | Task 2 `…_dropped_with_a_warning[non-finite]` |
| `…_seed_zero_is_valid` | Task 2 `…_old_parser_s_coercion[zero-seed-is-not-absent]` |
| `…_negative_seed_falls_back` | `tests/providers/test_ollama.py` (`seed` default `None`) |
| `…_num_ctx_still_accepts_a_numeric_string` | Task 2 `…_old_parser_s_coercion[numeric-string-int]` |
| `…_num_predict_default_is_none` | `tests/providers/test_ollama.py` (`num_predict` default `None`) |
| `…_num_predict_parsed` | Task 2 `…_old_parser_s_coercion[strict-int-kept]` |
| `…_num_predict_valid_value_has_no_warning` | Task 2 `…_old_parser_s_coercion[strict-int-kept]` |
| `…_num_predict_invalid_falls_back` | Task 2 `…_dropped_with_a_warning[strict-int-refuses-non-positive]` |
| `…_num_predict_zero_falls_back_with_warning` | Task 2 `…_dropped_with_a_warning[strict-int-refuses-non-positive]` |
| `…_num_predict_bool_falls_back_with_warning` | Task 2 `…_dropped_with_a_warning` (the `bool` branch) |
| `…_num_predict_float_falls_back_with_warning` | Task 2 `…_dropped_with_a_warning[strict-int-refuses-a-fraction]` |
| `…_num_predict_numeric_string_falls_back_with_warning` | Task 2 `…_dropped_with_a_warning[strict-int-refuses-a-numeric-string]` |

Before deleting each one, open the named replacement and confirm it asserts the same
effective value. A fallback test may be deleted only because something else already
proves the fallback — never because the field it read no longer exists.

**Retarget the remaining three**, which assert something Task 2 does not:

- `test_ollama_options_parsed` — the only case that asserts the whole block at once.
  Becomes `profile.options == {"num_ctx": 32768, "temperature": 0.7, "seed": 42,
  "think": True, "keep_alive": "10m", "native_api": True}`. Keep `native_api` in the
  expected mapping: it is the migration's promise that an existing install keeps the
  `/api/chat` transport it was already running.
- `test_ollama_keep_alive_accepts_integer_seconds` — Task 2 only covers the duration
  *string* form. Becomes `profile.options["keep_alive"] == 300`.
- `test_ollama_non_mapping_section_is_ignored` — the only case that pins the
  `ollama: not-a-mapping` branch. Becomes `"num_ctx" not in profile.options`.

**Do not delete** the fifteen `test_save_agent_config_*` cases. They are the only tests
of the *file safety* rules — atomic replace, `fsync` before `os.replace`, preserving a
restrictive `0600` mode, refusing to clobber a foreign `.tmp`, preserving unrelated and
extension keys — and none of that is provider-shaped. Re-point them at
`save_agent_profiles` (Task 3) and, where they asserted a written legacy key, assert the
equivalent key under `agent.profiles.<name>`. The four tier cases
(`…_writes_an_explicit_low_tier`, `…_writes_an_explicit_high_tier`,
`…_removes_tier_for_automatic_routing`, `…_preserves_unrelated_keys_and_rules`) transfer
directly: `save_agent_profiles` keeps the `model_tier` keyword.

**`tests/ui/test_agent_wiring.py`.** This file breaks *twice*, and the plan splits the
work accordingly:

- At **Task 10**, when `AgentUiController.__init__` loses its `configurator` parameter.
  Task 10 Step 7 already runs all of `tests/ui/`, so that task fixes the construction
  sites there; do not defer them to here.
- At **this task**, for the 42 references to `AgentSettings` and `AgentConfigurator`
  (imported locally inside the test bodies, e.g. at `:387` and `:416`). Replace each
  `AgentSettings(...)` construction with the `AgentProfileConfig(...)` the wiring now
  takes, and each `AgentConfigurator` double with the profile-writer callable. A test
  whose entire subject was "the configurator is asked to save" becomes "the profile
  writer is asked to save" — same assertion, new collaborator — not a deletion.

**`tests/ui/test_impact_security.py`.** One reference, at
`test_impact_preview_works_with_the_agent_disabled:376`:
`assert env.app.config.agent_enabled is False`. Replace it with
`assert env.app.config.agent_profiles.active_profile is None`. The test's subject — the
impact preview is a deterministic graph query that works with no LLM at all — is
unchanged, and it is a security test, so it stays.

Run the three files together before the full suite, because they are the ones this step
touches:

```bash
uv run pytest -p no:tach tests/core/test_config.py tests/ui/test_agent_wiring.py tests/ui/test_impact_security.py -q
```

Expected: PASS. Then narrow to the sections most likely to have been missed:

```bash
uv run pytest -p no:tach tests/core/test_config.py -q -k "agent or ollama or save"
```

Expected: PASS, with a non-zero collected count — a `-k` filter that collects nothing
means the retarget deleted more than it moved.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. `tests/providers/test_registry.py` will still exercise the deleted `create_provider`/`build_credentials`: delete those test functions (the profile path is covered by `tests/providers/test_pydantic_factory.py` from Task 14) and keep whatever else that file asserts. Fix any remaining import of a deleted module by moving its caller onto the profile API — do not reintroduce a shim.

- [ ] **Step 6: Lint, typecheck, layer check, dependency check and commit**

```bash
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
uv run mypy src/
uv run tach check
uv run deptry .
git add -A
git commit -m "refactor: delete the legacy provider configuration path" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 18: Documentation and migration notes

**Files:**
- Modify: `docs/agent.md`, `docs/provider-plugins.md`, `docs/airgap.md`, `docs/threat-model.md`, `docs/release-notes/unreleased.md`, `README.md`
- Modify: `tests/test_docs_agent_contracts.py`

**Interfaces:**
- Consumes: the shipped behaviour from Tasks 1–17.
- Produces: operator-facing documentation whose claims the docs contract test can verify.

- [ ] **Step 1: Write the failing documentation contract test**

Append to `tests/test_docs_agent_contracts.py`:

```python
def test_agent_docs_document_the_profile_shape() -> None:
    text = (_DOCS / "agent.md").read_text(encoding="utf-8")
    assert "agent:\n  active:" in text
    assert "profiles:" in text
    assert "provider:model" in text
    for method in ("none", "environment", "keyring", "provider-default", "device-login"):
        assert method in text


def test_agent_docs_no_longer_document_the_legacy_keys() -> None:
    """Legacy keys may appear only under the migration heading."""
    text = (_DOCS / "agent.md").read_text(encoding="utf-8")
    heading = "### Migrating from the pre-0.4 configuration"
    body, separator, migration = text.partition(heading)
    assert separator == heading, "agent.md must keep the migration section"
    assert migration.strip(), "the migration section must not be empty"
    for retired in ("agent.provider:", "api_key_env:", "openai-compat"):
        assert retired not in body, f"{retired} is documented as if it were current"


def test_agent_docs_list_the_provider_extras() -> None:
    text = (_DOCS / "agent.md").read_text(encoding="utf-8")
    for extra in ("provider-openai", "provider-anthropic", "provider-google", "provider-bedrock"):
        assert extra in text


def test_release_notes_describe_the_migration() -> None:
    text = (_DOCS / "release-notes" / "unreleased.md").read_text(encoding="utf-8")
    assert "model connection profiles" in text
    assert "read without changes" in text
    # The Azure migration is the one that can silently break a
    # deployment if it is documented wrong, so pin it.
    assert "azure:" in text
    assert "api-key" in text
    assert "entra" in text.lower()
    assert "azure_deployment" in text


def test_agent_docs_map_every_adapter_to_its_extra() -> None:
    from korvid.providers.adapter_extras import ADAPTER_EXTRAS

    text = (_DOCS / "agent.md").read_text(encoding="utf-8")
    for adapter, entry in ADAPTER_EXTRAS.items():
        assert adapter in text, f"agent.md never mentions the {adapter} adapter"
        assert entry.extra in text, f"agent.md never mentions the {entry.extra} extra"


def test_agent_docs_explain_the_azure_endpoint_rewrite() -> None:
    """The one migration that changes a value the operator wrote down.

    Every other legacy key is carried over verbatim. Azure's `base_url`
    is not: a deployment-scoped URL is split into a resource endpoint plus
    `options.azure_deployment`, and an operator who reads the new file
    without that explanation will think korvid lost their deployment.
    """
    text = (_DOCS / "agent.md").read_text(encoding="utf-8")
    assert "azure_deployment" in text
    assert "/openai/deployments/" in text


def test_agent_docs_document_the_retirement_of_agent_enabled() -> None:
    """`agent.enabled` is gone and `agent.active` replaced it.

    An operator who upgrades and finds no `enabled` key has to be told
    both that it was removed and what turns the agent off now, or the
    only way to find out is to read the parser.
    """
    text = (_DOCS / "agent.md").read_text(encoding="utf-8")
    heading = "### Migrating from the pre-0.4 configuration"
    _, separator, migration = text.partition(heading)
    assert separator == heading
    assert "agent.enabled" in migration, "the retired key is never named"
    assert "active: null" in migration, "the replacement off switch is not shown"


def test_agent_enabled_is_not_documented_as_a_current_key() -> None:
    """It may be named only where it is explained as retired.

    `docs/agent.md` currently says "If `agent.enabled` is set but the extra
    is missing…" under *Installing the agent*, far from any migration
    note. That sentence has to be rewritten, not just supplemented.
    """
    text = (_DOCS / "agent.md").read_text(encoding="utf-8")
    heading = "### Migrating from the pre-0.4 configuration"
    body, _, _ = text.partition(heading)
    assert "agent.enabled" not in body
    assert "enabled:" not in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -p no:tach tests/test_docs_agent_contracts.py -q`
Expected: FAIL — `assert "agent:\n  active:" in text`.

- [ ] **Step 3: Rewrite the operator documentation**

In `docs/agent.md`, replace the configuration section with:

````markdown
## Model connection profiles

korvid connects to a model through a named **profile**. A profile names a
model as `provider:model`, an optional endpoint, an auth method, and any
provider options:

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
```

`agent.active` names the profile korvid uses. Profile names may contain
letters, digits, `.`, `_` and `-`, up to 100 characters, and are matched
exactly — `prod-east` and `prod_east` are different profiles.

### Auth methods

| Method | Meaning | Settings |
| --- | --- | --- |
| `none` | No credential is sent | — |
| `environment` | Read the key from an environment variable | `key`: the variable name |
| `keyring` | Read the key from the OS keyring | `key`: the entry name |
| `provider-default` | Use the provider SDK's own credential chain | — |
| `device-login` | Interactive sign-in run by `:ai` | — |

Secrets are never written to `config.yaml`: a profile stores only the
*name* of the variable or keyring entry that holds one.

### Adapters and their extras

The base install talks to no provider. Install the extra for the vendors
you connect to:

| Adapter | Extra | Install |
| --- | --- | --- |
| `openai` | `provider-openai` | `uv tool install "korvid[agent,provider-openai]"` |
| `azure` | `provider-openai` | `uv tool install "korvid[agent,provider-openai]"` |
| `ollama` | `provider-openai` | `uv tool install "korvid[agent,provider-openai]"` |
| `github-copilot` | `provider-openai` | `uv tool install "korvid[agent,provider-openai]"` |
| `anthropic` | `provider-anthropic` | `uv tool install "korvid[agent,provider-anthropic]"` |
| `google` | `provider-google` | `uv tool install "korvid[agent,provider-google]"` |
| `bedrock` | `provider-bedrock` | `uv tool install "korvid[agent,provider-bedrock]"` |

`azure`, `ollama` and `github-copilot` all speak the OpenAI wire format,
so one extra covers them. An adapter whose extra is missing is shown in
`:ai` with an install hint and cannot be selected. korvid never
substitutes a different provider.

### Azure OpenAI

`azure` is its own adapter, not an `openai` profile with a different
endpoint. It sends the raw `api-key` header Azure OpenAI expects:

```yaml
    azure-prod:
      model: azure:gpt-4o
      endpoint: https://contoso.openai.azure.com
      auth:
        method: environment
        key: AZURE_OPENAI_API_KEY
      options:
        api_version: "2024-10-21"
        azure_deployment: gpt-4o-prod
```

`endpoint` is the **resource** URL — `https://<resource>.openai.azure.com`
— not a deployment URL. The deployment goes in
`options.azure_deployment`; leave it out and korvid uses the model name.
Writing the deployment into `endpoint` produces a doubled path such as
`/openai/deployments/gpt-4o/openai/deployments/gpt-4o/chat/completions`,
which is why korvid splits an old deployment-scoped `base_url` for you
during migration and logs a warning saying so.

`endpoint` is required — korvid never falls back to
`AZURE_OPENAI_ENDPOINT`, because the profile is the record of which
resource the agent is allowed to talk to. `auth.method` must be
`environment`, `keyring` or `provider-default`; `none` is refused, since
an Azure OpenAI deployment cannot accept an unauthenticated request and
failing at startup is more useful than a 401 mid-conversation. With
`provider-default` korvid signs requests with an Entra ID token instead
of a key; that path needs the `entra` extra
(`uv tool install "korvid[agent,provider-openai,entra]"`).

### Amazon Bedrock

Bedrock has no `endpoint`: the region *is* the endpoint, so it is a
required option.

```yaml
    bedrock-prod:
      model: bedrock:anthropic.claude-3-5-sonnet-20241022-v2:0
      auth:
        method: provider-default
      options:
        region_name: us-east-1
```

`provider-default` is Bedrock's only auth method — credentials come from
the AWS SDK's own chain (environment, shared config, instance role).
`network.ca_bundle` does **not** apply to this adapter; botocore takes
its trust from `AWS_CA_BUNDLE`.

### Migrating from the pre-0.4 configuration

An older `agent.provider` / `agent.model` / `agent.base_url` file is
**read without changes** — korvid migrates it in memory into a profile
named `default`. The file is rewritten in the new shape the first time
you save from `:ai`. There is no separate migration command.

| Legacy `agent.provider` | Migrated profile |
| --- | --- |
| `openai` | `openai:<model>` |
| `azure` | `azure:<model>`; `api_version` preserved; a deployment-scoped `base_url` is split into the resource `endpoint` plus `options.azure_deployment` (see below) |
| `ollama` | `ollama:<model>` with the same `base_url` as `endpoint`, plus `options.native_api: true` so the connection keeps the native `/api` transport it already used |
| `github-copilot` | `github-copilot:<model>` |
| `openai-compat` and its aliases | `openai:<model>` with the same `base_url` as `endpoint` |

Only the `azure` row changes a value you wrote down, and it is the one
case where leaving the value alone would break the connection. korvid
recognises both shapes an older config could hold:

| Legacy `agent.base_url` | Migrated `endpoint` | Migrated `options.azure_deployment` |
| --- | --- | --- |
| `https://contoso.openai.azure.com` | unchanged | absent — the model name is used |
| `https://contoso.openai.azure.com/openai/deployments/gpt-4o-prod` | `https://contoso.openai.azure.com` | `gpt-4o-prod` |
| `https://contoso.openai.azure.com/openai/v1` | `https://contoso.openai.azure.com` | absent — the model name is used |

Each rewrite is reported as a startup warning naming the old and new
values, so the change is never silent. Any other path is left untouched
and warned about rather than guessed at.

#### `agent.enabled` is retired

Older configurations had a separate `agent.enabled: false` switch. It is
gone. **`agent.active` is now the off switch**: a config with no
`agent.active`, or with `active: null`, has no active profile and the
agent is off. Keeping both keys would let them disagree — an enabled
agent with no profile, or a disabled one with a perfectly good profile —
and only one of the two could win.

```yaml
# The agent is off. The profiles are kept, not deleted, so `:ai` can
# turn one back on without you retyping it.
agent:
  active: null
  profiles:
    production:
      model: anthropic:claude-sonnet-4-5
```

A legacy `agent.enabled: false` is honoured on read: the migration
produces the `default` profile from your other keys but leaves
`agent.active` empty, so the agent stays off exactly as before. The
first save from `:ai` rewrites the file without `agent.enabled`. A
legacy `agent.enabled: true` with no usable `agent.model` also leaves
`agent.active` empty and warns, because "on" was never sufficient on its
own.
````

Update `docs/provider-plugins.md` to describe descriptor-based catalog registration and delete the built-in alias list; note in `docs/airgap.md` that provider extras must be pre-installed; note in `docs/threat-model.md` that Pydantic AI instrumentation is disabled and no prompt or secret reaches an exporter.

**Also rewrite the one sentence outside the configuration section.**
`docs/agent.md` line 38, under *Installing the agent*, reads "If `agent.enabled` is set
but the extra is missing, startup fails with the exact install command instead of silently
disabling the agent." That names a key this change deletes, and it is not in the section
Step 3 replaces — `test_agent_enabled_is_not_documented_as_a_current_key` fails until it is
fixed. Replace it with:

```markdown
If `agent.active` names a profile whose provider extra is missing, startup
fails with the exact install command instead of silently disabling the
agent.
```

Add to `docs/release-notes/unreleased.md`:

```markdown
### Changed

- The agent is now configured through named **model connection profiles**
  (`agent.active` / `agent.profiles`). Existing configurations are **read
  without changes** and migrated in memory into a `default` profile; the
  file is rewritten in the new shape the first time you save from `:ai`.
- Model transport moved to the Pydantic AI model layer. Per-vendor SDKs
  ship in the new `provider-openai`, `provider-anthropic`,
  `provider-google` and `provider-bedrock` extras.
- The `:ai` wizard is driven by adapter descriptors: it no longer has a
  hardcoded provider list, and third-party adapters configure through the
  same path as built-in ones.

### Removed

- The `agent.provider` / `agent.base_url` / `agent.api_key_env` /
  `agent.auth_method` scalars and the `openai-compat` provider aliases.
  A profile's `provider:model` string plus `endpoint` replaces all of
  them.
- `agent.enabled`. `agent.active` is the off switch: with no active
  profile the agent is off. A legacy `agent.enabled: false` is still
  honoured on read — the migration builds the `default` profile but
  leaves `agent.active` empty — and the key disappears the first time
  `:ai` saves.

### Migration

- `provider: azure` becomes an `azure:<model>` profile. Azure keeps its
  own adapter and its own authentication: an API key travels in the
  `api-key` header, and `auth.method: provider-default` still uses Entra
  ID (the `entra` extra is unchanged). Azure is **not** re-pointed at the
  generic `openai` adapter.
- `provider: openai-compat` (and its aliases) becomes an
  `openai:<model>` profile with the same `endpoint`. Those alias names
  stay reserved, so a plugin cannot claim one.
- A deployment-scoped Azure `base_url` is split into the resource
  `endpoint` plus `options.azure_deployment`, with a startup warning
  naming both values. This is the only migration that changes a
  configured value, and it is required: the Azure SDK appends the
  deployment path itself, so the old URL would be doubled.
```

- [ ] **Step 4: Run the documentation tests**

Run: `uv run pytest -p no:tach tests/test_docs_agent_contracts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs README.md tests/test_docs_agent_contracts.py
git commit -m "docs: document model connection profiles and provider extras" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 19: Import graph, layer boundaries and the full gate

**Files:**
- Modify: `tests/test_optional_extras.py`
- Modify: `src/korvid/providers/pydantic_factory.py`, `src/korvid/providers/adapter_catalog.py`, `src/korvid/__main__.py` (move imports off `korvid.core` / off module scope)
- **Not modified:** `tach.toml` — `korvid.providers` already declares `depends_on = ["korvid.agent"]`, which is the boundary this task enforces. Step 4 proves that rule is live.

**Interfaces:**
- Consumes: every module Tasks 1–18 created.
- Produces: an enforced guarantee that the base install imports no provider SDK and that no layer rule was widened.

- [ ] **Step 1: Write the import-graph and layer guards**

These are *regression guards*, and this step is explicit about what that means. A
regression guard is written against a tree that already satisfies it — the tree is clean
because Tasks 1–18 kept it clean, not because the guard is doing nothing. Asserting a RED
run on a pristine tree here would be a lie, and worse, it would push whoever executes this
plan to weaken the guard until it turns red for a reason nobody intended. **Step 3 proves
each guard is load-bearing by mutation instead**, which is the honest test of a regression
guard: break the invariant, watch the guard catch it, restore the tree.

In `tests/test_optional_extras.py`, widen the watched-module tuples and add the layer
assertions. The file already defines `_MCP_MODULES`, `_AGENT_MODULES`, `_PROBE` and
`_assert_import_is_extra_free`; extend them rather than duplicating:

```python
#: `httpx2` is the client flavour Pydantic AI 2.35.3 and the OpenAI SDK
#: use; it ships in `[agent]` and must not reach a base install either.
_AGENT_MODULES = ("httpx", "httpx2", "keyring", "pydantic_ai")
#: Vendor SDKs, one per `provider-*` extra.
_PROVIDER_SDK_MODULES = ("openai", "anthropic", "google.genai", "boto3")

_SRC = Path(__file__).resolve().parents[1] / "src"


def _assert_import_is_extra_free(module: str) -> None:
    watched = _MCP_MODULES + _AGENT_MODULES + _PROVIDER_SDK_MODULES
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module, watched=watched)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


#: Watched module → the distribution that provides it. Presence is
#: checked through `importlib.metadata`, which reads metadata and imports
#: nothing: `importlib.util.find_spec("google.genai")` would import the
#: `google` namespace package as a side effect, which is exactly the kind
#: of accidental import this file exists to forbid.
_PROVIDER_SDK_DISTRIBUTIONS = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google.genai": "google-genai",
    "boto3": "boto3",
}


@pytest.mark.parametrize("module", _PROVIDER_SDK_MODULES)
def test_every_provider_sdk_is_installed_in_the_dev_environment(module: str) -> None:
    """The probe proves nothing about an SDK that is absent entirely.

    `uv sync --frozen --dev --all-extras` installs every one of them, so a
    missing distribution means the guard above is vacuous rather than
    satisfied.
    """
    distribution = _PROVIDER_SDK_DISTRIBUTIONS[module]
    try:
        importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - environment error
        pytest.fail(f"{distribution} is not installed; run `uv sync --frozen --dev --all-extras`")
```

Then the layer guards. These parse the file and inspect `import` statements, rather than
searching the text: `src/korvid/providers/registry.py` already says "this module never
imports korvid.core" *in its docstring*, and a substring guard would fail on that sentence
— a false positive that teaches the next reader to delete the guard.

```python
def _imported_roots(path: Path) -> set[str]:
    """Dotted module names this file imports, from its AST.

    Covers both `import a.b` and `from a.b import c`, at any nesting
    depth, so a function-local import is caught exactly like a
    module-scope one. Prose in a docstring is not an import and is
    correctly ignored.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module)
    return roots


def _imports_any(path: Path, prefixes: tuple[str, ...]) -> list[str]:
    return sorted(
        name
        for name in _imported_roots(path)
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)
    )


@pytest.mark.parametrize(
    "path", sorted((_SRC / "korvid" / "core").rglob("*.py")), ids=lambda p: p.name
)
def test_core_imports_no_provider_module(path: Path) -> None:
    offending = _imports_any(path, ("korvid.providers", "pydantic_ai"))
    assert offending == [], f"{path.name} imports {offending}"


@pytest.mark.parametrize(
    "path", sorted((_SRC / "korvid" / "ui").rglob("*.py")), ids=lambda p: p.name
)
def test_the_ui_imports_no_provider_module(path: Path) -> None:
    """The UI talks to adapters through the catalog, never directly."""
    offending = _imports_any(path, ("korvid.providers", "pydantic_ai"))
    assert offending == [], f"{path.name} imports {offending}"


@pytest.mark.parametrize(
    "path", sorted((_SRC / "korvid" / "providers").rglob("*.py")), ids=lambda p: p.name
)
def test_providers_import_the_shared_vocabulary_from_the_agent_layer(path: Path) -> None:
    """`korvid.providers` may depend on `korvid.agent`, not on `korvid.core`.

    `AgentProfileConfig` and friends are re-exported from
    `korvid.agent.model_profiles` (Task 5) precisely so this holds.
    """
    offending = _imports_any(path, ("korvid.core",))
    assert offending == [], f"{path.name} imports {offending}"


def test_the_registry_does_not_eagerly_import_the_agent_stack() -> None:
    """`registry.py` is on the base install's import path.

    A module-scope `from korvid.providers.pydantic_factory import ...`
    here would pull `pydantic_ai` and `httpx2` into every `korvid`
    invocation, including one installed without `[agent]`.
    """
    tree = ast.parse(
        (_SRC / "korvid" / "providers" / "registry.py").read_text(encoding="utf-8")
    )
    module_scope = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module}
    assert "korvid.providers.pydantic_factory" not in module_scope
```

Add `import ast`, `import importlib.metadata` and `from pathlib import Path` to the file's
imports. The existing `test_base_import_has_no_optional_extras`-style parametrisations keep
calling `_assert_import_is_extra_free`, so widening the tuple widens every one of them at
once.

- [ ] **Step 2: Run the tests**

Run: `uv run pytest -p no:tach tests/test_optional_extras.py -q`
Expected: PASS. This is the expected and correct result — see Step 1. If anything here
fails, it has found a real violation introduced by Tasks 1–18; fix the offending import,
never the guard.

- [ ] **Step 3: Prove every guard is load-bearing (mutation)**

This step is the RED half of Step 1's guards. Each mutation is one appended line followed
by a `git checkout`; every mutation is mandatory and each has exactly one expected outcome.
Run them one at a time — a tree carrying two mutations tells you nothing about either.

Mutation A — the providers/core layer guard:

```bash
printf '\nfrom korvid.core.config import KorvidConfig  # mutation\n' \
  >> src/korvid/providers/pydantic_factory.py
uv run pytest -p no:tach tests/test_optional_extras.py -q
```
Expected: FAIL — `test_providers_import_the_shared_vocabulary_from_the_agent_layer[pydantic_factory.py]`
reports `pydantic_factory.py imports ['korvid.core.config']`.

```bash
git checkout -- src/korvid/providers/pydantic_factory.py
uv run pytest -p no:tach tests/test_optional_extras.py -q
```
Expected: PASS.

Mutation B — the base-install import guard:

```bash
printf '\nfrom korvid.providers.pydantic_factory import build_model  # mutation\n' \
  >> src/korvid/providers/registry.py
uv run pytest -p no:tach tests/test_optional_extras.py -q
```
Expected: FAIL, on two tests: `test_the_registry_does_not_eagerly_import_the_agent_stack`,
and the base-install probe, which now finds `pydantic_ai` and `httpx2` in `sys.modules`
after importing `korvid.__main__`.

```bash
git checkout -- src/korvid/providers/registry.py
uv run pytest -p no:tach tests/test_optional_extras.py -q
```
Expected: PASS.

Mutation C — the UI layer guard:

```bash
printf '\nfrom korvid.providers.registry import create_profile_provider  # mutation\n' \
  >> src/korvid/ui/agent_ui_controller.py
uv run pytest -p no:tach tests/test_optional_extras.py -q
```
Expected: FAIL — `test_the_ui_imports_no_provider_module[agent_ui_controller.py]` reports
`agent_ui_controller.py imports ['korvid.providers.registry']`.

```bash
git checkout -- src/korvid/ui/agent_ui_controller.py
uv run pytest -p no:tach tests/test_optional_extras.py -q
```
Expected: PASS.

Mutation D — tach:

```bash
uv run tach check
```
Expected: PASS (exit 0).

```bash
printf '\nfrom korvid.core.config import KorvidConfig  # mutation\n' \
  >> src/korvid/providers/registry.py
uv run tach check
```
Expected: FAIL (non-zero exit) — tach reports that `korvid.providers` cannot import
`korvid.core`, because `korvid.providers` declares only `depends_on = ["korvid.agent"]`.

```bash
git checkout -- src/korvid/providers/registry.py
uv run tach check
```
Expected: PASS (exit 0).

- [ ] **Step 4: Confirm the tree is clean**

```bash
git status --porcelain
```
Expected: empty output. If any mutation is still present, `git checkout --` the file before
continuing. If any mutation in Step 3 did **not** produce the failure stated, the boundary
is not enforced: fix the guard (or `tach.toml`) before moving on — never widen the rule to
make a mutation pass.

- [ ] **Step 5: Run the whole gate**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run tach check
uv run deptry .
uv run pytest --cov -q
```
Expected: every command exits 0 and coverage is ≥ 80%.

Then prove that "green" means "executed". Every adapter test in this migration sits
behind a `pytest.importorskip`, so a missing extra turns the entire transport suite into
silent skips without failing anything:

```bash
uv run pytest -q -rs tests/providers/ | tail -20
```

Expected: no `SKIPPED` lines attributable to a missing `openai`, `httpx2` or
`pydantic_ai`. Confirm by name that the two reproductions this plan relies on for its
external-behaviour claims actually ran:

```bash
uv run pytest -p no:tach -q -rs \
  tests/core/test_config_profiles.py \
  tests/providers/test_pydantic_model.py \
  tests/providers/test_pydantic_contracts.py \
  -k "azure_url or deployment_scoped or stream or usage or cancel"
```

Expected: a non-zero passed count and `0 skipped`. These are the Azure MockTransport URL
table (Task 2 Step 3) and the scripted-stream reproduction (Task 13) — the fragmented
tool-call assembly, the text deltas, the usage record, the absence of `request_sent`
when the transport refuses, and cancellation. Neither is reachable from the UI in a
test, so if either is skipped here the migration has no evidence for the two behaviours
most likely to break silently in front of a user. A skip at this step blocks acceptance
exactly like a failure does.

- [ ] **Step 6: Run the repository's own gate target**

Run: `make check`
Expected: PASS. If the pre-commit ruff-format hook rewrites files, `git add -A` and commit again — never `--amend`, never `--no-verify`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "test: pin the provider import graph and layer boundaries" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git push
```

---

### Task 20: Run the review loop to termination, then hand the PR back

**Files:** none (PR operations only).

**Interfaces:**
- Consumes: the branch from Tasks 1–19 and, if one was opened, the draft PR from Task 4.
- Produces: a non-draft PR with every required check green, handed to the maintainer. **This task ends in a report, never a merge.**

This task runs the **iterative** review loop from `AGENTS.md` — not a single round. One
round is not a review loop: the first round of a change this size reliably produces
findings, and the fixes for those findings are themselves unreviewed until a later round
looks at them. The loop below terminates on a stated condition, not on a fixed count.

**Termination rule.** Keep a counter of *consecutive* rounds that contain only suppressed
low-confidence findings and no unresolved blocking findings. After **two** such consecutive
rounds, stop: resolve or document whatever advisory findings remain, do not request another
review, and go to the hand-back step. Any new credible blocking finding resets the counter
to zero. The counter is never a licence to ignore a credible finding or to skip a required
check.

- [ ] **Step 1: Confirm there is a PR to review**

```bash
gh pr view --json number,isDraft,url --jq '{number,isDraft,url}'
```

If this reports no pull request, Task 4's authorisation gate was not satisfied — no human
in that conversation asked for a PR. In that case **stop here** and hand the branch back
instead: report the branch name, the commit groups, and the local `make check` result from
Task 19. Do not open a PR to make this task runnable. Record the PR number for the rest of
this task:

```bash
PR="$(gh pr view --json number --jq .number)"
```

- [ ] **Step 2: Mark the PR ready and request the first review**

```bash
gh pr ready "$PR"
gh api -X POST "repos/hellices/korvid/pulls/$PR/requested_reviewers" \
  -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
```
Expected: the PR leaves draft state and the reviewer is requested.

- [ ] **Step 3: Poll for the round's review**

```bash
gh api graphql -f query='
query($owner:String!,$name:String!,$number:Int!) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      reviewRequests(first:10) { nodes { requestedReviewer { ... on Bot { login } } } }
      reviews(last:5) { nodes { author { login } body state submittedAt } }
      reviewThreads(first:50) {
        nodes { id isResolved comments(first:10) { nodes { id body path } } }
      }
    }
  }
}' -F owner=hellices -F name=korvid -F number="$PR"
```
Expected: `reviewRequests` empties and a new `reviews` entry appears. Reviews typically land
within 5–10 minutes and may need a second wait; re-run this query rather than assuming the
round produced nothing.

- [ ] **Step 4: Read and classify every finding in the round**

Read the inline thread comments **and** the suppressed low-confidence findings hidden in
the review body's `<details>` block. Classify each one as either:

- **blocking** — a correctness, security, data-loss or architecture-invariant defect, or a
  required check that is failing. These are always addressed.
- **advisory** — a suppressed low-confidence finding with no demonstrated defect behind it.
  These may be answered with a reply instead of a code change.

A finding that touches one of this branch's security invariants is blocking regardless of
the confidence label the reviewer attached: the approval gate, the `run_kubectl`
validation, fail-closed audit logging, "no secret value in `config.yaml`", "Azure sends
`api-key`, never a bearer", and "no SDK client is constructed with an implicit ambient
credential".

- [ ] **Step 5: Fix every blocking finding with TDD**

For each blocking finding: write the failing test first (RED), confirm it fails for the
stated reason, then make it pass (GREEN), then run the full gate before committing.

```bash
uv run pytest -p no:tach <the new test> -q   # RED, before the fix
make check                                   # after the fix
git add -A
git commit -m "fix: address review finding — <one-line summary>" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git push
```

If the pre-commit ruff-format hook rewrites files on the first attempt, `git add -A` and
commit again — never `--amend`, never `--no-verify`.

- [ ] **Step 6: Reply to and resolve each thread in the round**

Reply to every finding individually, naming the commit and the test:

```bash
gh api "repos/hellices/korvid/pulls/$PR/comments/<comment_id>/replies" \
  -f body="Fixed in <commit sha>; covered by <test name>."
gh api graphql -f query='mutation {
  resolveReviewThread(input:{threadId:"<PRRT_id>"}) { thread { isResolved } }
}'
```
Expected: every addressed thread reports `isResolved: true`. An advisory finding that is
being declined gets a reply saying *why*, and is resolved too — an unexplained resolve is
indistinguishable from ignoring it.

- [ ] **Step 7: Decide whether the loop terminates**

```bash
gh api graphql -f query='
query($owner:String!,$name:String!,$number:Int!) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:50) { nodes { id isResolved } }
    }
  }
}' -F owner=hellices -F name=korvid -F number="$PR" \
  --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length'
```
Expected: `0`.

Then:

- If the round contained **any** blocking finding, reset the low-confidence counter to 0,
  re-request the review, and go back to Step 3:

  ```bash
  gh api -X POST "repos/hellices/korvid/pulls/$PR/requested_reviewers" \
    -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
  ```

- If the round contained **only** advisory findings (or none), increment the counter. If it
  is now less than 2, re-request the review with the command above and go back to Step 3.
  If it has reached 2, stop requesting reviews and go to Step 8. Do not make speculative
  changes past this point.

- [ ] **Step 8: Verify every required check**

```bash
gh pr view "$PR" --json statusCheckRollup \
  --jq '.statusCheckRollup[] | {name: .name, conclusion: .conclusion}'
```
Expected: every entry is `SUCCESS` — ruff, mypy, pytest on 3.11, 3.12 and 3.13, coverage
≥ 80%, tach, and deptry. A single non-`SUCCESS` entry sends you back to Step 5; the
termination rule governs *review rounds*, never required checks.

- [ ] **Step 9: Confirm the relock helper branch is closed, not merged**

Task 11 may have opened a helper pull request for the `uv.lock` regeneration. It exists to
show the maintainer the lock diff, and it is **never** merged — the lock change travels in
this feature PR.

```bash
gh pr list --state open --search "uv.lock in:title" --json number,title
```
Expected: empty. If a helper PR is still open, close it and delete its branch:

```bash
gh pr close <helper-pr> --comment "Superseded: the lock change ships in #$PR." --delete-branch
```

- [ ] **Step 10: Hand the PR back and stop**

Report to the maintainer:

- the PR number and URL, and that it is ready for their review;
- the six commit groups it contains;
- the `statusCheckRollup` output from Step 8, showing every required check `SUCCESS`;
- how many review rounds ran and what terminated the loop;
- every advisory finding that was declined, with the reason given in its reply;
- the one behaviour this branch knowingly leaves in place: `providers/ollama.py` keeps the
  `thinking` round-trip, with the deletion criterion recorded in its docstring.

**Do not merge.** Do not run `gh pr merge`, do not enable auto-merge, do not add any
workflow, action, script or scheduled job that merges, do not call a REST or GraphQL merge
endpoint, and do not approve your own work. The maintainer merges; this plan ends in a
report.
