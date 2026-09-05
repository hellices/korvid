# Provider-Neutral Model Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace korvid's single hard-coded provider configuration and CSP-oriented `:ai` wizard with named model connection profiles whose provider and model selection is **data-driven** — read from LiteLLM's shipped catalog tables and optionally enriched from models.dev — and whose routing is **delegated** to `litellm.get_llm_provider`/`litellm.acompletion`. korvid's `NativeAgentEngine`, `RequestGateway`, `OutboundPolicy`, `ToolHarness`, approval gate, audit log, conversation repair and evidence contracts are untouched.

**Architecture:** `core/config.py` owns immutable, provider-neutral profile dataclasses (`ConnectionAuthConfig`, `ModelConnectionConfig`, `ModelConnectionsConfig`) as the single source of truth. `agent/model_profiles.py` publishes the catalog vocabulary the UI consumes without importing `providers/` or `litellm`. `providers/litellm_catalog.py` builds that catalog from LiteLLM's offline tables; `providers/models_dev.py` optionally enriches it from a bounded, cached public JSON document; `providers/special_flows.py` registers the two flows LiteLLM cannot own. `providers/_litellm_import.py` is the **only** module in korvid that executes `import litellm` — it makes that import offline and silent — and `providers/litellm_runtime.py` is the only module that imports it, applying the telemetry/callback lockdown. `providers/litellm_provider.py` implements korvid's existing `LLMProvider` over `acompletion(stream=True)`.

**Tech Stack:** Python 3.11+, Textual, `litellm==1.98.0` (inside the `[agent]` extra only), `httpx>=0.27` (LiteLLM's own floor is `httpx>=0.28,<1.0` — the same family korvid already uses, so no second client flavour appears), pytest / pytest-asyncio, Ruff, mypy --strict, tach, deptry, uv.

**API baseline:** every LiteLLM call in this plan is written against the exact interfaces of **litellm 1.98.0**, verified by reflection *and by execution* against an installed copy (`/tmp/korvid-litellm-probe`). The load-bearing facts:

| Fact | Exact 1.98.0 interface / observed behaviour |
| --- | --- |
| Package | `litellm` 1.98.0, **MIT**. Base install resolves **55 distributions**, including `openai`, `boto3`/`botocore`/`s3transfer`, `tiktoken`, `tokenizers`, `huggingface_hub`, `aiohttp`, `pydantic`, `pydantic-settings`, `jsonschema`, `jinja2`, `fastuuid`, `requests`. Declared floors include `httpx>=0.28.0,<1.0` and `openai>=2.20.0,<3.0.0` |
| Async entry point | `litellm.acompletion(model, messages=[], ..., stream=None, tools=None, tool_choice=None, base_url=None, api_version=None, api_key=None, extra_headers=None, timeout=None, stream_options=None, temperature=None, max_tokens=None, seed=None, reasoning_effort=None, thinking=None, **kwargs)` — 43 named parameters, all positional-or-keyword, plus `**kwargs`. `api_base`, `custom_llm_provider`, `client`, `drop_params`, `extra_body`, `num_retries` are **not** named parameters; they are accepted through `**kwargs` |
| Streaming return | `await acompletion(..., stream=True)` returns `litellm.utils.CustomStreamWrapper`, an async iterator with `__anext__` and **`aclose()`** (there is no `close()`) |
| Streaming failure timing | `await acompletion(..., stream=True)` raises **before returning a wrapper** for every failure — refused connection, timeout, and answered error status alike. The failures are **not distinguishable by exception type**: `httpx.ConnectError` and a genuine HTTP 500 both surface as `litellm.InternalServerError` with `status_code=500` (MRO: `litellm.exceptions.InternalServerError` → `openai.InternalServerError` → `openai.APIStatusError`). They **are** distinguishable by the exception's `__context__` chain (`__cause__` is `None` for all of them): a refused connection or timeout carries an `httpx.TransportError` subclass (`ConnectError`, `ReadError`, `ConnectTimeout`, `ReadTimeout`), while an answered request carries an `httpx.HTTPStatusError`. Observed chains — connect-refused: `litellm.InternalServerError ← litellm.llms.openai.common_utils.OpenAIError ← openai.APIConnectionError ← httpx.ConnectError`; HTTP 401/403/404/429/500/503: `litellm.<X>Error ← … ← openai.<X>Error ← httpx.HTTPStatusError`; connect/read timeout: `… ← openai.APITimeoutError ← httpx.ConnectTimeout\|ReadTimeout`. This is what makes the `REQUEST_SENT` rule in Task 14 decidable |
| Chunk type | `litellm.types.utils.ModelResponseStream` with fields `id`, `created`, `model`, `object`, `system_fingerprint`, `choices`, `provider_specific_fields` |
| Choice / delta | `StreamingChoices(finish_reason=None, index=0, delta: Delta | None = None, logprobs=None, ...)`; `Delta(content=None, role=None, function_call=None, tool_calls=None, audio=None, images=None, reasoning_content=None, thinking_blocks=None, ...)` |
| Tool-call fragments | `delta.tool_calls` is a list of `ChatCompletionDeltaToolCall` (a pydantic model) with `index: int`, `id: str | None`, `type`, and `function: Function` carrying `name: str | None` and `arguments: str`. Arguments arrive **fragmented**: observed `'{"ns":'` then `'"kube-system"}'` across two chunks, with `id`/`name` only on the first |
| Usage | `litellm.types.utils.Usage(prompt_tokens=None, completion_tokens=None, total_tokens=None, ...)`, exposed as `chunk.usage` on a trailing chunk. With `stream_options={"include_usage": True}` and a provider that emits usage in a `choices: []` chunk, LiteLLM **passes the provider's numbers through** (observed 11/7/18). When the provider attaches usage to a chunk that *also* carries choices, LiteLLM substitutes its own tokenizer estimate (observed 8/31 for a 11/7 payload) |
| Routing | `litellm.get_llm_provider(model: str, custom_llm_provider: str | None = None, api_base: str | None = None, api_key: str | None = None, litellm_params: GenericLiteLLMParams | None = None) -> tuple[str, str, str | None, str | None]` returning `(model, provider, dynamic_api_key, dynamic_api_base)`. Observed: `openai/gpt-4o → ('gpt-4o', 'openai', None, None)`; `ollama/llama3 → ('llama3', 'ollama', None, 'http://localhost:11434')`; `hosted_vllm/qwen → ('qwen', 'hosted_vllm', 'fake-api-key', None)`; `xai/grok-4 → ('grok-4', 'xai', None, 'https://api.x.ai/v1')` |
| **`dynamic_api_base` is not "does this provider have a default host"** | It is a *dynamic override* the router computes for the handful of providers whose base URL is environment-derived or fixed, and it is `None` for the ones with the most famous default hosts. Measured: `openai/gpt-4o → None`, `anthropic/claude-sonnet-4-5 → None`, `azure/gpt-4o → None`, `gemini/gemini-2.5-pro → None`, `bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0 → None`, `openai_like/x → None`; non-`None` only for `ollama/llama3 → 'http://localhost:11434'`, `groq/llama-3.3-70b-versatile → 'https://api.groq.com/openai/v1'`, `xai/grok-4 → 'https://api.x.ai/v1'`. Reading it as "has a default host" inverts the truth on every major vendor, which is why the `none`-auth rule keys on the operator's own `base_url` instead (Global Constraint on the `none`-auth rule; Task 15) |
| **`model_cost` records carry no host** | Grepped across every record in 1.98.0: no `api_base`, `base_url`, `host`, `endpoint`, `api_endpoint` or `url` key exists on any of them. The only adjacent keys are `supported_endpoints` (a list of API *paths* such as `/v1/chat/completions`) and `supports_url_context` (a boolean capability). There is therefore **no data source** from which a catalog could derive "does this provider need an endpoint", which is why `endpoint_requirement` is answered from the special-flow registry and defaults to `OPTIONAL` (Task 6) |
| **Routing hazard** | `get_llm_provider(model="github_copilot/gpt-4o")` **starts an interactive GitHub device-login flow inside the routing call**: it prints `Please visit https://github.com/login/device and enter code ...`, blocks on polling, writes `~/.config/litellm/github_copilot/api-key.json`, and finally raises `BadRequestError` wrapping `AuthenticationError`. korvid must never hand a Copilot reference to LiteLLM — this is the concrete reason the special-flow registry exists |
| Catalog tables | `litellm.model_list` (model ids), `litellm.provider_list` (`LlmProviders` members; the enum subclasses `str`, so `p.value` and `str` comparison both work), `litellm.models_by_provider` (provider → ids) and `litellm.model_cost` (per-model records). **No cardinality is recorded here and none is asserted anywhere in this plan** — the bundled offline tables and the remote model-cost map are *different sizes*, and both change with every LiteLLM patch release, so a written-down count is wrong the moment the floor moves |
| **`model_cost` key hazard** | `model_cost` is keyed **both** bare (`sora-2`) and provider-qualified (`openai/sora-2`). For a measurable minority of references *both* keys exist and carry **different** facts. A bare-first lookup therefore reads another provider's record. Every lookup tries `f"{provider}/{model_id}"` **first**, then the bare key |
| **Import-time network hazard** | `import litellm` calls `get_model_cost_map(url=...)`, which performs a **blocking HTTPS GET** of `model_prices_and_context_window.json` unless `LITELLM_LOCAL_MODEL_COST_MAP` is `"true"` in `os.environ` *before* the import statement executes. Measured: 4 outbound `connect()` calls to `185.199.x.x:443` on a bare import; **0** with the variable set. On failure it warns to **stderr** through `litellm.verbose_logger`, whose `handlers` are `['StreamHandler']` with `propagate=True` — i.e. straight onto the terminal a Textual app owns. Neither hazard can be fixed after the import, which is why Task 6 adds a dedicated import wrapper |
| **Catalog hazard** | `litellm.models_by_provider` values are **heterogeneous**: most are `set[str]`, a handful are `list[str]`. Indexing a value (`[:5]`) raises `TypeError: 'set' object is not subscriptable`. Every read must normalize through `sorted(...)` |
| Model info | `litellm.get_model_info(model=...)` returns a mapping including `max_input_tokens`, `max_output_tokens`, `supports_function_calling`, `supports_vision`, `mode`, `litellm_provider`. For an unmapped model it raises a **plain `Exception`** whose message starts `This model isn't mapped yet.` — so the call site catches `Exception` narrowly at that one seam and treats it as "unknown" |
| Supported params | `litellm.get_supported_openai_params(model=..., custom_llm_provider=...) -> list[str] | None`, e.g. openai → `['frequency_penalty', 'logit_bias', 'logprobs', 'top_logprobs', 'max_tokens', 'max_completion_tokens', ...]` |
| **Copilot catalog reach** | `models_by_provider["github_copilot"]` ships dozens of already-slash-qualified ids (`github_copilot/claude-haiku-4.5`, …). Emitting them into korvid's catalog would put the routing hazard above one keystroke away in model search, so the catalog **excludes or rewrites** that provider, and the special-flow claim normalizes `_`→`-` so **both** spellings resolve to korvid's own flow |
| Lockdown flags | `litellm.telemetry` (default `True`), `litellm.turn_off_message_logging` (`False`), `litellm.success_callback` / `failure_callback` / `callbacks` / `_async_success_callback` / `_async_failure_callback` (all `[]`), `litellm.suppress_debug_info` (`False`). All eight names exist in 1.98.0 (measured: none missing), so the `hasattr` guard Task 6 adds is a tripwire for a future rename, not a workaround for today |
| **`suppress_debug_info` guards `print()`, not a logger** | Two call sites emit an ANSI-coloured help block with a bare `print()` to **stdout** whenever `litellm.suppress_debug_info is False`: `litellm/litellm_core_utils/exception_mapping_utils.py` (in the exception mapper, on *every* mapped provider error) and `litellm/litellm_core_utils/get_llm_provider_logic.py` (on an unrecognised reference). Neither goes through `litellm.verbose_logger`, so the import wrapper's handler detaching cannot reach them — only the flag can. Under Textual, an unsuppressed 401 writes `\033[1;31m…` into the terminal the app is painting. This is why the flag is a security/UI invariant with a `capsys` test (Task 6), not a tidiness setting |
| Exceptions | `litellm.exceptions` exports `APIConnectionError`, `APIError`, `AuthenticationError`, `BadRequestError`, `ContextWindowExceededError`, `InternalServerError`, `NotFoundError`, `PermissionDeniedError`, `RateLimitError`, `ServiceUnavailableError`, `Timeout`-family and more. **Measured across the 24 exported error classes: exactly one (`APIError` itself) subclasses `litellm.exceptions.APIError`, while 22 share `openai.OpenAIError` as their common base.** `AuthenticationError` → `openai.AuthenticationError` → `openai.APIStatusError` → `openai.APIError` → `openai.OpenAIError`; `Timeout` → `openai.APITimeoutError` → `openai.APIConnectionError` → … → `openai.OpenAIError`. So `except litellm.exceptions.APIError` catches **almost nothing** and a 401 would escape the transport unmapped; the correct base is `openai.OpenAIError`, re-exported by `litellm_runtime.py` as `ProviderSDKError` so `providers/` still names one module for everything LiteLLM. The two classes outside that base — `BudgetExceededError` and the PII/guardrail error — belong to router and guardrail features the lockdown disables |
| Test seam | `acompletion(..., client=AsyncOpenAI(base_url=..., api_key=..., http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))` reaches the handler for OpenAI-shaped providers. Observed request: `POST https://<base_url>/chat/completions` with `Authorization: Bearer <key>` and body keys `['messages', 'model', 'stream', 'stream_options', 'tools']` |

Anything not in this table is quoted with its verified signature at the point of use. No step in this plan says "confirm after install": the signatures are already confirmed.

**How these facts were established, and how to re-establish them.** Create a throwaway venv *outside* the repository through the global uv proxy (see the network constraint below) and reflect on it with `inspect.signature`, then *drive real objects* for behaviour. Wire-level facts — which header carries a credential, which URL a request reaches, what JSON body a setting produces — are established by mounting an `httpx.MockTransport` on a client passed to `acompletion(client=...)` and capturing the `httpx.Request` the SDK builds. **Never** reach into private SDK internals for a security fact: a private helper can change shape in a patch release, and an invariant asserted through a private hook is one that silently stops being asserted.

**Five reproductions have already been executed end to end** against that venv (`litellm 1.98.0`, `openai 2.x`, `httpx 0.28`, `boto3 1.43.83`):

- **Streaming and tool-fragment assembly.** A four-chunk SSE script through `MockTransport` produced, in order: a text delta `"he"`, a tool-call fragment `id='c1' name='get_pods' arguments='{"ns":'`, a continuation fragment `arguments='"kube-system"}'` with `id=None`/`name=None`, and a trailing usage chunk. This is why the provider accumulates by `tool_call.index` and emits one tool call at stream end.
- **`REQUEST_SENT` timing.** A transport raising `httpx.ConnectError` and a transport answering `HTTP 500` produced the *same* exception type and status code; only the `__context__` chain differed (`httpx.ConnectError` vs `httpx.HTTPStatusError`). That measurement is the whole reason Task 14's rule keys on the chain rather than on `isinstance(exc, openai.APIStatusError)`, which would report a refused connection as a sent request.
- **Usage passthrough vs. estimate.** Usage in a `choices: []` chunk arrived verbatim (11/7/18); usage attached to a chunk carrying choices was replaced by LiteLLM's own estimate.
- **Import-time network traffic.** Patching `socket.socket.connect`/`connect_ex` to record and refuse, then importing `litellm`, recorded 4 attempts to `185.199.x.x:443`. With `os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"` set first, the same probe — extended to build the full normalized `models_by_provider` index — recorded **0**. `litellm.verbose_logger.handlers` is `['StreamHandler']` with `propagate=True`.
- **`model_cost` key divergence.** Iterating every provider×model pair and comparing `model_cost[bare]` against `model_cost[f"{provider}/{model}"]` found a measurable set of references where both keys exist and the records differ (e.g. `openai`/`sora-2`). The exact size is not recorded — it drifts with the data — but the existence of the class is what forces qualified-key-first lookup.

Neither reproduction lives in the repository, so none of them substitutes for the tests this plan writes. They are the reason those tests are written the way they are.

## Global Constraints

- **One pull request.** Config, catalog, setup UI, transport, special flows and legacy deletion land on the single branch `agents/provider-neutral-profiles`. Each task below is an independently testable commit-group step; none of them is its own PR. Opening that PR — draft or otherwise — requires an explicit instruction from the human driving the work; Task 4 pushes the branch unconditionally and opens the PR only under that instruction.
- **The maintainer merges. You never do.** No `gh pr merge`, no auto-merge, no merge automation of any kind, including for the relock helper PR (which is *closed and its branch deleted*, never merged).
- **The review loop runs to AGENTS.md's termination rule, not to one round.** Task 21 iterates: read every comment including the suppressed low-confidence findings inside the review body's `<details>` block, fix credible findings with TDD, reply per comment, resolve each thread, re-request review, poll, repeat. It terminates after two consecutive rounds that contain only suppressed low-confidence findings and no unresolved blocking findings; any new credible blocking finding resets that counter.
- **Never `git commit --no-verify`**, never edit a gate file to make a failure pass.
- Profile names: non-empty, at most 100 characters, only ASCII letters, digits, `.`, `_`, `-`. Names are **not** normalized (`prod-east` != `prod_east`). `agent.active` must exactly name an entry in `agent.profiles`.
- **Profile insertion order is the file's order.** `ModelConnectionsConfig.profiles` preserves the order the profiles appear in `config.yaml`; nothing sorts them. The wizard's list, the `:model` picker and every test assert that same order.
- **Model references use the standard slash form `provider/model`.** That is what LiteLLM accepts, what models.dev publishes, and what every tool in the ecosystem prints. A colon form is not used: colons already occur *inside* model ids (`qwen3:8b`, `anthropic.claude-3-5-sonnet-20240620-v1:0`), so a colon separator is ambiguous. Tasks 1 and 2 landed with a colon form; **Task 2B is a correction commit that fixes it before anything consumes a reference.**
- **korvid owns no provider class table and no per-vendor construction branch.** Routing is `litellm.get_llm_provider` for validation and `litellm.acompletion` for execution. There is no `BUILTIN_ADAPTERS`, no `provider-*` extra, and no `_build_<vendor>_model` function anywhere in this plan. A test asserts that the transport package contains no vendor-name dispatch.
- Common auth methods, exactly these five ids: `none`, `environment` (with `key`), `keyring` (with `key`), `provider-default`, `device-login`. They map onto LiteLLM's *common* call arguments; `provider-default` deliberately passes **no** `api_key` so the provider SDK's own environment/default credential chain applies.
- Secrets are never stored in YAML — only references. Both `profile.options` and `auth.settings` are parsed through the *same* bounded, secret-refusing validator before they are frozen.
- **Freezing is recursive, so thawing must be too.** `_freeze_config_value` produces `MappingProxyType`/`tuple`; `yaml.safe_dump` has no representer for `mappingproxy` and raises `RepresenterError`. `save_model_connections` thaws recursively (`Mapping → dict`, `tuple → list`) before dumping, and a nested-option round-trip test pins it.
- **A save never deletes what the parser could not model.** A profile korvid dropped (invalid name, no `model:`) and an `options`/`auth` block korvid rejected are carried on `ModelConnectionsConfig.unparsed` as raw mappings and written back verbatim. `unparsed` is *never* read by anything that builds, activates or lists a connection: it is write-back state only, and a test pins that. The one exception is an *explicit* delete, and it must clear **both** halves — a profile that parsed into a `config_error` is a member of `profiles` *and* of `unparsed`, so removing it from one alone lets the writer re-emit it and the deleted profile comes back on the next load.
- **`config_error` refuses construction, everywhere and permanently.** `create_provider_from_profile` returns `None` with a warning whenever `profile.config_error is not None`, and a test that survives the legacy deletion pins it.
- **A misconfiguration disables the agent; it never crashes startup.** Every documented failure mode of the factory (unroutable reference, unsupported auth method, missing credential, unreadable CA bundle, keyless public host) returns `None` and appends to `startup_warnings`. `__main__`'s provider construction catches `(ProviderPluginError, ValueError)`, and a test asserts an unroutable profile yields `None` plus a warning rather than raising.
- Core never imports `litellm`, `korvid.providers`, or any provider identifier outside the isolated legacy-migration region. UI never imports `korvid.providers` and never branches on `azure`, `aws`, `gcp`, `ollama`, or `github-copilot`.
- **The vendor-leak guard scans a premeasured surface and allows by exception, and the exceptions are scoped.** `tests/test_vendor_neutrality.py` (Task 18) parses modules with `ast` and looks for vendor tokens in **executable** string positions only — docstrings are excluded, because prose may name a vendor while code may not branch on one. The scan surface is **the routing surface**, not the whole tree: `src/korvid/providers/`, `src/korvid/agent/`, `src/korvid/ui/` and `src/korvid/core/config.py`. That is where the guard's claim ("korvid owns no per-vendor branch") is meaningful; `src/korvid/evals/` runs local benchmark harnesses that legitimately name `ollama` and `openai` in URLs and CLI defaults, `src/korvid/k8s/` names cloud vendors for *cluster* detection, and `__main__.py` wires whatever exists. Widening the scan to those would produce a long allow-list of unrelated files, which is a guard that has stopped saying anything. A short allow-list carries the modules *inside* the surface where a vendor token is legitimate, each with a written reason. **The allowances are premeasured against the tree this plan starts from** (see Task 18 for the exact list, the measurement command and its recorded output), so the guard passes on arrival rather than being debugged into passing. An allowance is not a blank cheque either: every allowed module is re-scanned by a companion test that pins **which** tokens it may contain, so `litellm_catalog.py` may name the Copilot prefixes it rewrites and nothing else — a static provider frozenset appearing in an allowed module fails the guard instead of hiding behind it. `core/config.py` is scoped by **AST region**, not by line or by whole file: the migration function bodies and the migration-only alias table are computed from the module's tree, so a vendor name introduced anywhere else in that file still fails. Seven meta-tests keep the guard honest — every allowance must name a file that exists, the allowance set must be a **strict** subset of the scanned set so a stray directory prefix can never silently switch off whole subtrees, each allowance's tokens must stay within its declared reason, the migration exemption must not cover the whole module, every migration helper named in that exemption must still be **defined in the module's AST at the commit the guard lands in** (so a helper Task 18 deletes must not be named there, and a substring match in a comment cannot stand in for a definition), and `load_config` itself — the module's largest function, which today infers `device-login` from a vendor name inline and reads the legacy `agent.ollama` sub-mapping by name — must name no vendor at all, forcing Task 2B to move both into `_legacy_*` helpers rather than exempting 165 lines.
- **Only one module imports `litellm`, and it is an import wrapper, not the runtime.** `providers/_litellm_import.py` sets `LITELLM_LOCAL_MODEL_COST_MAP` **before** `import litellm` and detaches LiteLLM's `StreamHandler`s; `providers/litellm_runtime.py` imports that wrapper and applies the lockdown. Two tests walk `src/korvid/**/*.py`, parse each with `ast`, and assert (a) the set of modules naming `litellm` in an `Import`/`ImportFrom` is exactly `{providers/_litellm_import.py}`, and (b) the set of modules importing `korvid.providers._litellm_import` is exactly `{providers/litellm_runtime.py}`. A leaf `import litellm` in `litellm_runtime.py` cannot carry the env var, because an import sorter moves a third-party import above any `korvid` import in the same top-level block — the ordering has to be a *file* boundary, not a statement order anyone can reflow.
- **The provider layer imports without touching the network.** A test spawns a **fresh subprocess**, patches `socket.socket.connect`/`connect_ex` to record and refuse, imports `korvid.providers.litellm_runtime`, builds the full catalog index, and asserts the recorded connection list is empty. A subprocess is required: `litellm` may already be imported by another test in the same session, and a cached module proves nothing.
- **LiteLLM is locked down before the first call, and a renamed flag fails loudly.** `telemetry=False`, `turn_off_message_logging=True`, `success_callback=[]`, `failure_callback=[]`, `callbacks=[]`, `_async_success_callback=[]`, `_async_failure_callback=[]`, `suppress_debug_info=True`. Each name is checked with `hasattr` **before** assignment and a missing name raises `ImportError` naming it — assigning first and reading back afterwards is a tautology that would keep passing after an upstream rename while the real callback sink stayed open. No `Router`, no `num_retries`, no `fallbacks`, no proxy, no caching, no observability callback. Tests assert each flag after import *and* that a stub module missing one flag raises.
- **`suppress_debug_info=True` is the flag that protects stdout, and it is not hygiene.** LiteLLM calls bare `print()` with ANSI colour codes on every mapped exception — `litellm_core_utils/exception_mapping_utils.py` and `litellm_core_utils/get_llm_provider_logic.py` both emit a red help block straight to **stdout**, gated on nothing but `litellm.suppress_debug_info is False`. Those calls never touch the logging subsystem, so the import wrapper's `StreamHandler` removal does not reach them: in a Textual app the first 401 repaints the screen with escape sequences korvid did not write. A test drives a provider error under `capsys` and asserts `capsys.readouterr().out == ""`, so a maintainer trimming the flag list as "noise control" fails CI instead of corrupting the TUI.
- **The transport catches the SDK's real base class, not `litellm.exceptions.APIError`.** Measured on 1.98.0, `litellm.exceptions.AuthenticationError` → `openai.AuthenticationError` → `openai.APIStatusError` → `openai.APIError`; 22 of the 24 exported error classes have `openai.OpenAIError` as their only common base and do **not** subclass `litellm.exceptions.APIError`. `litellm_runtime.py` re-exports that base as `ProviderSDKError` so `providers/` still names exactly one module for everything LiteLLM, and a test asserts every name in `litellm.exceptions` that matters is caught by it. The two classes it misses (`BudgetExceededError`, the guardrail errors) belong to features the lockdown disables. Catching the wrong base would make the whole `REQUEST_SENT` rule below dead code and let a raw `AuthenticationError` reach korvid's engine — and the fix must never be widening to bare `except Exception`.
- **korvid never routes to LiteLLM's `github_copilot` provider, in either spelling.** Resolving that prefix starts an interactive device flow and writes files under `~/.config/litellm/`. Three mechanisms enforce it: the catalog excludes or rewrites LiteLLM's `github_copilot` entries so the underscore spelling is never offered; `SpecialFlowRegistry.claim()` normalizes `_`→`-` before matching so both spellings hit korvid's flow; and the factory checks the normalized prefix against the claimed-and-denied set **before** calling `get_llm_provider`. The test is **behavioural** — `litellm_runtime.get_llm_provider` is monkeypatched to fail if called, and profiles are built for both `github_copilot/gpt-4o` and `github-copilot/gpt-4o` — because a source grep cannot see a reference that arrived from LiteLLM's own data.
- Model libraries never receive Kubernetes clients, `WriteOps`, approval callbacks, or audit handles.
- The outbound order is fixed: provider message preparation → `OutboundPolicy` redaction/canonicalization/size enforcement → canonical request snapshot → LiteLLM argument conversion → network transmission. Conversion may re-encode but may never add user-controlled prompt text or tool definitions.
- **`REQUEST_SENT` means the provider has the payload, which includes an answered error.** It is emitted when `await acompletion(...)` returns a stream wrapper, **and** when that `await` raises with an `httpx.HTTPStatusError` in its `__context__` chain — 401, 403, 404, 429, 500 and 503 all mean the request reached the provider, and `agent/provider.py`'s existing contract says so explicitly ("as soon as the transport has accepted the request (response headers received), before the status code is judged"). It is **not** emitted when the chain carries an `httpx.TransportError` (connect/read failure, connect/read timeout), nor when neither marker appears. Keying on `isinstance(exc, openai.APIStatusError)` instead would be wrong: a refused connection surfaces as `InternalServerError(status_code=500)`, indistinguishable from a real 500 by type or status.
- **Tool-call fragments accumulate by `tool_call.index`, never `choice.index`.** `choice.index` is `0` on every chunk when `n=1`, so keying on it collapses parallel tool calls into one. When the stream raises mid-iteration, accumulated-but-unemitted calls are **dropped** and the error is surfaced — a half-received call is not a call.
- **`provider-default` omits `api_key` entirely.** The request plan carries a tri-state (`None` means "no key resolved", a distinct sentinel means "omit"), and `call_kwargs` never puts the key in the mapping for that method. Passing `api_key=None` would not delegate — the SDK sees an explicit argument and stops consulting its own chain. The test asserts `"api_key" not in kwargs` **unconditionally**, not inside an `if` that can pass vacuously.
- **The `none`-auth rule is one field of the operator's own profile.** `none` is allowed **only** when `profile.base_url` is a non-empty string, and refused whenever it is absent. With no operator endpoint the request goes wherever the SDK's default takes it, and a default the operator did not choose is somebody else's service. No provider name and no routing result appear in the rule, so it is evaluated **before** `get_llm_provider`, and `auth_methods(reference, endpoint=...)` mirrors it exactly rather than approximating it. An earlier revision keyed on `get_llm_provider`'s `dynamic_api_base` being non-`None`; measured on 1.98.0 that field is a *dynamic override*, `None` for `openai`, `anthropic`, `azure`, `gemini` and `bedrock` and `http://localhost:11434` for `ollama` — so the old rule permitted a keyless POST to `api.openai.com` and refused the local Ollama case it existed to allow. Tests parametrize over **real** references with routing unpatched.
- Capabilities are translated only where the source directly asserts an equivalent fact; unknown facts stay `None`. Never infer tool support, context window, or reasoning from provider/model names.
- **models.dev is bounded metadata, never infrastructure, and never routing.** On-demand only (never at import, wiring or agent construction), 10 s total timeout, 12 MiB response ceiling enforced while streaming, `application/json` content type required, schema validated per entry, cached at the standard per-user cache directory as `korvid/models-dev.json` written atomically with `0o600`, ETag-revalidated, stale-tolerant, falling back to the bundled LiteLLM tables. No credentials, no prompts, no query parameters, no remote images or other assets. It supplies **display and capability metadata only**: it cannot select a provider, cannot change a reference, cannot supply a base URL or credential, and cannot make a model reachable or unreachable. An entry it adds is a *search result*, connectable exactly to the degree LiteLLM can already route it. Provenance is relabelled `MODELS_DEV` only for an entry where models.dev supplied a fact LiteLLM lacked.
- **No test, prose line, commit message or PR body states a catalog size.** The offline tables and the remote map differ, and both move with every LiteLLM release. Counts are asserted as "non-empty", "contains", or "does not contain" — never `== N`.
- Automatic multi-provider fallback is **not** enabled.
- **Every constant shared by two layers lives in a leaf module.** `providers/litellm_settings.py` is stdlib-only and imports nothing from `korvid`; the catalog, the factory, `registry.py` and `plugin_registry.py` take shared constants from there. No shared table is defined in a module that also imports one of its consumers.
- **This network cannot resolve public PyPI directly, but its proxy does.** `uv.lock` is produced only by the `Relock` workflow (Task 5B). Never run `uv lock` locally; never commit a lock resolved through a mirror. Verifying a library's API locally is a *separate* activity and has already been done: `uv venv /tmp/korvid-litellm-probe && uv pip install --python /tmp/korvid-litellm-probe/bin/python 'litellm==1.98.0'` succeeded through the global proxy and is the source of the API baseline table. That venv never touches `pyproject.toml`, `uv.lock` or `.venv`, which is why it is permitted where `uv lock` is not: it leaves no mirror-scoped URL behind.
- **The dependency lands before the first module that reads it.** Task 5B takes `litellm` into `[agent]` and relocks *between* Task 5 (the vocabulary) and Task 6 (the first LiteLLM catalog module), so Tasks 6–8 have a genuine RED and a genuine GREEN instead of three commits whose suites silently `importorskip`. The price is one commit where `deptry` reports DEP002 for a declared-but-unimported `litellm`; Task 5B adds a scoped, commented ignore and **Task 6 deletes it again** in the commit that adds the first importing module — the deletion is itself the proof that the wrapper imports what it claims to.
- **Every new import edge is already legal under `tach.toml`, checked against the rule file rather than the AGENTS.md summary.** `korvid.providers` declares `depends_on = ["korvid.agent"]` and nothing else — notably **not** `korvid.core`. That is why `agent/model_profiles.py` re-exports `ConnectionAuthConfig`/`ModelConnectionConfig`/`ModelConnectionsConfig` from `korvid.core.config` and every `providers/` module imports them from *there*: no `providers/` module imports `korvid.core` today, and none may start. The new intra-package edges (`litellm_runtime.py` → `_litellm_import.py`, `plugin_registry.py` → `litellm_settings.py`, `litellm_factory.py` → `special_flows.py`) are all inside `korvid.providers`, which tach does not govern. Still run `uv run tach check` whenever imports cross packages. Every test contains at least one `assert` or `pytest.raises(..., match=...)`. No bare `except:`; no bare `# type: ignore`.
- Every commit message ends with the trailer `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.

## File Structure

| Path | Responsibility | Task |
| --- | --- | --- |
| `src/korvid/core/config.py` | Immutable `ConnectionAuthConfig`/`ModelConnectionConfig`/`ModelConnectionsConfig`, new-shape parser, legacy migration, slash references, `save_model_connections` writer, derived legacy scalars (deleted in Task 18) | 1, 2, 2B, 3, 18 |
| `src/korvid/agent/model_profiles.py` (new) | Public, provider-free catalog vocabulary: `SetupFieldKind`, `SetupField`, `EndpointRequirement`, `AuthMethodDescriptor`, `ModelEntry`, `ModelEntrySource`, `ModelCatalog`, `DeviceLoginPrompt`, `SpecialFlow`, `split_reference()`, re-export of the three config dataclasses | 5 |
| `src/korvid/providers/litellm_settings.py` (new) | Stdlib-only leaf: `AGENT_EXTRA`, `KEYLESS_API_KEY_SENTINEL`, `RETIRED_PROVIDER_ALIASES`. Imports nothing from `korvid`; the install hint stays in the existing `agent/install_hint.py` | 5, 6, 8, 13, 15 |
| `src/korvid/providers/_litellm_import.py` (new) | The **only** module executing `import litellm`. Stdlib-only above the import: sets `LITELLM_LOCAL_MODEL_COST_MAP` before it, imports inside the install-hint `try:`, then detaches LiteLLM's `StreamHandler`s. No policy of its own | 6 |
| `src/korvid/providers/litellm_runtime.py` (new) | The only module importing `_litellm_import`. Applies the lockdown (fail-loud on a missing flag); exposes `acompletion`, `get_llm_provider`, `exceptions`, `LOCKDOWN_FLAGS`, `models_by_provider()`, `model_cost_entry()`, `supported_params()` | 6, 14 |
| `src/korvid/providers/litellm_catalog.py` (new) | `LiteLLMModelCatalog(ModelCatalog)`: offline search over LiteLLM's tables, capability translation, per-reference auth/option/endpoint answers, enrichment overlay, discovery and manual entry | 6, 7, 8, 17 |
| `src/korvid/providers/models_dev.py` (new) | `ModelMetadataSource` ABC + `ModelsDevSource`: bounded, cached, schema-validated models.dev enrichment. Never routes | 7 |
| `src/korvid/providers/special_flows.py` (new) | `SpecialFlowRegistry`: entry-point loading, prefix claiming, option claiming, reserved/retired-name refusal. No enumeration API | 8, 17 |
| `src/korvid/providers/endpoint_discovery.py` (new) | `EndpointDiscovery`: bounded `GET /v1/models` then `/api/tags` against an operator's own endpoint; `()` on every failure | 8 |
| `src/korvid/providers/litellm_request.py` (new) | `RequestPlan` + `build_plan()`: one frozen, resolved `acompletion` call per request | 13 |
| `src/korvid/providers/litellm_provider.py` (new) | `LiteLLMProvider(LLMProvider)`: stream driving, tool-fragment assembly, usage, reasoning, `REQUEST_SENT`, cancellation-safe `aclose()` | 14 |
| `src/korvid/providers/litellm_factory.py` (new) | `create_provider_from_profile()`: flow claiming, credential resolution, refusals, capability translation | 15 |
| `src/korvid/providers/flow_copilot.py` (new, from `github_copilot.py`) | Copilot device login + token exchange, declared as a `SpecialFlow` claiming `github-copilot/` | 17 |
| `src/korvid/providers/flow_ollama_thinking.py` (new, from `ollama.py`) | Native `/api/chat` transport, declared as a `SpecialFlow` claiming the `native_thinking` **option** on the `ollama` prefix | 17 |
| `src/korvid/providers/plugin_registry.py` | Keeps `RESERVED_PROVIDER_NAMES` and entry-point loading; loses the built-in alias sets in Task 18 | 8, 18 |
| `src/korvid/providers/registry.py` | **Deleted in Task 18** — `BUILTIN_ADAPTERS` and `create_provider()` are the table this design removes | 15, 18 |
| `src/korvid/providers/configurator.py`, `openai_compat.py`, `github_copilot.py`, `ollama.py` | **Deleted in Task 18** (the last two superseded by the flow modules) | 17, 18 |
| `src/korvid/providers/entra.py` | **Kept**: Azure's `provider-default` resolves through it | 15 |
| `src/korvid/ui/widgets/profile_manager_screen.py` (new) | `ProfileManagerScreen`: list, activate (write-only), add, edit, delete | 9 |
| `src/korvid/ui/widgets/model_search_screen.py` (new) | `ModelSearchScreen`: free-text search over the catalog + manual entry | 10 |
| `src/korvid/ui/widgets/agent_setup_screen.py` | Rewritten as a descriptor-driven stage machine: endpoint, auth method, auth fields, options, tier, test. No vendor names | 11 |
| `src/korvid/ui/agent_ui_controller.py` | Opens the profile manager or setup, activates profiles, persists them; keeps `KorvidConfig` for tier and follow | 11 |
| `src/korvid/ui/messages.py` | Profile-activation and profile-persist UI Bus messages | 9, 11 |
| `src/korvid/__main__.py` | Builds and injects the catalog (no I/O), builds providers from profiles, persists profiles; loses the interim dual wiring in Task 18 | 3, 8, 15, 18 |
| `src/korvid/evals/__main__.py` | `provider_factory_from_env` imports the deleted transports directly and branches on `provider_id == "ollama"`. **Migrated onto `create_provider_from_profile` in Task 18 Step 1, before any deletion** | 18 |
| `pyproject.toml` | `litellm==1.98.0` in `[agent]`, the two flow entry points, the `litellm.*` mypy override. **No per-vendor extras** | 5B, 17 |
| `uv.lock` | Regenerated by the `Relock` workflow only | 5B |
| `docs/agent.md`, `docs/provider-plugins.md`, `docs/airgap.md`, `docs/threat-model.md`, `docs/dev/agent-decisions.md`, `docs/release-notes/unreleased.md`, `AGENTS.md` | Operator documentation, decision record, dependency/licensing tradeoff, migration notes | 19 |

**Test files created:** `tests/agent/test_model_profiles.py` (5), `tests/providers/test_litellm_catalog.py` (6), `tests/providers/test_models_dev.py` (7), `tests/providers/test_special_flows.py` (8), `tests/providers/test_endpoint_discovery.py` (8), `tests/ui/test_profile_manager_screen.py` (9), `tests/ui/test_model_search_screen.py` (10), `tests/ui/test_agent_ui_controller_profiles.py` (11), `tests/providers/test_litellm_request.py` (13), `tests/providers/test_litellm_provider.py` (14), `tests/providers/test_litellm_factory.py` (15), `tests/agent/test_litellm_boundary.py` (16), `tests/providers/test_flow_copilot.py` (17), `tests/providers/test_flow_ollama_thinking.py` (17), `tests/test_vendor_neutrality.py` (18).

**Test files migrated, not deleted:** `tests/ui/test_agent_setup_screen.py` (Task 11 — rewritten case by case; a file Task 11 rewrites cannot also be removed wholesale in Task 18), `tests/providers/test_github_copilot.py` → `test_flow_copilot.py` (Task 17), the thinking half of `tests/providers/test_ollama.py` → `test_flow_ollama_thinking.py` (Task 17). `tests/providers/test_entra.py` is **kept unchanged**.

**Test files deleted in Task 18:** `tests/providers/test_registry.py`, `tests/providers/test_configurator.py`, `tests/providers/test_openai_compat.py` — but only after each behaviour they pin is either gone or re-pinned elsewhere, named individually in the commit body.

**Measured blast radius** (counted on the current tree by AST, over the symbols this change removes — `BUILTIN_ADAPTERS`, `create_provider`, `OpenAICompatProvider`, `OllamaProvider`, `GitHubCopilotProvider`, `AgentConfigurator`, `OPENAI_COMPAT_ALIASES`, and the five legacy `agent_*` config scalars). **`src/` is scanned as well as `tests/`**: a source module that imports a symbol Task 18 deletes breaks `mypy src/` and pytest *collection*, which a tests-only scan cannot see.

`src/` — every module that must be migrated or deleted before the removal lands:

| File | References | Disposition |
| --- | --- | --- |
| `src/korvid/__main__.py` | 15 | Interim dual wiring removed in Task 18 |
| `src/korvid/core/config.py` | 13 | Legacy scalars removed in Task 18 |
| `src/korvid/ui/agent_ui_controller.py` | 11 | Rewritten in Task 11 |
| `src/korvid/providers/registry.py` | 8 | Deleted in Task 18 |
| `src/korvid/providers/configurator.py` | 6 | Deleted in Task 18 |
| **`src/korvid/evals/__main__.py`** | **6** | **Migrated in Task 18 Step 1, before any deletion** |
| `src/korvid/ui/app.py` | 5 | Follows the controller |
| `src/korvid/ui/widgets/agent_setup_screen.py` | 4 | Rewritten in Task 11 |
| `src/korvid/agent/__init__.py` | 3 | Re-exports; pruned with the deletions |
| `src/korvid/agent/setup.py`, `src/korvid/providers/plugin_registry.py` | 2 each | Alias sets pruned in Task 18 |
| `src/korvid/providers/ollama.py`, `openai_compat.py` | 1 each | Superseded by the flow modules (Task 17) |

`tests/` — where the behaviour is pinned:

| File | Test functions touching a removed symbol | References |
| --- | --- | --- |
| `tests/providers/test_registry.py` | 37 | 62 |
| `tests/core/test_config.py` | 22 | 32 |
| `tests/test_main_wiring.py` | 21 | 109 |
| `tests/providers/test_configurator.py` | 9 | 13 |
| `tests/ui/test_agent_wiring.py` | 9 | 40 |
| `tests/providers/test_ollama.py` | 8 | 13 |
| `tests/providers/test_openai_compat.py` | 7 | 11 |
| `tests/providers/test_net.py`, `tests/providers/test_plugin_registry.py`, **`tests/evals/test_cli.py`** | 2 each | 6, 5, **4** |
| `tests/agent/test_outbound.py`, `tests/ui/test_agent_interrupt.py`, `tests/ui/test_agent_write.py` | 1 each | 1–2 each |
| `tests/agent/test_offline_local_session.py`, `tests/agent/test_setup.py`, `tests/evals/operation_app.py`, `tests/test_agent_replacement_guard.py`, `tests/ui/agent_write_support.py`, `tests/ui/test_agent_setup_screen.py`, `tests/ui/test_agent_ui_controller.py` | 0 (module-level or helper references) | 1–5 each |

The last row matters as much as the first: a module-level import of a deleted
symbol fails **collection**, so a file with zero matching test functions can
still turn the whole suite red.

Re-measure before starting Task 18 — the branch will have moved:

```bash
uv run python - <<'EOF'
import ast, pathlib, re
symbols = ["BUILTIN_ADAPTERS", "create_provider", "OpenAICompatProvider", "OllamaProvider",
           "GitHubCopilotProvider", "AgentConfigurator", "OPENAI_COMPAT_ALIASES",
           "agent_provider", "agent_base_url", "agent_api_key_env",
           "agent_auth_method", "agent_model"]
pat = re.compile("|".join(re.escape(s) for s in symbols))
for root in ("src", "tests"):
    print(f"== {root} ==")
    for path in sorted(pathlib.Path(root).rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if not pat.search(src):
            continue
        tree = ast.parse(src)
        hits = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
            and pat.search(ast.get_source_segment(src, node) or "")
        )
        print(f"{path} funcs={hits} refs={len(pat.findall(src))}")
EOF
```

Every `src/` line the script prints must be either **migrated** or **deleted**
by Task 18; a `src/` line left standing is a `mypy src/` failure in the commit
that runs `make check`.

---

## Commit group 1 — Named profile config domain (Tasks 1–4)

> **Execution state at the time this plan was last revised.** Task 1 (`a71ee6cf`)
> and Task 2 (`d7b69101`) are **LANDED** — implemented, committed and on the
> branch. Do not re-implement either; each carries a Step 1 that *verifies* the
> landed state instead. **Task 2B is the next commit to write.** It is a
> correction commit: it repairs a defect the two landed commits introduced
> (colon-separated model references, plus two vendor names left inline in
> `load_config`), and every later task in this plan assumes the slash form and
> the vendor-free `load_config`. Nothing after Task 2B may begin until Task 2B
> is committed and green — Task 3 writes references to disk, and a reference
> format shipped once is a data migration rather than a rename.

### Task 1: Immutable profile dataclasses and the new config shape — **LANDED at `a71ee6cf`**

**Status:** already implemented and committed. Do not redo it; verify it and move on.

`src/korvid/core/config.py` now holds `_freeze_config_value`, `_freeze_config_mapping`, `_validated_config_mapping`, `ConnectionAuthConfig`, `ModelConnectionConfig`, `ModelConnectionsConfig`, `is_valid_profile_name`, `AGENT_PROFILE_NAME_MAX_LENGTH`, `_parse_profile_entry` and `_parse_model_connections`. `KorvidConfig` carries `model_connections: ModelConnectionsConfig`. `tests/core/test_config_profiles.py` covers ordering, immutability, bounded/secret-refusing `options` and `auth.settings`, `config_error`, `unparsed` retention, unhashability, and the name rules.

**Steps**

- [ ] Step 1 — Confirm the landed state rather than trusting this document.

```bash
cd "$(git rev-parse --show-toplevel)"
git log --oneline -3
uv run pytest -p no:tach tests/core/test_config_profiles.py -q
```

Expected: `a71ee6cf feat: parse named agent model profiles` is in the log and the suite passes.

- [ ] Step 2 — Confirm the four properties later tasks depend on, so a regression here is caught before it is built on:

```bash
uv run python - <<'PY'
from korvid.core.config import ConnectionAuthConfig, ModelConnectionConfig, ModelConnectionsConfig
p = ModelConnectionConfig(model="x/y", options={"nested": {"a": 1}, "items": [1, 2]})
assert type(p.options["nested"]).__name__ == "mappingproxy", "freeze must recurse into mappings"
assert p.options["items"] == (1, 2), "freeze must turn sequences into tuples"
assert ModelConnectionConfig.__hash__ is None and ConnectionAuthConfig.__hash__ is None
c = ModelConnectionsConfig(active="b", profiles={"b": p, "a": p})
assert list(c.profiles) == ["b", "a"], "insertion order is the file's order"
assert c.active_profile is p
print("ok")
PY
```

**Dependencies:** none. **Blocks:** Task 2, Task 2B, Task 3.

---

### Task 2: Legacy agent configuration migration — **LANDED at `d7b69101`**

**Status:** already implemented and committed. It has one defect, corrected by Task 2B; do not fix it here and do not amend `d7b69101`.

`core/config.py` now holds `LEGACY_PROFILE_NAME`, `_LEGACY_OPENAI_COMPAT_NAMES`, `_LEGACY_REVIEW_NAMES`, `_LEGACY_OLLAMA_KEYS`, `_LEGACY_OLLAMA_NUMERIC_KEYS`, `_LEGACY_OLLAMA_STRICT_INT_KEYS`, `_LEGACY_AUTH_METHODS`, `_legacy_model_reference`, `_legacy_auth`, `_legacy_options`, `_legacy_ollama_options`, `_legacy_ollama_number`, `_migrate_azure_endpoint`, `_migrate_legacy_agent` and `_resolve_model_connections`.

**Steps**

- [ ] Step 1 — Confirm the landed state:

```bash
uv run pytest -p no:tach tests/core/test_config_profiles.py tests/core/test_config.py -q
```

- [ ] Step 2 — Read `_legacy_model_reference` and record the defect Task 2B fixes:

```bash
sed -n '/^def _legacy_model_reference/,/^def _legacy_auth/p' src/korvid/core/config.py
```

It returns `f"{provider}:{model}"`. The design mandates the standard slash form `provider/model`. Nothing consumes a reference yet, which is exactly why this is fixed now.

**Dependencies:** Task 1. **Blocks:** Task 2B.

---

### Task 2B: Correct model references to the standard `provider/model` slash form — **the next commit to write**

**Why now.** Task 3 writes references to disk, Task 6 hands them to `litellm.get_llm_provider`, and Task 10 renders them in search results. Every one of those consumers is downstream, so this is the last moment a reference change is a pure rename rather than a data migration. The colon form is not merely non-standard: colons occur *inside* model ids that korvid must support verbatim (`qwen3:8b`, `anthropic.claude-3-5-sonnet-20240620-v1:0`), so `provider:model` cannot be split unambiguously, while `provider/model` splits on the **first** `/` and matches LiteLLM, models.dev and the wider ecosystem.

**This is a correction commit on top of landed work.** Tasks 1 and 2 are already on the branch (`a71ee6cf`, `d7b69101`); they are not to be redone or amended. Task 2B is a *new* commit that fixes what they got wrong, and it must land before Task 3.

**What stays.** The migration-only alias map (`_LEGACY_OPENAI_COMPAT_NAMES`) stays exactly as it is. It is the single place korvid writes a vendor name down, it is reachable only from `_migrate_legacy_agent`, and Task 18 deletes it with the rest of the legacy path. Do not "generalise" it into a runtime provider table — that is the thing this whole plan removes.

**What moves — two vendor names, not one.** `load_config` today names a vendor in **two** places, and both must move into `_legacy_*` helpers:

1. It infers `auth_method = "device-login"` inline from `provider == "github-copilot"`. Move that into `_legacy_auth` alongside the rest of the legacy credential handling.
2. It reads the legacy `agent.ollama` sub-mapping by name — `agent_raw.get("ollama")` — to recover the native-thinking options. Move that read into a `_legacy_ollama_options` helper (or into `_migrate_legacy_agent` directly), so the vendor name lives in a migration function like every other legacy name.

Neither is cosmetic: Task 18's vendor guard exempts *named migration regions*, and `load_config` is 165 lines — the module's largest function. Exempting it would turn the guard's only real blind spot into its biggest one, so Task 18 asserts directly that `load_config` names no vendor. Doing both moves here, in the commit that already restructures legacy parsing, costs nothing; discovering the second one in Task 18 costs a re-plan. Verify the count is zero rather than assuming — the check is in Step 1 below and is re-run in Step 5.

**Files**

- `src/korvid/core/config.py` — `_legacy_model_reference`, its docstring, `_LEGACY_OPENAI_COMPAT_NAMES`' docstring, `_legacy_auth` (gains the device-login inference), and the legacy `agent.ollama` read moved out of `load_config`
- `tests/core/test_config_profiles.py` — reference expectations, plus the `httpx2` → `httpx` fix below
- `tests/core/test_config.py` — the legacy round-trip assertions that pin device-login inference and the native-thinking options still resolving after the moves

**Interfaces**

```python
#: The separator between the provider prefix and the model identifier in a
#: profile's `model` reference. Slash, not colon: colons occur *inside*
#: model identifiers (`qwen3:8b`, `...-v1:0`), so only a slash splits
#: unambiguously — and it is the form LiteLLM, models.dev and the rest of
#: the ecosystem already use.
MODEL_REFERENCE_SEPARATOR: str = "/"


def _legacy_model_reference(provider: str, model: str) -> str:
    """`provider/model` for a legacy provider name.

    Translated at this one parser boundary: nothing downstream branches on
    a legacy provider name again. The alias set below is migration-only —
    it is not a runtime provider table, and Task 18 deletes it.
    """
```

**Steps**

- [ ] Step 1 — Write the RED tests first. Edit `tests/core/test_config_profiles.py`.

`test_legacy_provider_names_translate_to_model_references` is already a `load_config` round-trip that writes YAML into `tmp_path` — that is the form that proves the migration *end to end*, so **keep it** and change only the `expected` column to the slash form. Replacing it with a direct call to `_legacy_model_reference` would trade an integration proof for a unit proof and lose coverage of the parser path that reaches it:

```python
@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai-compat", "gpt-4o-mini", "openai/gpt-4o-mini"),
        ("openai", "gpt-4o", "openai/gpt-4o"),
        ("vllm", "qwen", "openai/qwen"),
        ("azure", "gpt-4o", "azure/gpt-4o"),
        ("ollama", "llama3", "ollama/llama3"),
        ("github-copilot", "gpt-4o", "github-copilot/gpt-4o"),
        ("company-llm", "v2", "company-llm/v2"),
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
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.model == expected
```

Add the unit-level test *alongside* it, so the helper's own contract is pinned without a YAML round-trip. `_legacy_model_reference` is not currently imported by this module, so extend the existing `from korvid.core.config import (...)` block with it:

```python
def test_the_legacy_reference_helper_joins_with_a_slash() -> None:
    assert _legacy_model_reference("ollama", "llama3") == "ollama/llama3"
    assert _legacy_model_reference("vllm", "qwen") == "openai/qwen"
```

Add a case that only a slash separator can satisfy — this is the test that makes the change load-bearing rather than cosmetic:

```python
def test_a_model_identifier_containing_a_colon_survives_migration(tmp_path: Path) -> None:
    """`qwen3:8b` is a real Ollama tag. A colon separator would make the
    reference `ollama:qwen3:8b`, which cannot be split back into a
    provider and a model without guessing which colon was the separator.
    """
    path = _write(
        tmp_path,
        """
agent:
  provider: ollama
  model: qwen3:8b
  base_url: http://localhost:11434
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.model == "ollama/qwen3:8b"
    prefix, _, tag = profile.model.partition("/")
    assert (prefix, tag) == ("ollama", "qwen3:8b")
```

Then update the three remaining colon assertions in the legacy section:

- `test_legacy_entra_auth_becomes_provider_default` → `assert profile.model == "azure/gpt-4o"`
- `test_legacy_azure_api_key_keeps_the_azure_adapter` → `assert profile.model == "azure/gpt-4o"` and rewrite its docstring's `openai:` mentions to `openai/`
- `test_legacy_ollama_config_becomes_the_default_profile` → `assert profile.model == "ollama/llama3"`

The new-shape tests (`test_multiple_profiles_round_trip_into_the_domain` and the rest) write `model:` values themselves and are **not** a migration concern; still convert their literals to the slash form in the same commit so the file speaks one dialect. At the commit this task starts from there are **two** colon-form references in `src/korvid/core/config.py` (both inside `_legacy_model_reference`) and roughly three dozen lines carrying one in `tests/core/test_config_profiles.py`; all of them are corrected here, in a **new commit** — never by amending the commits that landed them.

Grep afterwards to prove none is left. **Two patterns are needed, because the source-side and test-side occurrences do not look alike.** The test-side pattern must not fire on a colon *inside* a model id (`ollama/qwen3:8b`) or on a host:port URL (`http://my-ollama:11434/v1`, which appears in `tests/ui/test_agent_off.py`), so it anchors on a provider name at the start of a literal or after a space:

```bash
# (a) test-side literals — measured 36 matching lines before the fix, 0 after
rg -n '(^|[ "])(openai|openai-compat|ollama|azure|anthropic|github-copilot|vllm|company-llm):[A-Za-z0-9]' \
  tests/core/test_config_profiles.py

# (b) source-side f-strings — measured 2 matches before the fix, 0 after
rg -n 'openai:\{|\{provider\}:\{model\}' src/korvid/core/config.py
```

Expected after the fix: no matches from **either**. Pattern (a) alone is not sufficient and must not be relied on: run against `src/korvid/core/config.py` it returns **zero matches both before and after the change**, because the two source occurrences are `f"openai:{model}"` and `f"{provider}:{model}"` — the character after the colon is `{`, not `[A-Za-z0-9]`. A grep that reads "clean" before the work has started proves nothing, so pattern (b) is the one that actually covers the source edit. Re-run both after Step 5.

- [ ] Step 2 — Fix the wrong-`httpx` Azure URL test in the same commit.

`test_the_azure_sdk_builds_the_url_from_the_resource_root` currently calls `pytest.importorskip("httpx2")` alongside `pytest.importorskip("openai")`. The reason to change it is **not** that `httpx2` is absent — it is in `uv.lock` already, via `mcp`. The reason is that it is the *wrong library*: `openai` 2.x builds its client on plain `httpx`, so an `httpx2.Response` is not a type that client accepts, and the test would be wrong the moment it stopped skipping. Change the `httpx2` references to `httpx`, which the `[agent]` extra declares in Task 5B:

```python
openai = pytest.importorskip("openai")
httpx = pytest.importorskip("httpx")
```

and the response construction to `httpx.Response(...)`. Leave the three parametrised rows unchanged — they were reproduced verbatim against `openai` 2.54.0 with plain `httpx`:

| `azure_endpoint` | request path |
| --- | --- |
| `https://x.openai.azure.com` | `/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21` |
| `https://x.openai.azure.com/openai/deployments/my-dep` | `/openai/deployments/my-dep/openai/chat/completions?api-version=2024-10-21` |
| `https://x.openai.azure.com/openai/v1` | `/openai/v1/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21` |

The test **stays skipped after this task**: `openai` is not a declared korvid dependency until Task 5B adds `litellm` to `[agent]`, and `importorskip("openai")` on line one short-circuits regardless of what the second line names. Fixing it now is still correct — this is the commit that touches the file, and leaving a known-wrong type in place until Task 5B means Task 5B debugs a failure this task could have prevented. Task 5B Step 9 lists it among the tests that must **no longer skip** once the extra is installed.

- [ ] Step 3 — Add the RED for the two vendor-name moves, then run the whole RED and read the failures.

The reference rename is covered by existing assertions, but the moves in "What moves" are behaviour-preserving refactors — without a test they can silently drop a legacy path, and the only thing that would notice is Task 18's guard, twelve tasks later. Add two round-trip tests to `tests/core/test_config.py` (the legacy-config file, not the profiles file), each driving `load_config` over a real legacy TOML so the assertion covers the *whole* parse, not the helper in isolation:

```python
def test_legacy_copilot_config_still_infers_device_login(tmp_path: Path) -> None:
    """The device-login inference survives the move into `_legacy_auth`.

    The inference used to live inline in `load_config`; this pins the
    behaviour to the migration result rather than to its location.
    """
    path = _write(
        tmp_path,
        """
[agent]
provider = "github-copilot"
model = "gpt-4o"
""",
    )
    profile = load_config(path).model_connections.active_profile
    assert profile is not None
    assert profile.auth.method == "device-login"
    assert profile.model == "github-copilot/gpt-4o"


def test_legacy_ollama_options_survive_the_move_out_of_load_config(
    tmp_path: Path,
) -> None:
    """The legacy `[agent.ollama]` sub-mapping still reaches the profile."""
    path = _write(
        tmp_path,
        """
[agent]
provider = "ollama"
model = "qwen3:8b"

[agent.ollama]
native_thinking = true
""",
    )
    profile = load_config(path).model_connections.active_profile
    assert profile is not None
    assert profile.model == "ollama/qwen3:8b"
    assert profile.options["native_thinking"] is True
```

Read the two helper names off the module before writing these — `_write` is whatever `tests/core/test_config.py` already uses to materialise a config file, and the legacy option key is whatever Task 2 landed. Do not invent either; if the names differ, use the ones in the file.

```bash
uv run pytest -p no:tach tests/core/test_config_profiles.py tests/core/test_config.py -q \
  -k "reference or colon or azure or ollama or copilot or device_login"
```

Expected failures: every reference assertion reports `AssertionError: assert 'openai:gpt-4o-mini' == 'openai/gpt-4o-mini'` (and siblings), and the two new tests fail on the reference form as well. `test_the_azure_sdk_builds_the_url_from_the_resource_root` reports **skipped**, before and after — that is the expected outcome here, not a regression, and it is why the reference assertions are this step's proof.

Note what these two tests are and are not: they are RED for the *rename* and a **regression net** for the moves. A pure refactor cannot have a failing-first test for its own sake — moving code correctly changes no behaviour. The value is that if the move drops the inference or the sub-mapping read, these fail immediately instead of in Task 18.

- [ ] Step 4 — GREEN. In `src/korvid/core/config.py`, add `MODEL_REFERENCE_SEPARATOR` next to `LEGACY_PROFILE_NAME` and change the two return statements:

```python
def _legacy_model_reference(provider: str, model: str) -> str:
    if provider in _LEGACY_OPENAI_COMPAT_NAMES:
        return f"openai{MODEL_REFERENCE_SEPARATOR}{model}"
    return f"{provider}{MODEL_REFERENCE_SEPARATOR}{model}"
```

Update the docstrings of `_legacy_model_reference` and `_LEGACY_OPENAI_COMPAT_NAMES` to say `openai/` rather than `openai:`, and correct the `azure` comment in `_LEGACY_OPENAI_COMPAT_NAMES` the same way.

Then move the two vendor names out of `load_config` (see "What moves"): the `provider == "github-copilot"` device-login inference goes into `_legacy_auth`, and the `agent_raw.get("ollama")` sub-mapping read goes into a `_legacy_ollama_options(agent_raw)` helper called from the legacy branch. `load_config` keeps the control flow and loses both literals. Confirm with the guard's own criterion before moving on:

```bash
uv run python - <<'EOF'
import ast, pathlib
src = pathlib.Path("src/korvid/core/config.py").read_text(encoding="utf-8")
tree = ast.parse(src)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "load_config")
vendors = ("openai", "ollama", "anthropic", "azure", "github-copilot",
           "github_copilot", "gemini", "bedrock", "vllm", "openai-compat")
bad = [(n.lineno, n.value) for n in ast.walk(fn)
       if isinstance(n, ast.Constant) and isinstance(n.value, str)
       and any(v in n.value.lower() for v in vendors)]
print(bad or "clean")
EOF
```

Expected: `clean`. Measured on the tree this task starts from, it prints two entries — the `"github-copilot"` comparison and the `"ollama"` key — which is the RED for this half of the step.

- [ ] Step 5 — Verify:

```bash
uv run pytest -p no:tach tests/core/test_config_profiles.py tests/core/test_config.py -q
uv run ruff check --fix src/korvid/core/config.py tests/core/test_config_profiles.py tests/core/test_config.py
uv run ruff format src/korvid/core/config.py tests/core/test_config_profiles.py tests/core/test_config.py
uv run mypy src/korvid/core/config.py
```

Then re-run **both** grep patterns from Step 1 and the `load_config` AST check from Step 4. All four must report clean.

- [ ] Step 6 — Commit:

```bash
git add src/korvid/core/config.py tests/core/test_config_profiles.py tests/core/test_config.py
git commit -m "fix: use the standard provider/model reference form

Model references are written as \`provider/model\`, matching LiteLLM,
models.dev and the rest of the ecosystem. A colon separator cannot be
split unambiguously because colons occur inside model identifiers
(\`qwen3:8b\`, \`...-v1:0\`). This corrects the two references that landed
in the previous commits, in a new commit rather than an amend.

The two vendor names left inline in load_config move into the legacy
migration helpers where the rest of the legacy vocabulary already
lives: the device-login inference into _legacy_auth, and the
[agent.ollama] sub-mapping read into its own helper. Round-trip tests
pin both behaviours through load_config so the move cannot silently
drop a legacy path.

The Azure URL-construction test asserted against httpx2 while the
openai client it drives is built on plain httpx; it now names the
right library. It stays skipped until the agent extra is installed.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Task 2. **Blocks:** Task 3 (the writer serialises references), Task 6 (the catalog splits them), Task 15 (the factory passes them to LiteLLM).

---

### Task 3: Profile writer and derived legacy scalars

**Files**

- `src/korvid/core/config.py` — `_thaw_config_value`, `_profile_to_raw`, `save_model_connections`, `LEGACY_AGENT_KEYS`, `_derive_legacy_scalars`, `_PREFIXES_WITHOUT_LEGACY_TRANSPORT`, `_legacy_azure_base_url`
- `tests/core/test_config_profiles.py` — writer and derivation tests
- `src/korvid/__main__.py` — persist through `save_model_connections`

**Interfaces**

```python
#: Agent-level keys the legacy shape owned. `save_model_connections` removes
#: them once it has written the new shape, so the first successful save
#: upgrades the file rather than leaving two shapes to disagree.
#: `enabled` is included: `active: null` is the new off switch.
LEGACY_AGENT_KEYS: tuple[str, ...] = (
    "provider",
    "model",
    "base_url",
    "api_key_env",
    "auth",
    "ollama",
    "options",
    "enabled",
)


def _thaw_config_value(value: object) -> object:
    """Undo `_freeze_config_value` recursively for serialization.

    `yaml.safe_dump` has no representer for `mappingproxy` and raises
    `RepresenterError`; tuples happen to serialize (SafeRepresenter maps
    `tuple` to `represent_list`) but round-trip back as lists anyway, so
    both are converted here rather than relying on that.
    """


def save_model_connections(path: Path, profiles: ModelConnectionsConfig) -> None:
    """Write `agent.active`/`agent.profiles`, preserving everything else.

    Read-modify-write: unrelated top-level keys, unrelated `agent.*` keys
    and every `unparsed` entry survive. Only the keys in
    `LEGACY_AGENT_KEYS` are removed, and only after the new shape is in
    place.
    """


def _derive_legacy_scalars(
    profiles: ModelConnectionsConfig, warnings: list[str]
) -> _LegacyScalars:
    """Project the active profile onto the pre-profile scalar fields.

    Temporary. It exists only so commit groups 1–3 stay buildable while
    the transport is still the legacy one, and Task 18 deletes it. It
    refuses rather than guesses: a profile whose provider prefix the
    legacy transport cannot serve yields no scalars and a warning naming
    the prefix and the task that will enable it.
    """
```

**Steps**

- [ ] Step 1 — RED. Add to `tests/core/test_config_profiles.py`:

```python
def test_saving_writes_only_the_new_shape_and_drops_the_legacy_keys(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
kube_context: prod
agent:
  provider: ollama
  model: llama3
  base_url: http://localhost:11434
  enabled: true
  ollama:
    num_ctx: 8192
""",
    )
    cfg = load_config(path)
    save_model_connections(path, cfg.model_connections)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["kube_context"] == "prod"
    assert set(raw["agent"]) == {"active", "profiles"}
    assert raw["agent"]["active"] == "default"
    assert raw["agent"]["profiles"]["default"]["model"] == "ollama/llama3"
    assert raw["agent"]["profiles"]["default"]["options"]["num_ctx"] == 8192


def test_a_nested_option_round_trips_through_the_writer(tmp_path: Path) -> None:
    """`_freeze_config_value` produces `mappingproxy`/`tuple`; `yaml.safe_dump`
    raises `RepresenterError` on the first and normalises the second. The
    writer must thaw recursively, and the result must reload equal."""
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
      options:
        nested:
          depth: 1
        items: [1, 2]
""",
    )
    before = load_config(path).model_connections
    save_model_connections(path, before)
    after = load_config(path).model_connections
    assert after.profiles["main"].options["nested"]["depth"] == 1
    assert after.profiles["main"].options["items"] == (1, 2)
    assert after == before


def test_saving_carries_unparsed_entries_back_verbatim(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: good
  profiles:
    good:
      model: openai/gpt-4o
    broken: {}
""",
    )
    cfg = load_config(path)
    assert set(cfg.model_connections.profiles) == {"good"}
    save_model_connections(path, cfg.model_connections)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(raw["agent"]["profiles"]) == {"good", "broken"}


def test_an_explicitly_removed_profile_does_not_come_back(tmp_path: Path) -> None:
    """A profile with a rejected `options` block is in *both* `profiles`
    and `unparsed`. Dropping it from one alone lets the writer re-emit it
    from the other, and the operator can never delete it."""
    path = _write(
        tmp_path,
        """
agent:
  active: good
  profiles:
    good:
      model: openai/gpt-4o
    rejected:
      model: openai/gpt-4o
      options:
        api_key: inline-secret-value
""",
    )
    cfg = load_config(path)
    assert cfg.model_connections.profiles["rejected"].config_error is not None
    assert "rejected" in cfg.model_connections.unparsed

    pruned = replace(
        cfg.model_connections,
        profiles={k: v for k, v in cfg.model_connections.profiles.items() if k != "rejected"},
        unparsed={k: v for k, v in cfg.model_connections.unparsed.items() if k != "rejected"},
    )
    save_model_connections(path, pruned)
    assert set(load_config(path).model_connections.profiles) == {"good"}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(raw["agent"]["profiles"]) == {"good"}


def test_derived_scalars_refuse_a_prefix_the_legacy_transport_cannot_serve(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: anthropic/claude-sonnet-4-5
""",
    )
    cfg = load_config(path)
    assert cfg.agent_enabled is False
    assert cfg.agent_provider is None
    assert any("anthropic" in w and "Task 15" in w for w in cfg.warnings)


def test_derived_scalars_reattach_the_azure_deployment_path(tmp_path: Path) -> None:
    """Group 1's transport is still the legacy string-concatenating one,
    which posts to `<base_url>/chat/completions`. The profile holds the
    resource root, so the projection has to rebuild what Task 2 stripped
    or every interim Azure request 404s."""
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: azure/gpt-4o
      endpoint: https://x.openai.azure.com
      auth:
        method: environment
        key: AZURE_OPENAI_API_KEY
      options:
        azure_deployment: my-dep
""",
    )
    cfg = load_config(path)
    assert cfg.agent_provider == "azure"
    assert cfg.agent_base_url == "https://x.openai.azure.com/openai/deployments/my-dep"


def test_a_profile_with_a_config_error_yields_no_legacy_scalars(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
      options:
        api_key: inline-secret-value
""",
    )
    cfg = load_config(path)
    assert cfg.agent_enabled is False
    assert any("rejected" in w for w in cfg.warnings)
```

Add `from dataclasses import replace` and `import yaml` to the test module's imports, and `save_model_connections` to the `korvid.core.config` import list.

- [ ] Step 2 — Run the RED:

```bash
uv run pytest -p no:tach tests/core/test_config_profiles.py -q -k "saving or nested_option or unparsed or removed or derived or config_error"
```

Expected: `ImportError: cannot import name 'save_model_connections'` collects as an error for the whole module — that is the RED. Do not proceed until you have seen it.

- [ ] Step 3 — GREEN, part 1: the writer.

```python
def _thaw_config_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_config_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw_config_value(item) for item in value]
    return value


def _profile_to_raw(profile: ModelConnectionConfig) -> dict[str, Any]:
    entry: dict[str, Any] = {"model": profile.model}
    if profile.endpoint is not None:
        entry["endpoint"] = profile.endpoint
    auth: dict[str, Any] = {"method": profile.auth.method}
    auth.update(cast("dict[str, Any]", _thaw_config_value(profile.auth.settings)))
    entry["auth"] = auth
    options = cast("dict[str, Any]", _thaw_config_value(profile.options))
    if options:
        entry["options"] = options
    return entry


def save_model_connections(path: Path, profiles: ModelConnectionsConfig) -> None:
    raw = _read_config_document(path)
    agent_value = raw.get("agent")
    agent: dict[str, Any] = agent_value if isinstance(agent_value, dict) else {}
    written: dict[str, Any] = {
        name: _profile_to_raw(profile) for name, profile in profiles.profiles.items()
    }
    for name, entry in profiles.unparsed.items():
        # Only entries the caller did not model. A name present in both is
        # written from `profiles`; a name in neither was deleted.
        if name not in written:
            written[name] = _thaw_config_value(entry)
    agent["active"] = profiles.active
    agent["profiles"] = written
    for key in LEGACY_AGENT_KEYS:
        agent.pop(key, None)
    raw["agent"] = agent
    _write_config_document(path, raw)
```

`_read_config_document`/`_write_config_document` are the existing atomic read/write helpers this module already uses for config persistence; reuse them rather than adding a second write path. If they do not exist under those names, use whichever helper the current `save_*` function in this module uses and keep the same temp-file-plus-rename and permission behaviour.

- [ ] Step 4 — GREEN, part 2: the derived scalars.

```python
#: Provider prefixes the *interim* legacy transport cannot serve. Between
#: this task and Task 15 the running transport is still the legacy one,
#: which speaks only bearer-token OpenAI-compatible HTTP, Azure and
#: Ollama. Anything else must disable the agent visibly rather than be
#: silently routed through a bearer-token client — sending an
#: `Authorization: Bearer` to a vendor that expects its own header is a
#: credential leak, not a degraded experience. Deleted in Task 18.
_PREFIXES_WITHOUT_LEGACY_TRANSPORT: frozenset[str] = frozenset(
    {"anthropic", "bedrock", "gemini", "vertex_ai", "cohere", "mistral", "groq", "xai"}
)


def _legacy_azure_base_url(profile: ModelConnectionConfig) -> str | None:
    """Rebuild the deployment-scoped URL the legacy transport needs."""
    if profile.endpoint is None:
        return None
    deployment = profile.options.get("azure_deployment")
    if not isinstance(deployment, str) or not deployment:
        return profile.endpoint
    return f"{profile.endpoint.rstrip('/')}/openai/deployments/{deployment}"
```

`_derive_legacy_scalars` then:

1. returns empty scalars when `profiles.active_profile is None`;
2. returns empty scalars **and** a warning when `profile.config_error is not None`
   (`f"the active profile was rejected: {profile.config_error} — the agent is disabled"`);
3. splits `profile.model` on the first `/`; a reference with no `/` yields empty
   scalars and a warning;
4. returns empty scalars and
   `f"the {prefix!r} provider needs the new transport (Task 15); the agent is disabled"`
   when the prefix is in `_PREFIXES_WITHOUT_LEGACY_TRANSPORT`;
5. otherwise projects `agent_enabled=True`, `agent_provider=prefix`,
   `agent_model=<tag>`, `agent_base_url=_legacy_azure_base_url(profile)` for
   `azure` and `profile.endpoint` otherwise, `agent_auth_method` from
   `profile.auth.method`, `agent_api_key_env` from `auth.settings["key"]` when
   the method is `environment`, and `agent_options` from `_thaw_config_value(profile.options)`.

Wire it into `load_config` so `KorvidConfig`'s legacy scalars come from the projection, and make sure `agent_options_error` is assigned **before** the `KorvidConfig(...)` call that reads it.

- [ ] Step 5 — Persist through the writer. In `src/korvid/__main__.py`, replace the existing agent-config save call with `save_model_connections(config_path, profiles)`. Keep the existing "applied now, reverts on restart" warning on `OSError`.

- [ ] Step 6 — Verify:

```bash
uv run pytest -p no:tach tests/core/test_config_profiles.py tests/core/test_config.py -q
uv run ruff check --fix src/korvid/core/config.py src/korvid/__main__.py tests/core/test_config_profiles.py
uv run ruff format src/korvid/core/config.py src/korvid/__main__.py tests/core/test_config_profiles.py
uv run mypy src/korvid/core/config.py src/korvid/__main__.py
uv run tach check
```

- [ ] Step 7 — Commit:

```bash
git add -A
git commit -m "feat: write agent model profiles and derive legacy scalars

save_model_connections emits only the new agent.active/agent.profiles shape,
thaws frozen mappings so yaml.safe_dump can represent them, carries
unmodelled entries back verbatim, and removes the legacy agent keys once
the new shape is in place.

The derived legacy scalars keep the pre-transport wiring buildable. They
refuse rather than guess: a rejected profile or a prefix the interim
transport cannot serve disables the agent with a warning instead of
misrouting a credential.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Task 2B. **Blocks:** Task 4, Task 11, Task 18.

---

### Task 4: Push the branch, and open the draft pull request only if instructed

`AGENTS.md`: *"Do NOT open a pull request without explicit human instruction."* The design authorises a draft PR after the config group so CI and the diff stay visible — but authorising a thing in a design document is not the same as being told to do it. Pushing the branch is unconditional; opening the PR is not.

**Steps**

- [ ] Step 1 — Push:

```bash
git push -u origin agents/provider-neutral-profiles
```

- [ ] Step 2 — Decide. Open the draft PR **only if** the human driving this work has said so in this session. If they have not, skip Step 3, record that the PR is pending an instruction, and continue with Task 5. Do not ask and wait — the branch is pushed either way, and **Task 20 Step 6 opens the PR at the end of the final commit group if this gate was skipped**. Exactly one pull request carries this change on either path; Task 20 checks for an existing PR before creating one.

- [ ] Step 3 — Under that instruction only:

```bash
gh pr create --draft \
  --base main \
  --head agents/provider-neutral-profiles \
  --title "Provider-neutral model profiles" \
  --body "$(cat <<'BODY'
Replaces korvid's single hard-coded provider configuration and CSP-oriented
`:ai` wizard with named model connection profiles.

Provider and model selection become data-driven: the catalog is read from
LiteLLM's shipped offline tables and optionally enriched from models.dev, and
routing is delegated to `litellm.get_llm_provider` / `litellm.acompletion`.
korvid ships no provider class table and no per-vendor extras.

`NativeAgentEngine`, `RequestGateway`, `OutboundPolicy`, `ToolHarness`, the
approval gate, the audit log and the outbound-snapshot contract are unchanged.

Draft while the remaining commit groups land. Design:
`docs/superpowers/specs/2026-09-05-provider-neutral-model-profiles-design.md`
BODY
)"
```

- [ ] Step 4 — Confirm CI started (only if Step 3 ran):

```bash
gh pr checks --watch --interval 30 || true
gh pr view --json number,isDraft,statusCheckRollup
```

**Dependencies:** Task 3. **Blocks:** nothing; later tasks push to the same branch.

---

## Commit group 2 — Data-driven model catalog (Tasks 5, 5B, 6–8)

### Task 5: The public, provider-free catalog vocabulary

This is the boundary the UI talks to. It must be expressible without naming a single vendor, without importing `korvid.providers`, and without importing `litellm` — otherwise the base TUI stops importing.

Note what is **not** here: there is no `descriptors()` returning a list of adapters, and no `ModelAdapterDescriptor`. A list of adapters is the hand-written table this whole change removes. Every question the UI needs answered is answered *per model reference*, from data.

**Files**

- `src/korvid/agent/model_profiles.py` (new)
- `src/korvid/providers/litellm_settings.py` (new, stdlib-only leaf)
- `tests/agent/test_model_profiles.py` (new)

**Interfaces**

```python
"""Public model-profile vocabulary (design §Public Agent Boundary).

`ui/` imports this module to render the setup wizard. It must never grow
an import of `korvid.providers` or of any model SDK: the base TUI has
neither, and the layer rules forbid the first outright.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from korvid.core.config import ConnectionAuthConfig, ModelConnectionConfig, ModelConnectionsConfig

__all__ = [
    "ConnectionAuthConfig",
    "ModelConnectionConfig",
    "ModelConnectionsConfig",
    "AuthMethodDescriptor",
    "DeviceLoginPrompt",
    "EndpointRequirement",
    "ModelCatalog",
    "ModelEntry",
    "ModelEntrySource",
    "SetupField",
    "SetupFieldKind",
    "SpecialFlow",
    "split_reference",
]


class SetupFieldKind(Enum):
    TEXT = "text"
    SECRET_REF = "secret_ref"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    CHOICE = "choice"


@dataclass(frozen=True, slots=True)
class SetupField:
    """One declarative prompt. Data, never executable UI."""

    key: str
    label: str
    kind: SetupFieldKind
    required: bool = False
    default: str | None = None
    choices: tuple[str, ...] = ()
    help_text: str | None = None


@dataclass(frozen=True, slots=True)
class AuthMethodDescriptor:
    id: str
    display_name: str
    fields: tuple[SetupField, ...] = ()


class EndpointRequirement(Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    UNSUPPORTED = "unsupported"


class ModelEntrySource(Enum):
    """Where a catalog entry came from. Display and provenance only."""

    LITELLM = "litellm"
    MODELS_DEV = "models.dev"
    ENDPOINT = "endpoint"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One connectable model, as data.

    `provider_id` is informational — it is rendered and used for grouping
    in search results. Nothing dispatches on it: routing is
    `litellm.get_llm_provider`'s job.

    Every capability field is independently unknown (`None`). A fact is
    set only where the source directly asserts the equivalent fact; it is
    never inferred from the name.
    """

    reference: str
    provider_id: str
    display_name: str
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    source: ModelEntrySource = ModelEntrySource.LITELLM
    credential_env_hints: tuple[str, ...] = ()
    endpoint_hint: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceLoginPrompt:
    verification_uri: str
    user_code: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class SpecialFlow:
    """A flow the standard transport cannot own, declared as data.

    Not a provider list. A flow claims *one* reference prefix, or a named
    boolean option on a reference it otherwise shares, and supplies auth
    and transport for exactly what it claims. It never contributes to
    model search as a vendor choice and never gates a reference it did
    not claim.
    """

    prefix: str
    display_name: str
    auth_methods: tuple[AuthMethodDescriptor, ...]
    option_fields: tuple[SetupField, ...] = ()
    endpoint: EndpointRequirement = EndpointRequirement.OPTIONAL
    claims_option: str | None = None


def split_reference(reference: str) -> tuple[str, str]:
    """Split `provider/model` on the **first** slash.

    Returns `("", reference)` when there is no slash: LiteLLM resolves a
    bare reference against its own default-provider rules, and korvid
    must not pretend to know better.
    """
    prefix, separator, tag = reference.partition("/")
    if not separator:
        return "", reference
    return prefix, tag


class ModelCatalog(ABC):
    """Everything the setup UI needs to know, answered from data."""

    @abstractmethod
    def search(self, query: str, *, limit: int = 50) -> tuple[ModelEntry, ...]:
        """Rank catalog entries against a free-text query. Never raises."""

    @abstractmethod
    def entry(self, reference: str) -> ModelEntry | None:
        """The catalog's record for an exact reference, or None."""

    @abstractmethod
    def auth_methods(
        self, reference: str, *, endpoint: str | None = None
    ) -> tuple[AuthMethodDescriptor, ...]:
        """Auth methods valid for this reference, most specific first.

        `endpoint` is the endpoint the operator has entered so far, or
        None if they have not entered one. It is here so the catalog can
        mirror the factory's `none`-auth rule **exactly** rather than
        approximating it: `none` is offered only when `endpoint` is a
        non-empty string, because a keyless request with no operator
        endpoint goes to whatever default host the SDK picks. Offering a
        method the factory will refuse at build time is a trap; deciding
        it from a provider table is impossible, because LiteLLM's data
        carries no host (see the API baseline table).

        The parameter is a plain string the caller already has, not a
        lookup — implementations must not route, resolve or fetch to
        answer it. The UI's endpoint stage therefore runs *before* its
        auth-method stage (Task 11).
        """

    @abstractmethod
    def option_fields(self, reference: str) -> tuple[SetupField, ...]:
        """Declarative option prompts for this reference."""

    @abstractmethod
    def endpoint_requirement(self, reference: str) -> EndpointRequirement:
        """Whether the setup UI must, may, or must not ask for an endpoint.

        Answered from the special-flow registry alone: a flow that
        declares a requirement wins, and everything else is OPTIONAL.
        There is no provider table behind this — LiteLLM's `model_cost`
        records carry no host field of any kind, so no data exists from
        which "this provider needs an endpoint" could be derived.
        OPTIONAL is also the honest answer: any reference may be pointed
        at a proxy, a gateway or a self-hosted clone.
        """

    @abstractmethod
    async def discover(self, profile: ModelConnectionConfig) -> tuple[ModelEntry, ...]:
        """Live-list models from the profile's endpoint. Best effort: an
        empty tuple means "type it yourself", never an error dialog."""

    @abstractmethod
    async def test(self, profile: ModelConnectionConfig) -> str:
        """Probe the profile and return a short human-readable result."""

    @abstractmethod
    async def begin_auth(self, profile: ModelConnectionConfig) -> DeviceLoginPrompt | None: ...

    @abstractmethod
    async def finish_auth(self, profile: ModelConnectionConfig) -> str | None: ...
```

And the stdlib-only leaf, which exists so the catalog, the factory, `registry.py` and `plugin_registry.py` can share constants without importing one another:

```python
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
RETIRED_PROVIDER_ALIASES: frozenset[str] = frozenset(
    {"openai-compat", "vllm", "github", "claude"}
)
```

There is deliberately **no** `install_hint()` here. korvid already has one, in `src/korvid/agent/install_hint.py`:

```python
def isolated_install_hint(*, feature: str) -> str: ...
```

and it says something this module could not: it tells the operator to reinstall `korvid[all,entra]==<version>` through `uv tool install --force`, because reinstalling only `[agent]` into a tool-managed environment silently **drops** whatever other extras they had. Writing a second, simpler hint here would regress that. `providers/` may import `korvid.agent` under the layer rules, so `litellm_runtime.py` imports the existing one:

```python
from korvid.agent.install_hint import isolated_install_hint
```

**Steps**

- [ ] Step 1 — RED. Create `tests/agent/test_model_profiles.py`:

```python
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    EndpointRequirement,
    ModelCatalog,
    ModelEntry,
    ModelEntrySource,
    SetupField,
    SetupFieldKind,
    SpecialFlow,
    split_reference,
)


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("openai/gpt-4o", ("openai", "gpt-4o")),
        ("ollama/qwen3:8b", ("ollama", "qwen3:8b")),
        ("bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
         ("bedrock", "anthropic.claude-3-5-sonnet-20240620-v1:0")),
        ("openrouter/openai/gpt-4o", ("openrouter", "openai/gpt-4o")),
        ("gpt-4o", ("", "gpt-4o")),
    ],
)
def test_a_reference_splits_on_the_first_slash_only(
    reference: str, expected: tuple[str, str]
) -> None:
    assert split_reference(reference) == expected


def test_catalog_entries_default_every_capability_to_unknown() -> None:
    entry = ModelEntry(reference="x/y", provider_id="x", display_name="y")
    assert entry.context_window_tokens is None
    assert entry.supports_tools is None
    assert entry.supports_reasoning is None
    assert entry.source is ModelEntrySource.LITELLM


def test_the_vocabulary_is_immutable() -> None:
    field = SetupField(key="k", label="l", kind=SetupFieldKind.TEXT)
    with pytest.raises(AttributeError, match="cannot assign"):
        field.key = "other"  # type: ignore[misc]  # proving frozen-ness


def test_a_special_flow_declares_data_not_behaviour() -> None:
    flow = SpecialFlow(
        prefix="example-flow",
        display_name="Example",
        auth_methods=(AuthMethodDescriptor(id="device-login", display_name="Device login"),),
        endpoint=EndpointRequirement.OPTIONAL,
    )
    assert flow.claims_option is None
    assert not [name for name in vars(type(flow)) if callable(getattr(flow, name, None))
                and not name.startswith("__")]


def test_the_public_boundary_imports_no_provider_or_model_sdk() -> None:
    """`ui/` imports this module, and the base install has neither
    `korvid.providers` on its allowed-import list nor `litellm` on disk."""
    source = Path("src/korvid/agent/model_profiles.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            if node.module.startswith("korvid."):
                imported.add(node.module)
    assert "litellm" not in imported
    assert not any(name.startswith("korvid.providers") for name in imported)


def test_the_catalog_contract_has_no_adapter_list() -> None:
    """A `descriptors()`-shaped method would reintroduce the compiled-in
    vendor table this design exists to remove."""
    names = {name for name in vars(ModelCatalog) if not name.startswith("_")}
    assert "descriptors" not in names
    assert {"search", "entry", "auth_methods", "option_fields",
            "endpoint_requirement", "discover", "test"} <= names
```

- [ ] Step 2 — Run the RED:

```bash
uv run pytest -p no:tach tests/agent/test_model_profiles.py -q
```

Expected: `ModuleNotFoundError: No module named 'korvid.agent.model_profiles'`.

- [ ] Step 3 — GREEN. Create `src/korvid/agent/model_profiles.py` and `src/korvid/providers/litellm_settings.py` exactly as in **Interfaces**.

- [ ] Step 4 — Verify, including the layer check (this is the first agent→core re-export, so `tach` matters here):

```bash
uv run pytest -p no:tach tests/agent/test_model_profiles.py -q
uv run ruff check --fix src/korvid/agent/model_profiles.py src/korvid/providers/litellm_settings.py tests/agent/test_model_profiles.py
uv run ruff format src/korvid/agent/model_profiles.py src/korvid/providers/litellm_settings.py tests/agent/test_model_profiles.py
uv run mypy src/korvid/agent/model_profiles.py src/korvid/providers/litellm_settings.py
uv run tach check
```

- [ ] Step 5 — Commit:

```bash
git add -A
git commit -m "feat: publish the provider-free model catalog vocabulary

agent/model_profiles.py gives the setup UI a way to ask what a model
reference needs without importing korvid.providers or any model SDK.

There is deliberately no descriptors() returning a list of adapters:
auth methods, option fields and endpoint requirements are answered per
reference, from data, so adding a provider is not a korvid source edit.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Task 2B. **Blocks:** Tasks 6, 7, 8, 9, 10, 11.

---

### Task 5B: Take the dependency, deterministically — *before* the first module that reads it

**Why here and not in commit group 4.** Tasks 6, 7 and 8 are the LiteLLM catalog: they import `litellm`, read `models_by_provider`, and translate `model_cost` records. If the dependency lands after them, their RED is `importorskip` → *skipped*, their GREEN is *skipped*, and three commits go onto the branch whose suites have never executed a single assertion against the library they exist to wrap. That is not TDD with a delayed gate; it is three untested commits and a later task that discovers all their bugs at once. Taking the dependency here — between the vocabulary (Task 5, pure dataclasses, no LiteLLM) and the first LiteLLM module (Task 6) — costs one extra commit in this group and buys a real RED and a real GREEN for every catalog task.

**The one cost, paid explicitly.** In this commit `litellm` is declared and *not yet imported by any korvid module*, so `uv run deptry src` reports **DEP002 (unused dependency)**. Rather than skipping the check or reordering around it, add a scoped, commented ignore here and **delete it in Task 6** — the commit that adds `providers/_litellm_import.py`, the module that actually imports it. The deletion is the proof: if Task 6's wrapper did not really import `litellm`, removing the ignore turns deptry red immediately.

`litellm` enters `[agent]`. This is the one task that changes the lockfile, and the lockfile can only be regenerated by the Relock workflow (see **Global Constraints**). Every command below is written to be run verbatim, with no shell variable carried between steps — each step re-derives what it needs, because steps run in separate processes.

**Files**

- `pyproject.toml`
- `uv.lock` (via Relock only)
- `tests/test_optional_extras.py` (extend)
- `tests/test_lockfile.py` (verify unchanged)

**Steps**

- [ ] Step 1 — Edit `pyproject.toml`. Replace the `agent` extra:

```toml
agent = [
  "litellm==1.98.0",
  "httpx>=0.27",
  "keyring>=25.7.0",
]
```

`httpx` stays explicit even though `litellm` requires `httpx>=0.28.0,<1.0`: `providers/models_dev.py` and `providers/endpoint_discovery.py` import it directly, and deptry rejects an undeclared direct import. Note that LiteLLM's floor is inside korvid's existing `httpx` family — **no `httpx2` is involved anywhere in this design**, which is what lets Task 2B repair the Azure URL test instead of leaving it skipped.

There are **no** `provider-openai`, `provider-anthropic` or any other per-vendor extras. Adding one would recreate the compiled-in vendor list in the dependency table.

Add the mypy override in the same edit:

```toml
[[tool.mypy.overrides]]
module = ["litellm.*"]
ignore_missing_imports = true
```

And the temporary deptry ignore, with the reason and its removal condition written into the file so nobody has to reconstruct the intent:

```toml
[tool.deptry.per_rule_ignores]
# `korvid` itself is declared for the console-script entry point.
# `litellm` is declared here one commit before the module that imports it
# (providers/_litellm_import.py, Task 6) so the catalog tasks have a real
# RED/GREEN instead of an importorskip. Task 6 REMOVES the litellm entry
# in the same commit that adds the importing module.
DEP002 = ["korvid", "litellm"]
```

Check the existing table before editing rather than assuming its contents — at the time of writing `DEP002 = ["korvid"]`, so this is an append, not a new section. If a `[tool.deptry.per_rule_ignores]` table already exists, extend it; do not add a second one.

- [ ] Step 2 — Push the branch so Relock has something to lock against:

```bash
git add pyproject.toml
git commit -m "build: add litellm to the agent extra

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git push -u origin HEAD
```

- [ ] Step 3 — Dispatch Relock **against this branch**, not `main`:

```bash
git rev-parse --abbrev-ref HEAD
gh workflow run relock.yml -f package=litellm -f base="$(git rev-parse --abbrev-ref HEAD)"
```

- [ ] Step 4 — Wait for the run to be *created* before watching it. `gh run watch` on a stale id watches the wrong run:

```bash
sleep 20
gh run list --workflow=relock.yml --limit 5 \
  --json databaseId,createdAt,status,headBranch \
  --jq '.[] | "\(.databaseId) \(.createdAt) \(.status) \(.headBranch)"'
```

Take the id whose `createdAt` is after the dispatch, then:

```bash
gh run watch <id> --exit-status
```

- [ ] Step 5 — Find the branch Relock pushed. It is named `relock/<timestamp>`, so re-derive it rather than remembering it:

```bash
git fetch origin '+refs/heads/relock/*:refs/remotes/origin/relock/*'
git for-each-ref --sort=-committerdate --count=5 \
  --format='%(refname:short) %(committerdate:iso8601)' refs/remotes/origin/relock
```

- [ ] Step 6 — Take **only** `uv.lock` from it onto the feature branch:

```bash
git checkout origin/relock/<timestamp> -- uv.lock
git status --short
```

`git status` must show `uv.lock` and nothing else. If it shows a `pyproject.toml` change, Relock locked a different base — stop and redo Step 3.

- [ ] Step 7 — Verify the lock locally before committing it:

```bash
uv sync --frozen --dev --all-extras
uv run pytest -p no:tach tests/test_lockfile.py -q
uv run python -c "import litellm; print(litellm.__version__)"
```

`tests/test_lockfile.py` is the existing PyPI-only assertion. It must pass **unchanged** — do not edit it.

- [ ] Step 8 — Extend `tests/test_optional_extras.py`:

```python
def test_the_agent_extra_declares_litellm_and_no_per_vendor_extras() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert any(spec.startswith("litellm") for spec in extras["agent"])
    vendor_shaped = [
        name for name in extras
        if name.startswith("provider-") or name in {"openai", "anthropic", "azure"}
    ]
    assert vendor_shaped == []


def test_the_deptry_ignore_for_litellm_is_marked_temporary() -> None:
    """The DEP002 ignore added here must be removed by Task 6.

    Pinning the comment is not the point; pinning that the ignore is
    *scoped* is. A DEP002 list that has grown a second unexplained entry
    is a check that has been switched off.
    """
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    ignores = data["tool"]["deptry"]["per_rule_ignores"]["DEP002"]
    assert set(ignores) <= {"korvid", "litellm"}


def test_the_base_install_does_not_import_litellm() -> None:
    """The TUI must start with only the base dependencies. Task 6's AST
    test proves one module imports litellm; this proves the base import
    graph never reaches that module."""
    ...


def test_a_missing_agent_extra_degrades_to_no_agent_rather_than_a_crash() -> None: ...


def test_requesting_the_agent_explicitly_without_the_extra_fails_with_a_hint() -> None:
    ...
    assert "korvid[all,entra]" in str(excinfo.value)
    assert "uv tool install --force" in str(excinfo.value)
```

`test_the_base_install_does_not_import_litellm` is written here but is **vacuously true until Task 6** — no korvid module imports `litellm` yet. That is fine and deliberate: it is the tripwire that must already be armed when the importing module arrives, so Task 6 cannot accidentally wire `litellm` into the base import graph and discover it in review. Do not assert it "fails first"; assert instead that it is present and passing, and Task 6's Step for the import wrapper re-runs it as a gate.

- [ ] Step 9 — **Prove the library is genuinely installed and importable, and that Task 2B's Azure test stopped skipping.** This is the whole point of taking the dependency early — from here on, "skipped" is a failure signal, not a normal state:

```bash
uv run python -c "
import litellm, openai, httpx
print(litellm.__version__, openai.__version__, httpx.__version__)
"
uv run pytest -p no:tach \
  tests/core/test_config_profiles.py::test_the_azure_sdk_builds_the_url_from_the_resource_root \
  -q -rs
```

`test_the_azure_sdk_builds_the_url_from_the_resource_root` must **stop skipping here and pass**. Task 2B corrected its `pytest.importorskip("httpx2")` to `importorskip("httpx")` — the right library for `openai` 2.x's client — but it stayed skipped because `importorskip("openai")` on the line above short-circuits until `litellm` pulls `openai` in. **This** is the commit that installs it, so this is the commit where a wrong `httpx` type assertion first surfaces. If it now fails rather than skips, Task 2B's correction was wrong; fix it here rather than carrying a known-broken assertion forward.

Do **not** write a step here asserting that `tests/providers/test_litellm_catalog.py` runs — that file does not exist yet. It is created in Task 6, which is precisely why the dependency lands first. Task 6's own verification step carries the `-rs` check that no LiteLLM suite is skipped.

- [ ] Step 10 — Full gate, then commit the lock:

```bash
make check
uv run deptry src
git add -A
git commit -m "build: lock litellm into the agent extra

Generated by the Relock workflow against this branch, so every artefact
URL resolves to PyPI; tests/test_lockfile.py is unchanged and still
enforces that.

The dependency lands before the first module that imports it so the
catalog tasks that follow have a real failing test and a real passing
one, rather than three commits whose suites importorskip themselves
into silence. The cost is one commit where deptry sees a declared but
unimported package; the DEP002 ignore is scoped, commented, and removed
again by the commit that adds the importing module.

litellm's base install is 55 distributions, including openai, boto3,
tiktoken and tokenizers. That weight is the price of not maintaining a
vendor table by hand, and it is confined to the optional [agent] extra -
the base TUI install is untouched, and a test proves the base import
graph never reaches litellm. docs/dev/ records the tradeoff.

There are deliberately no per-vendor extras: a provider-anthropic extra
would be the compiled-in vendor list again, wearing a packaging hat.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

- [ ] Step 11 — Close and delete the helper pull request Relock opened. It exists only to carry a lock that now lives on the feature branch. **Close it — never merge it**:

```bash
gh pr list --head "relock/<timestamp>" --json number,title --jq '.[] | "\(.number) \(.title)"'
gh pr close <number> --comment "Superseded: uv.lock carried onto the feature branch."
git push origin --delete relock/<timestamp>
```

**Dependencies:** Task 5 (vocabulary only — nothing here imports it, but the group's order is vocabulary → dependency → implementation). **Blocks:** Tasks 6, 7, 8, 13, 14, 15, 16 — every task that touches LiteLLM.

---

### Task 6: The LiteLLM offline catalog

The primary, always-available layer. It works air-gapped, because everything it reads ships inside the `litellm` wheel.

**Files**

- `src/korvid/providers/_litellm_import.py` (new) — the only module that executes `import litellm`
- `src/korvid/providers/litellm_runtime.py` (new) — the only module that imports `_litellm_import`
- `src/korvid/providers/litellm_catalog.py` (new)
- `tests/providers/test_litellm_catalog.py` (new)
- `tests/providers/test_litellm_offline_import.py` (new) — the subprocess socket probe

**Interfaces**

```python
"""The single `import litellm` in korvid — made offline and silent.

`import litellm` is not side-effect free. In 1.98.0 it calls
`get_model_cost_map(url=...)`, which performs a blocking HTTPS GET of
`model_prices_and_context_window.json` unless `LITELLM_LOCAL_MODEL_COST_MAP`
is already `"true"` in the environment, and warns to **stderr** through a
`StreamHandler` when that fetch fails. Both are unacceptable in a Textual
application: the terminal is korvid's canvas, and a blocking fetch at wiring
time is a startup stall that grows with every firewall between here and
GitHub.

Neither can be fixed after the import, which is why this is a separate
module rather than a few lines at the top of `litellm_runtime`: an import
sorter is free to move a third-party `import litellm` above any `korvid`
import in the same block, so the ordering has to be a *file* boundary. This
module applies no policy of its own — the lockdown lives in
`litellm_runtime`, which is this module's only importer.
"""

from __future__ import annotations

import logging
import os
from types import ModuleType

from korvid.agent.install_hint import isolated_install_hint

# Must be set BEFORE `import litellm`: LiteLLM reads it at module scope and
# never re-reads it. `setdefault`, not assignment, so an operator who
# deliberately exports "false" keeps the remote map.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

try:
    import litellm as _litellm  # noqa: E402 - must follow the environ line above
except ImportError as exc:  # pragma: no cover - exercised by the extras tests
    raise ImportError(isolated_install_hint(feature="the embedded agent")) from exc


def _detach_litellm_logging() -> None:
    """Stop LiteLLM writing onto the terminal korvid is drawing on.

    `litellm.verbose_logger` ships with a `StreamHandler` and
    `propagate=True`, so anything it logs lands in the middle of the TUI.
    A `NullHandler` keeps `logging` from installing a last-resort handler
    of its own.
    """
    names = ("LiteLLM", "LiteLLM Proxy", "LiteLLM Router")
    loggers = [getattr(_litellm, "verbose_logger", None)]
    loggers.extend(logging.getLogger(name) for name in names)
    for logger in loggers:
        if logger is None:
            continue
        for handler in list(logger.handlers):
            if isinstance(handler, logging.StreamHandler):
                logger.removeHandler(handler)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False


_detach_litellm_logging()

#: The imported module, re-exported under a name that makes the indirection
#: obvious at the call site.
litellm: ModuleType = _litellm
```

```python
"""The single import boundary for `litellm` (design §Lockdown).

Importing this module locks LiteLLM down before any call can be made, and
fails loudly if a flag it means to set no longer exists upstream. Nothing
else in korvid imports `korvid.providers._litellm_import`; a test walks the
source tree and asserts it.
"""

from __future__ import annotations

from typing import Any, Final

import openai as _openai

from korvid.providers._litellm_import import litellm as _litellm

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
_missing = tuple(name for name, _ in LOCKDOWN_FLAGS if not hasattr(_litellm, name))
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
    setattr(_litellm, _name, [] if isinstance(_value, list) else _value)

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
    return {
        provider: sorted(models)
        for provider, models in _litellm.models_by_provider.items()
    }


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
        params = _litellm.get_supported_openai_params(
            model=model, custom_llm_provider=provider
        )
    except Exception:  # noqa: BLE001 - a lookup miss must not break setup
        return ()
    return tuple(params or ())
```

```python
class LiteLLMModelCatalog(ModelCatalog):
    """`ModelCatalog` over LiteLLM's shipped tables.

    Args:
        flows: The special-flow registry (Task 8). Empty is valid and
            fully functional.
        enrichment: An optional metadata source (Task 7). `None` means
            "offline only", which is the air-gapped default.
        discovery: The bounded endpoint prober. Injected so the catalog
            stays testable without a network.
    """

    def __init__(
        self,
        *,
        flows: SpecialFlowRegistry | None = None,
        enrichment: ModelMetadataSource | None = None,
        discovery: EndpointDiscovery | None = None,
    ) -> None: ...
```

**Steps**

- [ ] Step 1 — RED. Create `tests/providers/test_litellm_catalog.py`:

```python
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from korvid.agent.model_profiles import (
    ModelConnectionConfig,
    EndpointRequirement,
    ModelEntrySource,
)

litellm = pytest.importorskip("litellm")

from korvid.providers.litellm_catalog import LiteLLMModelCatalog  # noqa: E402
from korvid.providers.litellm_runtime import (  # noqa: E402
    LOCKDOWN_FLAGS,
    ProviderSDKError,
    model_cost_entry,
    models_by_provider,
)

_SRC = Path("src/korvid")


def _imported_module_names(path: Path) -> list[str]:
    names: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_importing_the_runtime_locks_litellm_down() -> None:
    for name, expected in LOCKDOWN_FLAGS:
        assert getattr(litellm, name) == expected, name


def test_a_mapped_provider_error_prints_nothing_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`suppress_debug_info` is the only thing standing between LiteLLM
    and the terminal a Textual app is drawing on.

    Both `litellm_core_utils/exception_mapping_utils.py` and
    `litellm_core_utils/get_llm_provider_logic.py` call bare `print()`
    with ANSI colour codes, gated on nothing but
    `litellm.suppress_debug_info is False`. They never touch
    `litellm.verbose_logger`, so detaching its handlers in the import
    wrapper does not reach them. Drive a real mapped failure and assert
    the capture is empty, so a maintainer who trims the flag list as
    "noise control" fails here instead of corrupting the TUI.
    """
    capsys.readouterr()  # discard anything the imports above emitted
    with pytest.raises(Exception, match="(?i)provider|model|llm"):
        litellm.completion(
            model="definitely-not-a-real-provider/nope",
            messages=[{"role": "user", "content": "x"}],
        )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "\033[" not in captured.out + captured.err


def test_the_flag_that_protects_stdout_is_not_quietly_droppable() -> None:
    """Pin the flag by name. The test above proves the behaviour; this
    one names the mechanism, so removing the flag from LOCKDOWN_FLAGS
    fails with a message that says why it mattered."""
    assert ("suppress_debug_info", True) in LOCKDOWN_FLAGS


def test_the_runtime_reexports_the_base_class_that_actually_catches_errors() -> None:
    """`except litellm.exceptions.APIError` catches almost nothing.

    Measured on 1.98.0: only `APIError` itself subclasses it, while the
    error classes korvid must translate -- Authentication, RateLimit,
    NotFound, BadRequest, ContextWindowExceeded, Timeout,
    APIConnection, InternalServer, ServiceUnavailable, PermissionDenied
    -- share `openai.OpenAIError`. Catching the wrong base would make
    the whole REQUEST_SENT rule dead code.
    """
    must_be_caught = [
        "AuthenticationError",
        "RateLimitError",
        "NotFoundError",
        "BadRequestError",
        "ContextWindowExceededError",
        "Timeout",
        "APIConnectionError",
        "InternalServerError",
        "ServiceUnavailableError",
        "PermissionDeniedError",
    ]
    for name in must_be_caught:
        cls = getattr(litellm.exceptions, name)
        assert issubclass(cls, ProviderSDKError), name
    assert not issubclass(litellm.exceptions.AuthenticationError, litellm.exceptions.APIError)


def test_a_renamed_lockdown_flag_fails_the_import_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assign-then-read-back would keep passing after an upstream rename:
    `setattr` on a name litellm no longer uses just creates an unused
    attribute while the real sink stays open. The guard has to run first.
    """
    import importlib
    import types

    stub = types.SimpleNamespace(
        **{name: value for name, value in LOCKDOWN_FLAGS if name != "telemetry"},
        acompletion=None,
        get_llm_provider=None,
        exceptions=None,
        models_by_provider={},
        model_cost={},
    )
    monkeypatch.setattr(
        "korvid.providers._litellm_import.litellm", stub, raising=True
    )
    import korvid.providers.litellm_runtime as runtime

    with pytest.raises(ImportError, match="telemetry"):
        importlib.reload(runtime)


def test_exactly_one_korvid_module_imports_litellm() -> None:
    """The env var that makes the import offline has to be set in a file
    that runs first — an import sorter would reorder a plain top-level
    `import litellm` above any `korvid` import in the same block.
    """
    offenders = {
        str(path.relative_to(_SRC))
        for path in sorted(_SRC.rglob("*.py"))
        if any(
            name == "litellm" or name.startswith("litellm.")
            for name in _imported_module_names(path)
        )
    }
    assert offenders == {"providers/_litellm_import.py"}


def test_exactly_one_korvid_module_imports_the_wrapper() -> None:
    importers = {
        str(path.relative_to(_SRC))
        for path in sorted(_SRC.rglob("*.py"))
        if "korvid.providers._litellm_import" in _imported_module_names(path)
    }
    assert importers == {"providers/litellm_runtime.py"}


def test_provider_model_tables_are_normalized_to_sorted_lists() -> None:
    """Most shipped values are sets and a handful are lists; indexing a
    set raises `TypeError`, and set iteration order is not stable."""
    table = models_by_provider()
    assert table, "litellm shipped an empty provider table"
    assert all(isinstance(models, list) for models in table.values())
    assert all(models == sorted(models) for models in table.values())
    assert table["anthropic"][:1] == sorted(litellm.models_by_provider["anthropic"])[:1]


def test_no_test_asserts_a_catalog_size() -> None:
    """Table cardinality differs between the bundled data and the remote
    cost map and moves with every litellm patch release, so an exact-count
    assertion is a scheduled false failure. Membership and shape only."""
    table = models_by_provider()
    assert len(table) > 1
    assert "anthropic" in table


def test_the_provider_qualified_cost_key_wins_over_the_bare_one() -> None:
    """Both spellings exist in `model_cost`, and for a measurable minority
    of references they carry *different* facts, so a bare-first lookup
    reads another provider's record."""
    assert model_cost_entry("anthropic", "claude-sonnet-4-5") is not None
    assert model_cost_entry("ollama", "ollama/llama3") is not None
    assert model_cost_entry("openai", "definitely-not-a-model") is None

    divergent = next(
        (
            (provider, model)
            for provider, models in models_by_provider().items()
            for model in models
            if model in litellm.model_cost
            and f"{provider}/{model}" in litellm.model_cost
            and litellm.model_cost[model] != litellm.model_cost[f"{provider}/{model}"]
        ),
        None,
    )
    if divergent is not None:
        provider, model = divergent
        assert model_cost_entry(provider, model) == litellm.model_cost[
            f"{provider}/{model}"
        ]


def test_search_finds_a_known_model_by_substring() -> None:
    catalog = LiteLLMModelCatalog()
    results = catalog.search("claude-sonnet-4-5")
    references = [entry.reference for entry in results]
    assert "anthropic/claude-sonnet-4-5" in references


def test_search_is_bounded_and_deterministic() -> None:
    catalog = LiteLLMModelCatalog()
    first = catalog.search("gpt", limit=10)
    second = catalog.search("gpt", limit=10)
    assert 0 < len(first) <= 10
    assert [e.reference for e in first] == [e.reference for e in second]


def test_search_never_raises_on_junk() -> None:
    catalog = LiteLLMModelCatalog()
    assert catalog.search("") == () or len(catalog.search("")) <= 50
    assert catalog.search("\x00\x01 ?? []") == ()


def test_capabilities_are_translated_faithfully_and_unknowns_stay_none() -> None:
    catalog = LiteLLMModelCatalog()
    known = catalog.entry("anthropic/claude-sonnet-4-5")
    assert known is not None
    record = model_cost_entry("anthropic", "claude-sonnet-4-5")
    assert record is not None
    assert known.context_window_tokens == record.get("max_input_tokens")
    assert known.supports_tools is record.get("supports_function_calling")
    assert known.source is ModelEntrySource.LITELLM

    unknown = catalog.entry("openai/definitely-not-a-model")
    assert unknown is None


def test_litellms_github_copilot_provider_never_reaches_the_catalog() -> None:
    """Resolving `github_copilot/...` starts an interactive device login
    inside the routing call. Offering those ids in search would put that
    one keystroke away, so the provider is excluded or rewritten onto
    korvid's own prefix."""
    catalog = LiteLLMModelCatalog()
    references = {entry.reference for entry in catalog.search("copilot", limit=50)}
    assert not any(ref.startswith("github_copilot/") for ref in references)
    assert "github_copilot" in litellm.models_by_provider, (
        "litellm stopped shipping the provider; the exclusion is now dead code"
    )


@pytest.mark.parametrize(
    "reference",
    ["github_copilot/gpt-4o", "github-copilot/gpt-4o"],
)
def test_per_reference_answers_never_route(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    """`auth_methods`, `option_fields` and `endpoint_requirement` render
    once per visible search row. A routing call there is slow for every
    reference and, for a claimed prefix, starts a device login."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_llm_provider must not be called here")

    monkeypatch.setattr(
        "korvid.providers.litellm_runtime.get_llm_provider", _explode
    )
    catalog = LiteLLMModelCatalog()
    assert catalog.auth_methods(reference)
    assert catalog.auth_methods(reference, endpoint="http://localhost:8080")
    assert catalog.option_fields(reference) is not None
    assert catalog.endpoint_requirement(reference) in EndpointRequirement


def test_a_manually_typed_reference_is_usable_even_when_unknown() -> None:
    catalog = LiteLLMModelCatalog()
    entry = catalog.manual_entry("company/internal-v2")
    assert entry.source is ModelEntrySource.MANUAL
    assert entry.reference == "company/internal-v2"
    assert entry.supports_tools is None


def test_every_reference_offers_the_generic_auth_methods() -> None:
    catalog = LiteLLMModelCatalog()
    ids = {m.id for m in catalog.auth_methods("openai/gpt-4o")}
    assert {"environment", "keyring", "provider-default"} <= ids


@pytest.mark.parametrize(
    "reference",
    ["openai/gpt-4o", "anthropic/claude-sonnet-4-5", "ollama/llama3", "company/internal-v2"],
)
def test_none_auth_is_offered_only_once_an_endpoint_is_known(reference: str) -> None:
    """The catalog mirrors the factory's rule exactly, for every reference.

    Keyless is refused with no endpoint and allowed with one — including
    for `ollama/llama3`, which the earlier default-host rule wrongly
    refused, and excluding `openai/gpt-4o`, which it wrongly allowed.
    Parametrizing over both a hosted and a local reference is the point:
    the answer must depend on the endpoint argument alone, never on the
    provider prefix.
    """
    catalog = LiteLLMModelCatalog()
    assert "none" not in {m.id for m in catalog.auth_methods(reference)}
    assert "none" not in {m.id for m in catalog.auth_methods(reference, endpoint="")}
    assert "none" in {
        m.id for m in catalog.auth_methods(reference, endpoint="http://localhost:11434")
    }


def test_the_catalogs_none_rule_names_no_provider() -> None:
    """A provider-shaped set anywhere near this rule is the bug the
    default-host inversion came from. Assert on the parsed module, not on
    a substring: a comment mentioning a vendor is fine, a frozenset of
    vendor names is not."""
    import korvid.providers.litellm_catalog as module

    tree = ast.parse(inspect.getsource(module))
    vendors = {"openai", "anthropic", "azure", "gemini", "bedrock", "ollama", "groq", "xai"}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            continue
        literals = {
            e.value.lower()
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
        assert not (literals & vendors), f"provider set at line {node.lineno}: {literals}"


def test_an_environment_auth_method_asks_for_a_reference_not_a_secret() -> None:
    catalog = LiteLLMModelCatalog()
    method = next(m for m in catalog.auth_methods("openai/gpt-4o") if m.id == "environment")
    assert [f.key for f in method.fields] == ["key"]
    assert method.fields[0].kind.value == "secret_ref"


def test_credential_env_hints_are_offered_but_never_read() -> None:
    """A hint tells the operator which variable to *name*. The catalog
    must not read the variable — that is the factory's job, and only for
    a profile that explicitly asked for it."""
    catalog = LiteLLMModelCatalog()
    entry = catalog.entry("anthropic/claude-sonnet-4-5")
    assert entry is not None
    assert all(hint.isupper() for hint in entry.credential_env_hints)


@pytest.mark.parametrize(
    "reference",
    ["openai/gpt-4o", "azure/gpt-4o", "hosted_vllm/qwen", "company/internal-v2"],
)
def test_endpoint_is_optional_for_every_reference_no_flow_claims(reference: str) -> None:
    """OPTIONAL is the only honest default.

    LiteLLM ships no host data (the `model_cost` records carry no
    api_base/base_url/host key at all), so nothing can distinguish
    "needs an endpoint" from "does not". Azure is included deliberately:
    an earlier revision asserted REQUIRED for it from a hand-built
    frozenset, which is the compiled-in provider table this design
    removes. Azure's real requirement is expressed where it belongs — the
    factory refuses an Azure profile with no endpoint at build time.
    """
    catalog = LiteLLMModelCatalog()
    assert catalog.endpoint_requirement(reference) is EndpointRequirement.OPTIONAL


def test_a_flow_declaration_is_the_only_source_of_a_non_optional_requirement() -> None:
    """Task 8 composes the flow registry in; here, with no flows, every
    answer is OPTIONAL. The flow-driven REQUIRED/UNSUPPORTED cases are
    asserted in Task 8's suite against a real registered flow rather than
    against a table this module does not own."""
    catalog = LiteLLMModelCatalog()
    answers = {
        catalog.endpoint_requirement(r)
        for r in ("openai/gpt-4o", "ollama/llama3", "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0")
    }
    assert answers == {EndpointRequirement.OPTIONAL}


async def test_discovery_without_an_endpoint_returns_nothing_rather_than_raising() -> None:
    catalog = LiteLLMModelCatalog()
    profile = ModelConnectionConfig(model="openai/gpt-4o")
    assert await catalog.discover(profile) == ()
```

Create `tests/providers/test_litellm_offline_import.py` in the same commit. It has to run in a **fresh subprocess**: `litellm` may already be imported by another test in the same session, and a module-cached import records no connections and proves nothing.

```python
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("litellm")

_PROBE = textwrap.dedent(
    """
    import socket
    import sys

    attempts = []

    def _refuse(self, address):  # noqa: ANN001, ANN202
        attempts.append(address)
        raise OSError("network disabled for this probe")

    socket.socket.connect = _refuse
    socket.socket.connect_ex = lambda self, address: (attempts.append(address), 1)[1]

    from korvid.providers.litellm_runtime import models_by_provider

    table = models_by_provider()
    assert table, "empty provider table"
    total = sum(len(models) for models in table.values())
    assert total > 0

    print(len(attempts))
    """
)


def test_importing_the_provider_layer_opens_no_socket() -> None:
    """`import litellm` fetches the remote cost map over HTTPS unless
    `LITELLM_LOCAL_MODEL_COST_MAP` is already set. Measured on 1.98.0:
    4 connections to 185.199.x.x:443 without it, 0 with it. The wrapper
    sets it before the import, which is the only place it can be set.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0", result.stdout


def test_litellm_logging_cannot_reach_the_terminal() -> None:
    """LiteLLM ships `verbose_logger` with a `StreamHandler` and
    `propagate=True`; in a Textual app that is a corrupted screen."""
    import logging

    import litellm

    import korvid.providers.litellm_runtime  # noqa: F401 - import applies the fix

    logger = litellm.verbose_logger
    assert not any(
        type(h) is logging.StreamHandler for h in logger.handlers
    ), logger.handlers
    assert logger.propagate is False
```

- [ ] Step 2 — Run the RED:

```bash
uv run pytest -p no:tach tests/providers/test_litellm_catalog.py -q -rs
```

Expected: a genuine collection failure — `ModuleNotFoundError: No module named 'korvid.providers.litellm_catalog'` — **not** a skip. Task 5B installed `litellm`, so the module-level `pytest.importorskip("litellm")` passes through and every test in this file actually executes. Confirm with `-rs` that the summary lists **no skips for this file**; a skip here means the `[agent]` extra did not install and Task 5B is unfinished. That is precisely why the dependency was moved ahead of this task: without it, both the RED and the GREEN of Tasks 6–8 would be the word "skipped".

Keep the four tests that do not need `litellm` — `test_exactly_one_korvid_module_imports_litellm`, `test_exactly_one_korvid_module_imports_the_wrapper` and the two vocabulary tests — **above** the `importorskip` anyway, so they run even in a base-only environment. They are the ones that hold the import invariant, and an invariant that only holds where an optional extra is installed is not an invariant.

- [ ] Step 3 — GREEN, part 1: `src/korvid/providers/_litellm_import.py` and `src/korvid/providers/litellm_runtime.py`, exactly as in **Interfaces**. Note the deliberate deviations from house style, each with its reason inline:

- `# noqa: E402` on `import litellm` in the wrapper — the import *must* follow the `os.environ.setdefault` line, and E402 exists to stop exactly that ordering. The reason is written on the line. This is why the wrapper is its own file: in `litellm_runtime.py` an import sorter would hoist a third-party `import litellm` above the `korvid` imports in the same block and the env var would be set too late, silently and invisibly in a diff nobody reads as behavioural.
- `except Exception` in `supported_params` and around `get_model_info` — LiteLLM raises a **plain `Exception`** for an unmapped model (`This model isn't mapped yet.`), so there is no narrower type to catch. Both are `# noqa: BLE001` with the reason written out.
- reading `_litellm._async_success_callback` — a private attribute. It is a *callback sink*, and leaving it unset would leave a channel open. The name is pinned by `LOCKDOWN_FLAGS`, checked with `hasattr` **before** assignment and asserted by a test, so a rename in LiteLLM fails the import loudly instead of silently reopening the channel.

- [ ] Step 4 — GREEN, part 2: `src/korvid/providers/litellm_catalog.py`.

Build the index once, lazily, on first use:

```python
#: LiteLLM's own spelling for the Copilot provider. Its ids ship
#: already-qualified (`github_copilot/claude-haiku-4.5`), and resolving the
#: prefix starts an interactive device login *inside* the routing call, so
#: the entries are re-prefixed onto korvid's own claimed spelling rather
#: than offered as LiteLLM writes them.
_LITELLM_COPILOT_PROVIDER: Final = "github_copilot"
_KORVID_COPILOT_PREFIX: Final = "github-copilot"


def _build_index(self) -> tuple[ModelEntry, ...]:
    entries: list[ModelEntry] = []
    for provider, models in models_by_provider().items():
        for model_id in models:
            if model_id == "sample_spec":
                continue
            record = model_cost_entry(provider, model_id)
            mode = record.get("mode") if record else None
            if mode is not None and mode != "chat":
                # Image, embedding, rerank and audio entries share the
                # table. Offering them as chat models would be a lie.
                continue
            reference = (
                model_id
                if model_id.startswith(f"{provider}/")
                else f"{provider}/{model_id}"
            )
            entry_provider = provider
            if provider == _LITELLM_COPILOT_PROVIDER:
                if self._flows.claim(f"{_KORVID_COPILOT_PREFIX}/") is None:
                    # No flow owns Copilot in this installation, so there
                    # is nothing safe to route these to. Drop them rather
                    # than offer a reference whose resolution blocks on a
                    # device-login poll.
                    continue
                _, tag = split_reference(reference)
                reference = f"{_KORVID_COPILOT_PREFIX}/{tag}"
                entry_provider = _KORVID_COPILOT_PREFIX
            entries.append(self._entry_from(entry_provider, reference, record))
    return tuple(entries)
```

No count is written down here and none is asserted: the bundled table and the remote cost map are different sizes, and both move with every LiteLLM patch release. `_entry_from` translates only direct assertions:

```python
ModelEntry(
    reference=reference,
    provider_id=provider,
    display_name=split_reference(reference)[1],
    context_window_tokens=_positive_int(record.get("max_input_tokens")),
    max_output_tokens=_positive_int(record.get("max_output_tokens")),
    supports_tools=_strict_bool(record.get("supports_function_calling")),
    supports_reasoning=_strict_bool(record.get("supports_reasoning")),
    source=ModelEntrySource.LITELLM,
    credential_env_hints=self._env_hints(provider),
)
```

`_strict_bool` returns `None` for anything that is not exactly `True`/`False` — never a truthiness coercion, because "the table has no opinion" and "the table says no" must stay distinguishable. `_positive_int` rejects `bool` (an `int` subclass) and non-positive values.

Search ranks with a pure, deterministic key — exact reference, then prefix match on the model tag, then substring, then the reference alphabetically — and always slices to `limit`. It lowercases the query, strips it, and returns `()` for a query with no alphanumeric character.

**`auth_methods`, `option_fields` and `endpoint_requirement` answer from static data and never call `get_llm_provider`.** They render once per visible search row, so a routing call there is slow for every reference — and for a claimed prefix it is the device-login hazard itself.

`auth_methods(reference, *, endpoint=None)` returns the generic descriptors, minus `device-login` unless a special flow claims the reference and declares it, and minus `none` unless `endpoint` is a non-empty string:

```python
def auth_methods(
    self, reference: str, *, endpoint: str | None = None
) -> tuple[AuthMethodDescriptor, ...]:
    methods = [m for m in _GENERIC_AUTH_METHODS if m.id != "none"]
    if endpoint:
        methods.append(_NONE_AUTH_METHOD)
    return tuple(methods)
```

That single `if endpoint:` is the whole rule, and it is deliberately the *same expression* the factory evaluates in Task 15 — not an approximation of it, and not a provider lookup. The UI must never offer a combination the factory refuses, and the only way to guarantee that is for both to test one field. An earlier revision gated `none` on "the provider has a default host", derived from `get_llm_provider`'s `dynamic_api_base`; measured on 1.98.0 that field is `None` for `openai`, `anthropic`, `azure`, `gemini` and `bedrock` and non-`None` for `ollama`, so the rule offered keyless auth for `api.openai.com` and withheld it from a local Ollama. Do not reintroduce a provider dimension here in any form.

`option_fields(reference)` returns the flow's fields when a flow claims it, plus the generic numeric knobs LiteLLM accepts for that provider derived from `supported_params(...)` — `temperature`, `max_tokens`, `seed`, `timeout` — as `SetupField`s, and `api_version` when `supported_params` reports it.

`endpoint_requirement(reference)` is answered from the **special-flow registry alone**:

```python
def endpoint_requirement(self, reference: str) -> EndpointRequirement:
    flow = self._flows.claim(reference) if self._flows else None
    if flow is not None:
        return flow.endpoint_requirement
    return EndpointRequirement.OPTIONAL
```

1. A claiming flow's declared requirement wins outright — that is the only way `REQUIRED` or `UNSUPPORTED` is ever produced (Copilot declares `UNSUPPORTED`; the native-thinking Ollama flow declares `REQUIRED`).
2. Everything else is `OPTIONAL`.

**There is no provider table, and there cannot be one.** The previous revision derived a `_PROVIDERS_NEEDING_AN_ENDPOINT` frozenset from "the providers whose `model_cost` records carry no vendor host" — but `model_cost` records carry **no host field at all**: no `api_base`, `base_url`, `host`, `endpoint` or `url` key exists on any record in 1.98.0 (the only near misses are `supported_endpoints`, a list of API *paths*, and the `supports_url_context` boolean). Every provider would satisfy "carries no vendor host", so the frozenset was either empty or the whole provider list depending on how the second, equally unavailable condition was implemented. The other candidate source, `get_complete_url`, is a per-provider handler method requiring an instantiated config object and is unusable for a bulk table.

Nor should there be one. `OPTIONAL` is the *correct* answer for an unclaimed reference: any model may be served through a corporate gateway, an LLM proxy or a self-hosted clone, so an endpoint is always permitted and never derivable as mandatory. Azure genuinely does need one — and that requirement is expressed where the information exists and the consequence is real: the factory refuses to build an Azure profile without an endpoint at build time (Task 15), with a message naming the missing field. A UI hint is not the enforcement point; the build is.

`_env_hints(provider)` reads the enrichment source when one is injected and otherwise returns `()`. It never reads `os.environ`.

`manual_entry(reference)` is a plain constructor returning `ModelEntry(..., source=ModelEntrySource.MANUAL)`; it is on the concrete class rather than the ABC, because "the operator typed something" is not a question the UI asks the catalog to *answer*.

`discover`, `test`, `begin_auth` and `finish_auth` are stubs in this task: `discover` returns `()` when `self._discovery is None` or the profile has no endpoint, `test` raises `NotImplementedError` and both auth methods return `None`. Task 8 fills them in. State that in the docstrings so a reviewer does not read them as finished.

- [ ] Step 5 — Verify:

```bash
uv run pytest -p no:tach tests/providers/test_litellm_catalog.py -q
uv run ruff check --fix src/korvid/providers/ tests/providers/test_litellm_catalog.py
uv run ruff format src/korvid/providers/ tests/providers/test_litellm_catalog.py
uv run mypy src/korvid/providers/litellm_catalog.py src/korvid/providers/litellm_runtime.py
uv run tach check
```

`mypy` passes here without special handling: Task 5B already added the `[[tool.mypy.overrides]]` entry for `litellm.*` alongside the dependency, so there is no missing-import noise to work around and no reason to reach for a blanket `# type: ignore`. If mypy still reports `litellm` as missing, the extra is not installed — fix the environment, not the annotation.

- [ ] Step 5b — **Remove the temporary deptry ignore Task 5B added, in this commit.** This module is the one that imports `litellm`, so the declared-but-unused window closes here:

```toml
[tool.deptry.per_rule_ignores]
# `korvid` itself is declared for the console-script entry point.
DEP002 = ["korvid"]
```

```bash
uv run deptry src
```

Expected: clean. If DEP002 fires for `litellm` after the removal, `_litellm_import.py` is not importing it — which is a real defect this step exists to catch, not a reason to restore the ignore. `test_the_deptry_ignore_for_litellm_is_marked_temporary` from Task 5B keeps passing (`{"korvid"} ⊆ {"korvid", "litellm"}`); tighten it here to `== ["korvid"]` so the ignore cannot quietly come back.

- [ ] Step 6 — Commit:

```bash
git add -A
git commit -m "feat: build the model catalog from LiteLLM's offline tables

providers/_litellm_import.py is the only module in korvid that executes
import litellm. It sets LITELLM_LOCAL_MODEL_COST_MAP before the import,
because litellm fetches the remote cost map over HTTPS at import time
otherwise, and detaches litellm's StreamHandlers so nothing it logs lands
on the terminal a Textual app is drawing on. Neither can be fixed after
the import, which is why it is a separate module: an import sorter would
otherwise hoist the import above the environment line.

providers/litellm_runtime.py imports that wrapper and applies the
lockdown, checking each flag name with hasattr before assigning so an
upstream rename fails the import instead of silently leaving a callback
sink open.

The catalog reads models_by_provider, model_cost and the supported-params
allowlist, entirely offline, so model search works air-gapped. No table
size is asserted anywhere: the bundled data and the remote map differ and
both move with every litellm release. Capability facts are copied only
where the table directly asserts them; anything else stays unknown.

model_cost is keyed both bare and provider-qualified, and for some
references both exist with different facts, so lookups try the qualified
key first.

litellm's github_copilot entries are excluded or re-prefixed onto korvid's
own claimed spelling: resolving that prefix starts an interactive device
login inside the routing call.

models_by_provider ships a mix of sets and lists, so every read is
normalized through sorted() — indexing the raw value raises TypeError and
set iteration order is not deterministic.

The temporary DEP002 ignore that Task 5B added for the one commit where
litellm was declared but not yet imported is removed here, by the commit
that imports it. deptry passing after the removal is the proof that the
wrapper reaches the library it claims to wrap.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 5, 5B (the dependency must be installed for this suite to execute). **Blocks:** Tasks 7, 8, 15.

---

### Task 7: Bounded, cached, optional models.dev enrichment

LiteLLM's tables are excellent for routing facts and thin on human-facing metadata — no descriptions, no release dates, no per-provider credential-variable names. models.dev publishes exactly that as one MIT-licensed JSON document, with no Python SDK to depend on.

This layer is **enrichment only**. Every rule below exists because a metadata source that can affect routing, block startup, or carry a prompt is a security problem, not a convenience.

**Files**

- `src/korvid/providers/models_dev.py` (new)
- `tests/providers/test_models_dev.py` (new)

**Interfaces**

```python
"""Optional, bounded metadata enrichment from models.dev.

Contract (design §Model Catalog Architecture, layer 2):

- Never fetched at startup, never awaited on any hot path.
- Never carries a credential, a prompt, a tool argument, a model
  reference the operator has selected, or any other korvid state — the
  request is a bare conditional GET of one public document.
- Never influences routing. It may add a description, a release date or
  a credential-variable *hint*; it can never change which endpoint a
  request goes to or which parameters are sent.
- A failure is silent and total: the catalog falls back to the cache,
  then to LiteLLM's bundled tables, and korvid stays fully usable.
"""

from __future__ import annotations

#: One conditional GET of one public document. No query string, ever.
MODELS_DEV_URL: Final[str] = "https://models.dev/api.json"

#: The document measured 4,473,344 bytes on 2026-09-05. The ceiling is a
#: little under 3x that, so ordinary growth does not trip it but a
#: redirect to something unbounded does.
MAX_RESPONSE_BYTES: Final[int] = 12 * 1024 * 1024

#: Whole-request budget. Enrichment is never worth making a human wait.
REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0

#: Revalidate at most daily; serve the cache unconditionally in between.
CACHE_TTL_SECONDS: Final[int] = 24 * 60 * 60

CACHE_FILENAME: Final[str] = "models-dev.json"


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """The subset korvid renders. Everything else is discarded on parse."""

    reference: str
    display_name: str
    description: str | None = None
    release_date: str | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    credential_env_hints: tuple[str, ...] = ()


class ModelMetadataSource(ABC):
    """What the catalog depends on. Keeps HTTP out of the catalog."""

    @abstractmethod
    def metadata(self, reference: str) -> ModelMetadata | None: ...

    @abstractmethod
    def env_hints(self, provider_id: str) -> tuple[str, ...]: ...


class ModelsDevSource(ModelMetadataSource):
    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        client_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None: ...

    async def refresh(self) -> RefreshOutcome:
        """Explicitly revalidate. Called only from the setup UI's
        "refresh model metadata" action — never at startup."""


class RefreshOutcome(Enum):
    UPDATED = "updated"
    NOT_MODIFIED = "not-modified"
    CACHED = "cached"       # TTL not expired; no request made
    UNAVAILABLE = "unavailable"  # network/parse failure; stale data kept


def default_cache_path() -> Path:
    """`$XDG_CACHE_HOME/korvid/models-dev.json`, falling back to the
    platform convention: `~/Library/Caches` on macOS,
    `%LOCALAPPDATA%` on Windows, `~/.cache` elsewhere."""
```

**Steps**

- [ ] Step 1 — RED. Create `tests/providers/test_models_dev.py`. These are the invariants, not a smoke test:

```python
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from korvid.providers.models_dev import (
    CACHE_TTL_SECONDS,
    MAX_RESPONSE_BYTES,
    MODELS_DEV_URL,
    REQUEST_TIMEOUT_SECONDS,
    ModelsDevSource,
    RefreshOutcome,
    default_cache_path,
)

httpx = pytest.importorskip("httpx")

_DOCUMENT = {
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic",
        "env": ["ANTHROPIC_API_KEY"],
        "models": {
            "claude-sonnet-4-5": {
                "id": "claude-sonnet-4-5",
                "name": "Claude Sonnet 4.5",
                "reasoning": True,
                "tool_call": True,
                "release_date": "2025-09-29",
                "limit": {"context": 200000, "output": 64000},
            }
        },
    }
}


def _source(tmp_path: Path, handler) -> ModelsDevSource:
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return ModelsDevSource(
        cache_path=tmp_path / "models-dev.json", client_factory=factory
    )


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json=_DOCUMENT,
        headers={"content-type": "application/json", "etag": '"v1"'},
    )


async def test_a_refresh_stores_metadata_and_hints(tmp_path: Path) -> None:
    source = _source(tmp_path, _ok)
    assert await source.refresh() is RefreshOutcome.UPDATED
    entry = source.metadata("anthropic/claude-sonnet-4-5")
    assert entry is not None
    assert entry.display_name == "Claude Sonnet 4.5"
    assert entry.context_window_tokens == 200000
    assert entry.supports_tools is True
    assert source.env_hints("anthropic") == ("ANTHROPIC_API_KEY",)


async def test_the_request_carries_no_korvid_state(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    await _source(tmp_path, handler).refresh()
    request = seen[0]
    assert str(request.url) == MODELS_DEV_URL
    assert request.url.query == b""
    assert request.method == "GET"
    assert not request.content
    forbidden = {"authorization", "cookie", "x-api-key", "proxy-authorization"}
    assert not forbidden & {name.lower() for name in request.headers}


async def test_a_response_over_the_ceiling_is_refused(tmp_path: Path) -> None:
    oversized = b"[" + b"0," * (MAX_RESPONSE_BYTES // 2) + b"0]"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=oversized, headers={"content-type": "application/json"}
        )

    source = _source(tmp_path, handler)
    assert await source.refresh() is RefreshOutcome.UNAVAILABLE
    assert source.metadata("anthropic/claude-sonnet-4-5") is None


async def test_a_non_json_content_type_is_refused(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hi</html>",
                              headers={"content-type": "text/html"})

    assert await _source(tmp_path, handler).refresh() is RefreshOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"anthropic": "not-an-object"},
        {"anthropic": {"models": "not-an-object"}},
        {"anthropic": {"models": {"m": {"limit": {"context": "lots"}}}}},
        {"anthropic": {"models": {"m": {"tool_call": "yes"}}}},
    ],
)
async def test_a_malformed_document_never_reaches_the_catalog(
    tmp_path: Path, payload: object
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload,
                              headers={"content-type": "application/json"})

    source = _source(tmp_path, handler)
    outcome = await source.refresh()
    assert source.metadata("anthropic/m") is None or outcome is RefreshOutcome.UPDATED
    assert source.env_hints("anthropic") == ()


async def test_a_failed_refresh_keeps_the_previous_cache(tmp_path: Path) -> None:
    source = _source(tmp_path, _ok)
    await source.refresh()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    stale = _source(tmp_path, boom)
    assert await stale.refresh() is RefreshOutcome.UNAVAILABLE
    assert stale.metadata("anthropic/claude-sonnet-4-5") is not None


async def test_a_fresh_cache_makes_no_request(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _ok(request)

    source = _source(tmp_path, handler)
    await source.refresh()
    assert await _source(tmp_path, handler).refresh() is RefreshOutcome.CACHED
    assert calls == 1


async def test_a_stale_cache_revalidates_with_the_stored_etag(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(304, headers={"etag": '"v1"'})

    source = _source(tmp_path, _ok)
    await source.refresh()
    _age_cache(tmp_path / "models-dev.json", CACHE_TTL_SECONDS + 60)
    assert await _source(tmp_path, handler).refresh() is RefreshOutcome.NOT_MODIFIED
    assert seen == ['"v1"']


async def test_the_cache_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    await _source(tmp_path, _ok).refresh()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_default_cache_path_follows_the_platform_convention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_path() == tmp_path / "korvid" / "models-dev.json"


def test_the_bounds_are_actually_bounds() -> None:
    assert REQUEST_TIMEOUT_SECONDS <= 10.0
    assert MAX_RESPONSE_BYTES <= 16 * 1024 * 1024
    assert MODELS_DEV_URL.startswith("https://")
```

Add `_age_cache` as a small helper in the test module that rewrites the cache file's stored `fetched_at` timestamp — **not** `os.utime`, because mtime is not what the TTL reads and a test that asserts on filesystem timestamps is flaky.

- [ ] Step 2 — Run the RED:

```bash
uv run pytest -p no:tach tests/providers/test_models_dev.py -q
```

Expected: `ModuleNotFoundError: No module named 'korvid.providers.models_dev'`. `httpx` is already in `[agent]` today, so unlike Task 6 this suite runs immediately.

- [ ] Step 3 — GREEN. Create `src/korvid/providers/models_dev.py`.

The fetch, with every bound enforced rather than declared:

```python
async def _fetch(self, etag: str | None) -> tuple[bytes, str | None] | None:
    headers = {"accept": "application/json"}
    if etag:
        headers["if-none-match"] = etag
    async with self._client_factory() as client:
        async with client.stream(
            "GET",
            MODELS_DEV_URL,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as response:
            if response.status_code == 304:
                return None
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").split(";")[0].strip()
            if media_type != "application/json":
                raise ValueError(f"unexpected content type: {media_type!r}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeded the size ceiling")
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get("etag")
```

Streaming with a running total is the point: `response.read()` would buffer an unbounded body *before* any check could reject it. `follow_redirects=False` keeps the ceiling and the host both meaningful.

`refresh()` wraps the whole thing:

```python
try:
    result = await self._fetch(cached_etag)
except (httpx.HTTPError, ValueError, OSError, json.JSONDecodeError):
    return RefreshOutcome.UNAVAILABLE
```

No bare `except`, and no `asyncio.CancelledError` swallowing — `CancelledError` is a `BaseException` on 3.11+, so it passes through untouched and a cancelled refresh stays cancelled.

Parsing validates per entry and drops anything that does not fit, rather than rejecting the document:

```python
def _parse(document: object) -> tuple[dict[str, ModelMetadata], dict[str, tuple[str, ...]]]:
    if not isinstance(document, dict):
        raise ValueError("models.dev document must be an object")
    ...
```

For each provider: skip unless the value is a `dict`; `env` contributes hints only when it is a list of non-empty `str`; `models` contributes only when it is a `dict`. For each model: `limit.context`/`limit.output` become ints only via `_positive_int` (which rejects `bool` and strings); `tool_call`/`reasoning` become booleans only when they are exactly `True`/`False`; `name` falls back to the model id; `description` and `release_date` must be `str` or they are dropped. A `reference` is `f"{provider_id}/{model_id}"` — the same construction the LiteLLM layer uses, which is what makes the two tables join.

Writing is atomic and owner-only, via the same pattern `core/config.py` already uses for `credentials.json`: create the parent with `parents=True, exist_ok=True`, write to `path.with_suffix(".tmp")` opened through `os.open(..., os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)`, `os.replace` into place. The stored envelope is `{"fetched_at": <float>, "etag": <str|null>, "document": {...}}` so the TTL never depends on filesystem metadata.

Loading tolerates a corrupt cache (`json.JSONDecodeError`, `OSError`, `ValueError` → treat as absent). A cache written by a future korvid with an unknown envelope shape is also treated as absent, never as a crash.

`metadata()` and `env_hints()` are pure dictionary reads over already-parsed data. **Neither performs I/O**, so the catalog can call them from a synchronous ranking loop.

- [ ] Step 4 — Verify:

```bash
uv run pytest -p no:tach tests/providers/test_models_dev.py -q
uv run ruff check --fix src/korvid/providers/models_dev.py tests/providers/test_models_dev.py
uv run ruff format src/korvid/providers/models_dev.py tests/providers/test_models_dev.py
uv run mypy src/korvid/providers/models_dev.py
uv run tach check
```

- [ ] Step 5 — Wire it into the catalog as the second layer. In `LiteLLMModelCatalog._entry_from`, after building the LiteLLM entry, overlay enrichment **only where LiteLLM has no answer**, and re-label provenance **only when the overlay actually contributed**:

```python
if self._enrichment is None:
    return entry
extra = self._enrichment.metadata(reference)
if extra is None:
    return entry
enriched = replace(
    entry,
    display_name=extra.display_name or entry.display_name,
    context_window_tokens=entry.context_window_tokens or extra.context_window_tokens,
    max_output_tokens=entry.max_output_tokens or extra.max_output_tokens,
    supports_tools=entry.supports_tools if entry.supports_tools is not None else extra.supports_tools,
    supports_reasoning=(
        entry.supports_reasoning
        if entry.supports_reasoning is not None
        else extra.supports_reasoning
    ),
    credential_env_hints=entry.credential_env_hints or extra.credential_env_hints,
)
if enriched == entry:
    # models.dev only restated what LiteLLM already knew. Re-labelling
    # provenance here would credit a source that contributed nothing, and
    # the UI's "where did this come from" line would be false.
    return entry
return replace(enriched, source=ModelEntrySource.MODELS_DEV)
```

LiteLLM wins every conflict, because LiteLLM is what actually makes the call. Add tests to `tests/providers/test_litellm_catalog.py` proving that:

- a fake `ModelMetadataSource` claiming `context_window_tokens=1` for a reference LiteLLM already has a window for must not change the entry's value, and must leave `source is ModelEntrySource.LITELLM`;
- a fake supplying a fact LiteLLM lacks (a `display_name` for an entry with none) **does** flip `source` to `MODELS_DEV`;
- a fake claiming a description for a reference LiteLLM does not know must not create a *routable* entry — it may appear in search, marked `MODELS_DEV`, and nothing more.

The first of those is the one that is easy to get wrong, so write it as a named
test rather than leaving it to a loop:

```python
def test_provenance_stays_litellm_when_the_overlay_adds_nothing() -> None:
    """An overlay that restates known facts must not claim credit.

    `replace()` returns a new object even when every field is identical, so
    the naive implementation flips `source` to `MODELS_DEV` for entries
    models.dev did not actually improve, and the UI's "where did this come
    from" line becomes false. Compare the dataclasses, not the identities.
    """
    base = ModelCatalogEntry(
        reference="openai/gpt-4o",
        display_name="GPT-4o",
        context_window_tokens=128_000,
        max_output_tokens=16_384,
        supports_tools=True,
        supports_reasoning=False,
        credential_env_hints=("OPENAI_API_KEY",),
        source=ModelEntrySource.LITELLM,
    )
    echoing = _FakeMetadataSource(
        {
            "openai/gpt-4o": ModelMetadata(
                display_name="GPT-4o",
                context_window_tokens=128_000,
                max_output_tokens=16_384,
                supports_tools=True,
                supports_reasoning=False,
                credential_env_hints=("OPENAI_API_KEY",),
            )
        }
    )
    catalog = LiteLLMModelCatalog(enrichment=echoing)

    result = catalog._overlay(base)

    assert result == base
    assert result.source is ModelEntrySource.LITELLM


def test_provenance_becomes_models_dev_only_when_a_fact_was_added() -> None:
    """The mirror image: a genuine contribution must be credited."""
    bare = ModelCatalogEntry(
        reference="openai/gpt-4o",
        display_name=None,
        context_window_tokens=128_000,
        max_output_tokens=None,
        supports_tools=True,
        supports_reasoning=None,
        credential_env_hints=("OPENAI_API_KEY",),
        source=ModelEntrySource.LITELLM,
    )
    contributing = _FakeMetadataSource(
        {"openai/gpt-4o": ModelMetadata(display_name="GPT-4o")}
    )
    catalog = LiteLLMModelCatalog(enrichment=contributing)

    result = catalog._overlay(bare)

    assert result.display_name == "GPT-4o"
    assert result.source is ModelEntrySource.MODELS_DEV
```

Also add the test that pins the boundary itself:

```python
def test_enrichment_cannot_change_where_a_request_goes() -> None:
    """Metadata may describe a model. It may never route one."""
    source = Path("src/korvid/providers/models_dev.py").read_text(encoding="utf-8")
    for forbidden in ("api_base", "base_url", "acompletion", "api_key", "get_llm_provider"):
        assert forbidden not in source
```

- [ ] Step 6 — Commit:

```bash
git add -A
git commit -m "feat: enrich the model catalog from models.dev, strictly bounded

models.dev publishes descriptions, release dates and per-provider
credential-variable names as one MIT-licensed JSON document. It has no
Python SDK, so this is a single conditional GET behind an interface.

The bounds are enforced, not documented: 10s budget, 12 MiB streaming
ceiling checked while reading rather than after, application/json
required, redirects refused, per-entry schema validation that drops bad
entries instead of failing the document.

The request carries no credential, prompt or korvid state, and enrichment
never wins a conflict with LiteLLM and never touches routing - a test
greps the module for api_base, base_url, api_key and acompletion.

Never fetched at startup. Cached 0600 under the platform cache dir with
an ETag; a failure falls back to the cache and then to LiteLLM's bundled
tables, so korvid works fully air-gapped.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 5, 5B, 6. **Blocks:** Tasks 8, 10.

---

### Task 8: Special flows, live discovery, manual entry, and composition

The last three catalog layers plus the wiring that makes the catalog exist at all.

The special-flow registry is the one concession to reality, and it is deliberately shaped so it cannot grow back into a provider list: a flow is **data**, it must **claim a prefix or a named option**, and it may only supply what LiteLLM structurally cannot. Two exist, and Task 17 implements them. The registry is fully functional when empty — a test asserts it.

**Files**

- `src/korvid/providers/special_flows.py` (new)
- `src/korvid/providers/endpoint_discovery.py` (new)
- `src/korvid/providers/litellm_catalog.py` (extend)
- `src/korvid/__main__.py` (extend)
- `tests/providers/test_special_flows.py` (new)
- `tests/providers/test_endpoint_discovery.py` (new)
- `tests/test_main_wiring.py` (extend)

**Interfaces**

```python
class SpecialFlowError(ValueError):
    """A declared flow is unusable. Always disables that flow only."""


def normalize_prefix(prefix: str) -> str:
    """The canonical spelling of a reference prefix.

    Lowercased, with `_` folded to `-`. LiteLLM's own tables publish
    `github_copilot/...` while korvid's flow claims `github-copilot/`; if
    those two do not fold together, the underscore spelling is unclaimed,
    falls through to `get_llm_provider`, and starts the interactive device
    login the registry exists to prevent.
    """
    return prefix.strip().lower().replace("_", "-")


class SpecialFlowRegistry:
    """Loads `SpecialFlow` declarations from the `korvid.provider` entry
    point group — the same group `ProviderPluginRegistry` already uses,
    so this adds no new extension mechanism.

    Loading is **selected-only and lazy**, mirroring
    `providers/plugin_registry.py`'s existing "Load only the selected
    entry point" rule: construction reads entry-point *names* from
    installed distribution metadata and calls `EntryPoint.load()` for
    nothing. A name is loaded the first time a reference resolving to it
    is claimed, and only that one. Loading every declared entry point at
    construction would execute arbitrary third-party module-level code on
    every korvid startup and let one broken plugin break TUI wiring —
    and building the catalog constructs this registry.

    Declared prefixes are stored normalized, so two flows differing only
    in separator collide and the second is rejected rather than silently
    shadowing the first.

    A declaration is rejected, with the failure recorded and the rest of
    the registry left working, when it:

    - claims a prefix that is empty, contains a slash, or is not a valid
      reference prefix;
    - claims a normalized prefix a previously loaded flow already claimed;
    - claims a name in `RETIRED_PROVIDER_ALIASES` or `RESERVED_*`;
    - is not a `SpecialFlow` instance;
    - raises on load.
    """

    def __init__(self, flows: Sequence[SpecialFlow] = ()) -> None: ...

    @classmethod
    def from_entry_points(cls) -> SpecialFlowRegistry:
        """Build from entry-point **names only**; load nothing yet."""

    def claim(self, reference: str) -> SpecialFlow | None:
        """The flow owning this reference's prefix, or None.

        Normalizes the prefix first, then loads the one matching entry
        point if it has not been loaded yet.
        """

    def claim_by_option(self, reference: str, options: Mapping[str, object]) -> SpecialFlow | None:
        """A flow that shares a reference but activates on a named
        boolean option (native Ollama `thinking` is the only case)."""

    @property
    def claimed_prefixes(self) -> frozenset[str]:
        """Every normalized prefix a declared flow claims, plus the
        retired aliases — available *without* loading anything, so the
        factory can refuse a claimed reference before it routes."""

    @property
    def errors(self) -> tuple[str, ...]:
        """Human-readable rejection reasons, for the setup UI's banner."""
```

```python
class EndpointDiscovery:
    """Best-effort model listing from an operator-supplied endpoint.

    Tries `GET {base}/v1/models` (OpenAI-compatible) then `GET {base}/api/tags`
    (Ollama-native), takes the first that parses, and returns `()` on any
    failure. Bounded by a 5s timeout and a 2 MiB ceiling, `application/json`
    only, redirects refused, at most 500 entries kept.
    """

    async def list_models(
        self, *, base_url: str, api_key: str | None, prefix: str
    ) -> tuple[ModelEntry, ...]: ...
```

**Steps**

- [ ] Step 1 — RED, part 1. Create `tests/providers/test_special_flows.py`:

```python
from __future__ import annotations

from collections.abc import Sequence

import pytest

from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    EndpointRequirement,
    SetupField,
    SetupFieldKind,
    SpecialFlow,
)
from korvid.providers.special_flows import SpecialFlowRegistry


def _flow(prefix: str, **kwargs: object) -> SpecialFlow:
    return SpecialFlow(
        prefix=prefix,
        display_name=prefix,
        auth_methods=(AuthMethodDescriptor(id="none", display_name="None"),),
        **kwargs,  # type: ignore[arg-type]  # test builder, exercised by mypy on the real call sites
    )


def test_an_empty_registry_is_fully_functional() -> None:
    """No flow declared is the normal case, not a degraded one."""
    registry = SpecialFlowRegistry()
    assert registry.claim("openai/gpt-4o") is None
    assert registry.errors == ()


def test_a_flow_claims_only_its_own_prefix() -> None:
    registry = SpecialFlowRegistry([_flow("github-copilot")])
    assert registry.claim("github-copilot/gpt-4o") is not None
    assert registry.claim("github-copilot-extra/gpt-4o") is None
    assert registry.claim("openai/gpt-4o") is None
    assert registry.claim("gpt-4o") is None


def test_the_first_claim_of_a_prefix_wins_and_the_second_is_reported() -> None:
    registry = SpecialFlowRegistry([_flow("dup"), _flow("dup")])
    assert registry.claim("dup/x") is registry.claim("dup/x")
    assert any("dup" in message for message in registry.errors)


@pytest.mark.parametrize("prefix", ["", "  ", "a/b", "UPPER", "with space", "sla\\sh"])
def test_a_malformed_prefix_is_refused(prefix: str) -> None:
    registry = SpecialFlowRegistry([_flow(prefix)])
    assert registry.claim(f"{prefix}/x") is None
    assert registry.errors


@pytest.mark.parametrize("prefix", ["openai-compat", "vllm", "github", "claude"])
def test_a_retired_builtin_alias_cannot_be_claimed(prefix: str) -> None:
    """Deleting the built-ins must not free the names for a third party
    to squat on: an operator still reads them as korvid's own."""
    registry = SpecialFlowRegistry([_flow(prefix)])
    assert registry.claim(f"{prefix}/x") is None
    assert registry.errors


def test_a_flow_may_claim_a_named_option_instead_of_a_prefix() -> None:
    flow = _flow(
        "ollama",
        claims_option="native_thinking",
        option_fields=(
            SetupField(key="native_thinking", label="Native thinking",
                       kind=SetupFieldKind.BOOLEAN),
        ),
    )
    registry = SpecialFlowRegistry([flow])
    assert registry.claim_by_option("ollama/qwen3:8b", {"native_thinking": True}) is flow
    assert registry.claim_by_option("ollama/qwen3:8b", {"native_thinking": False}) is None
    assert registry.claim_by_option("ollama/qwen3:8b", {}) is None
    assert registry.claim_by_option("openai/gpt-4o", {"native_thinking": True}) is None


def test_a_broken_declaration_disables_only_itself() -> None:
    class Exploding:
        @property
        def prefix(self) -> str:
            raise RuntimeError("boom")

    registry = SpecialFlowRegistry([Exploding(), _flow("good")])  # type: ignore[list-item]  # deliberately invalid
    assert registry.claim("good/x") is not None
    assert registry.errors


def test_the_registry_is_not_a_provider_list() -> None:
    """No enumeration API: nothing may iterate flows to render a vendor
    picker, which is the shape this design removes."""
    public = {name for name in vars(SpecialFlowRegistry) if not name.startswith("_")}
    assert not {"all", "flows", "descriptors", "names", "providers"} & public


@pytest.mark.parametrize(
    "reference",
    ["github-copilot/gpt-4o", "github_copilot/gpt-4o", "GitHub-Copilot/gpt-4o"],
)
def test_a_claim_folds_underscores_hyphens_and_case(reference: str) -> None:
    """LiteLLM's own tables publish `github_copilot/...`. If that spelling
    does not fold onto korvid's `github-copilot/` claim, it is unclaimed,
    reaches `get_llm_provider`, and starts an interactive device login."""
    flow = _flow("github-copilot")
    registry = SpecialFlowRegistry([flow])
    assert registry.claim(reference) is flow


def test_two_flows_differing_only_by_separator_collide() -> None:
    registry = SpecialFlowRegistry([_flow("github-copilot"), _flow("github_copilot")])
    assert registry.claim("github_copilot/x") is registry.claim("github-copilot/x")
    assert any("github-copilot" in message for message in registry.errors)


def test_claimed_prefixes_are_known_without_loading_anything() -> None:
    """The factory has to refuse a claimed reference *before* it routes,
    and it must be able to do that without importing plugin code."""
    registry = SpecialFlowRegistry([_flow("github-copilot")])
    assert "github-copilot" in registry.claimed_prefixes
    assert "openai-compat" in registry.claimed_prefixes


def test_only_the_resolved_entry_point_is_ever_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading every declared entry point at construction would execute
    arbitrary third-party module-level code on every korvid startup, and
    one broken plugin would break TUI wiring. `plugin_registry.py`
    already loads only the selected entry point; this must not be weaker.
    """
    loaded: list[str] = []

    class _FakeEntryPoint:
        def __init__(self, name: str, flow: SpecialFlow | None) -> None:
            self.name = name
            self.group = "korvid.provider"
            self._flow = flow

        def load(self) -> SpecialFlow:
            loaded.append(self.name)
            if self._flow is None:
                raise AssertionError(f"{self.name} must not be loaded")
            return self._flow

    wanted = _flow("wanted")
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (
            _FakeEntryPoint("wanted", wanted),
            _FakeEntryPoint("landmine", None),
        ),
    )

    registry = SpecialFlowRegistry.from_entry_points()
    assert loaded == [], "construction must load nothing"

    assert registry.claim("wanted/x") is wanted
    assert loaded == ["wanted"]
    assert registry.claim("unrelated/x") is None
    assert loaded == ["wanted"]
```

- [ ] Step 2 — RED, part 2. Create `tests/providers/test_endpoint_discovery.py` covering: an OpenAI-shaped `{"data": [{"id": "m"}]}` becomes one `ModelEntry` with `source is ModelEntrySource.ENDPOINT` and `reference == f"{prefix}/m"`; an Ollama-shaped `{"models": [{"name": "qwen3:8b"}]}` becomes `"{prefix}/qwen3:8b"` (**the colon survives — this is the second place slash separators matter**); a 404 on `/v1/models` falls through to `/api/tags`; both failing returns `()`; a connection error returns `()` and does not raise; an oversized body returns `()`; more than 500 entries is truncated to 500; the `Authorization` header is present when a key is supplied and **absent when it is not**.

- [ ] Step 3 — Run both REDs:

```bash
uv run pytest -p no:tach tests/providers/test_special_flows.py tests/providers/test_endpoint_discovery.py -q
```

Expected: two `ModuleNotFoundError`s.

- [ ] Step 4 — GREEN, part 1: `src/korvid/providers/special_flows.py`.

`_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")`, applied to the *declared* spelling. Validation order is: instance check, prefix pattern, reserved/retired check against `RETIRED_PROVIDER_ALIASES | RESERVED_PROVIDER_NAMES` (imported from `litellm_settings.py` and `plugin_registry.py` respectively) **on the normalized form**, then duplicate check on the normalized form. Each rejection appends a message to `self._errors` and continues — the loop never raises. Both the reserved set and the claim table are keyed by `normalize_prefix(...)`, so `github_copilot` and `github-copilot` cannot be separately claimed and a retired alias cannot be squatted under a different separator.

`from_entry_points()` reuses `_ENTRY_POINT_GROUP = "korvid.provider"` and mirrors `plugin_registry.py`'s **selected-only** pattern exactly:

1. `_iter_entry_points()` enumerates the group from installed distribution metadata and returns the `EntryPoint` objects **without calling `load()`**. It is a module-level function so tests can substitute it.
2. Construction builds `{normalize_prefix(ep.name): ep}` from those names alone. Nothing third-party has executed yet. This matters because `_build_model_catalog()` constructs the registry at composition time: eager loading here would run every installed plugin's module-level code on every korvid startup, and one raising plugin would take out TUI wiring.
3. `claim(reference)` normalizes the reference prefix, looks it up in that map, and calls `entry.load()` for **that one entry point** if it has not been loaded yet, memoizing the result (including a failure, so a broken plugin is not retried on every keystroke).
4. The `load()` call is wrapped in `try: ... except Exception as exc:` — third-party code can raise anything, and one bad plugin must not take down the agent; `# noqa: BLE001` with that reason. A loaded object contributes flows if it is a `SpecialFlow`, or if it exposes a `korvid_special_flows()` callable returning an iterable of them. Anything else is ignored silently — existing `ProviderPlugin` entry points in the same group are not errors.
5. `claimed_prefixes` is answered from the name map plus `RETIRED_PROVIDER_ALIASES`, so the factory's pre-routing refusal (Task 15) needs no `load()` at all.

An entry point declared under a name korvid never resolves is therefore never imported — which is the invariant the "unselected plugins are not imported" security bullet states.

`claim(reference)` splits on the first slash via `split_reference`; a reference with no slash never claims. `claim_by_option` requires `normalize_prefix(flow.prefix)` to equal the normalized reference prefix **and** `options.get(flow.claims_option) is True` — strictly `is True`, so a truthy string cannot silently switch transports.

- [ ] Step 5 — GREEN, part 2: `src/korvid/providers/endpoint_discovery.py`. Same bounded-fetch shape as Task 7 (stream, running total, content-type check, `follow_redirects=False`), two candidate paths tried in order, `()` on every failure path. Join the base URL with `str(httpx.URL(base_url).join(path))` rather than string concatenation so a trailing slash cannot produce `//v1/models`.

- [ ] Step 6 — GREEN, part 3: wire layers 3 and 4 into `LiteLLMModelCatalog`:

- `discover(profile)` — returns `()` when `self._discovery is None`, when `profile.config_error is not None`, or when `profile.base_url` is falsy. Otherwise resolves the key through the profile's auth method **without** falling back to ambient environment, and calls `list_models(prefix=split_reference(profile.model)[0] or "openai")`.
- `auth_methods(reference, *, endpoint=None)` / `option_fields` / `endpoint_requirement` now consult `self._flows.claim(reference)` first and return the flow's declarations ahead of the generic ones. Two rules survive the flow consult unchanged, and a test pins each:
  - The `endpoint` keyword still governs `none`. A flow that declares its own auth methods replaces the generic list, but it cannot *add* `none` for a profile with no endpoint — the factory would refuse it at build time either way, so the catalog filters `none` out of a flow's declarations when `endpoint` is falsy rather than trusting the plugin. A third-party flow is not a trusted source for a security rule.
  - `endpoint_requirement` returns the flow's declaration when one claims the reference (this is where `REQUIRED` and `UNSUPPORTED` come from — Copilot declares `UNSUPPORTED`, native-thinking Ollama declares `REQUIRED`) and `OPTIONAL` for everything else. Assert both against a **registered flow**, not against a provider table:

```python
def test_a_flow_supplies_the_only_non_optional_endpoint_requirements() -> None:
    registry = SpecialFlowRegistry(
        [_flow("github-copilot", endpoint=EndpointRequirement.UNSUPPORTED)]
    )
    catalog = LiteLLMModelCatalog(flows=registry)
    assert catalog.endpoint_requirement("github-copilot/gpt-4o") is (
        EndpointRequirement.UNSUPPORTED
    )
    assert catalog.endpoint_requirement("openai/gpt-4o") is EndpointRequirement.OPTIONAL
    assert catalog.endpoint_requirement("azure/gpt-4o") is EndpointRequirement.OPTIONAL


def test_a_flow_cannot_offer_keyless_auth_without_an_endpoint() -> None:
    """The catalog filters a plugin's declarations through korvid's own
    rule. A flow is third-party code; it does not get to widen a refusal
    the factory will enforce anyway."""
    registry = SpecialFlowRegistry(
        [_flow("company-flow", auth_methods=(AuthMethodDescriptor(id="none", display_name="None"),))]
    )
    catalog = LiteLLMModelCatalog(flows=registry)
    assert "none" not in {m.id for m in catalog.auth_methods("company-flow/x")}
    assert "none" in {
        m.id for m in catalog.auth_methods("company-flow/x", endpoint="http://host:8080")
    }
```
- `search` merges manual and endpoint entries the caller has supplied, deduplicating by `reference` with catalog entries winning.

- [ ] Step 7 — GREEN, part 4: composition. In `src/korvid/__main__.py`, add a lazy builder beside the existing provider wiring:

```python
def _build_model_catalog() -> ModelCatalog | None:
    """Build the catalog, or None when the agent extra is absent.

    A missing extra degrades to None — the TUI runs without an agent.
    A *broken* extra is different: it is reported, not swallowed.
    """
    try:
        from korvid.providers.endpoint_discovery import EndpointDiscovery
        from korvid.providers.litellm_catalog import LiteLLMModelCatalog
        from korvid.providers.models_dev import ModelsDevSource
        from korvid.providers.special_flows import SpecialFlowRegistry
    except ImportError:
        return None
    return LiteLLMModelCatalog(
        flows=SpecialFlowRegistry.from_entry_points(),
        enrichment=ModelsDevSource(),
        discovery=EndpointDiscovery(),
    )
```

All four imports are lazy and inside the `try`, `EndpointDiscovery` included — it is a `providers/` module like the others, so importing it at module scope would drag the provider package into a base install that has no `[agent]` extra, and `tests/test_optional_extras.py` would fail.

`ModelsDevSource()` construction performs **no I/O** — it only records a path. The first read loads the cache if one exists. Nothing fetches until the operator asks.

Add two tests in `tests/test_main_wiring.py`:

- building the catalog issues no HTTP request, by injecting a `client_factory` that fails the test if called;
- building the catalog opens **no socket at all**. This is the wiring-level counterpart to `tests/providers/test_litellm_offline_import.py`: `_build_model_catalog()` imports `litellm_catalog`, which imports `litellm_runtime`, which imports the wrapper — so a regression in the wrapper's env-var ordering shows up here as a real startup stall. Reuse the same subprocess probe shape (patch `socket.socket.connect`/`connect_ex`, call `_build_model_catalog()`, assert the recorded list is empty), and skip it when `litellm` is not importable.

- [ ] Step 8 — Verify:

```bash
uv run pytest -p no:tach tests/providers/ tests/test_main_wiring.py tests/test_optional_extras.py -q
uv run ruff check --fix src/korvid/providers/ src/korvid/__main__.py tests/
uv run ruff format src/korvid/providers/ src/korvid/__main__.py tests/
uv run mypy src/korvid/
uv run tach check
```

- [ ] Step 9 — Commit:

```bash
git add -A
git commit -m "feat: add special flows, endpoint discovery and manual entry

Three catalog layers on top of LiteLLM's tables, plus the composition
root wiring.

The special-flow registry is the one concession to flows a standard
transport cannot own. It is shaped so it cannot become a provider list
again: a flow is data, it must claim exactly one reference prefix or one
named boolean option, it loads through the existing korvid.provider entry
point group, and there is deliberately no API to enumerate flows - a test
asserts that too. An empty registry is fully functional, and a broken
declaration disables only itself. Retired built-in aliases stay
unclaimable so a third party cannot squat a name operators read as ours.

Endpoint discovery lists models from an operator's own endpoint, bounded
the same way as the models.dev fetch and silent on every failure, because
'type the name yourself' is a better outcome than an error dialog.

Building the catalog performs no I/O and issues no request; a wiring test
proves it.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 5, 6, 7. **Blocks:** Tasks 9, 10, 11, 17.

---

## Commit group 3 — Profile-first, search-first setup UI (Tasks 9–11)

Three screens replace the vendor-picker wizard. None of them contains a vendor name, a CSP list, or a branch on a provider id — a test greps for exactly that.

### Task 9: The profile manager

The entry point becomes "which profile?", not "which vendor?". `:ai` with profiles configured opens this screen.

**Files**

- `src/korvid/ui/widgets/profile_manager_screen.py` (new)
- `src/korvid/ui/messages.py` (extend)
- `tests/ui/test_profile_manager_screen.py` (new)

**Interfaces**

```python
@dataclass(frozen=True, slots=True)
class ProfileManagerResult:
    """What the manager hands back. Exactly one field is set.

    `activated` names a profile to switch to. `edited` carries a whole
    replacement profile set to persist. Splitting them keeps "switch"
    from silently rewriting a profile the operator did not touch.
    """

    activated: str | None = None
    edited: ModelConnectionsConfig | None = None


class ProfileManagerScreen(ModalScreen["ProfileManagerResult | None"]):
    """List, activate, add, edit and delete model profiles.

    Args:
        profiles: The current profile set, rendered in insertion order.
        catalog: Used only to label a profile's model; `None` renders the
            raw reference, which is still complete information.
        open_editor: Pushes the model-search/edit flow for a profile,
            returning the edited profile or None. Injected so this screen
            is testable without the whole wizard.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("enter", "activate", "Activate"),
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
    ]
```

**Steps**

- [ ] Step 1 — RED. Create `tests/ui/test_profile_manager_screen.py` using the existing pilot conventions and `tests/ui/waits.py::until`:

```python
async def test_enter_activates_without_rewriting_the_profile_set() -> None:
    """Activation is write-only on `active`. A switch must never
    re-serialize profiles the operator did not edit - that is how an
    unparsed profile gets silently dropped."""
    ...
    assert result == ProfileManagerResult(activated="staging")
    assert result.edited is None


async def test_a_profile_that_failed_to_parse_is_listed_and_deletable() -> None:
    """A rejected profile must stay visible and removable. Hiding it
    leaves an operator with a config file they cannot fix from the UI."""
    profiles = ModelConnectionsConfig(
        profiles={"good": ModelConnectionConfig(model="openai/gpt-4o")},
        unparsed={"broken": {"model": ["not", "a", "string"]}},
    )
    ...
    listing = screen.query_one("#profile-list", OptionList)
    assert any("broken" in str(option.prompt) for option in listing._options)
    # deleting it removes both halves
    ...
    assert "broken" not in result.edited.unparsed
    assert "broken" not in result.edited.profiles


async def test_a_profile_that_failed_to_parse_cannot_be_activated() -> None:
    """`config_error` means korvid cannot build a provider from it.
    Offering to activate it would guarantee a failed rebuild."""
    ...
    assert result is None
    assert "cannot be activated" in screen.query_one("#profile-status", Static).renderable


async def test_deleting_the_active_profile_clears_the_pointer() -> None:
    ...
    assert result.edited.active is None


async def test_deleting_the_last_profile_is_allowed() -> None:
    """An empty profile set is a valid state - it is what a fresh
    install has. Refusing would trap an operator with one bad profile."""
    ...
    assert result.edited.profiles == {}


async def test_profiles_render_in_insertion_order_not_sorted() -> None:
    ...
    assert [str(o.prompt).split()[0] for o in listing._options][:3] == ["zeta", "alpha", "mid"]


async def test_no_vendor_appears_anywhere_in_the_screen_source() -> None:
    source = Path("src/korvid/ui/widgets/profile_manager_screen.py").read_text(encoding="utf-8")
    for vendor in ("openai", "anthropic", "azure", "bedrock", "gemini", "ollama", "copilot"):
        assert vendor not in source.lower()
```

- [ ] Step 2 — Run the RED:

```bash
uv run pytest -p no:tach tests/ui/test_profile_manager_screen.py -q
```

Expected: `ModuleNotFoundError`.

- [ ] Step 3 — GREEN. Create the screen. Composition is an `OptionList#profile-list` inside a `VerticalScroll` (`height: auto; max-height: 80%`), a `Static#profile-status`, and a footer hint. CSS uses class-name selectors per the house rule.

Row rendering: `f"{name}{marker} — {label}"` where `marker` is `" (active)"` for the active profile and `" (invalid)"` for a `config_error` or unparsed one, and `label` is `catalog.entry(profile.model).display_name` when the catalog resolves it, else `profile.model`, else the recorded parse error. Unparsed profiles render after parsed ones, still in their own insertion order.

`action_activate` refuses when the selected profile is unparsed or carries a `config_error`, writing the reason into `#profile-status` and **not** dismissing. Otherwise it dismisses with `ProfileManagerResult(activated=name)`.

`action_delete` builds a new `ModelConnectionsConfig` with the name removed from **both** `profiles` and `unparsed`, clears `active` when it pointed at the deleted name, and dismisses with `ProfileManagerResult(edited=...)`.

`action_add` / `action_edit` await `self._open_editor(existing)` and, on a non-`None` result, merge it into the set preserving insertion order (an edit replaces in place; an add appends) and dismiss with `edited`.

- [ ] Step 4 — Verify:

```bash
uv run pytest -p no:tach tests/ui/test_profile_manager_screen.py -q
uv run ruff check --fix src/korvid/ui/ tests/ui/test_profile_manager_screen.py
uv run ruff format src/korvid/ui/ tests/ui/test_profile_manager_screen.py
uv run mypy src/korvid/ui/widgets/profile_manager_screen.py
uv run tach check
```

- [ ] Step 5 — Commit:

```bash
git add -A
git commit -m "feat: make agent setup start from profiles, not vendors

The first question is now 'which profile?'. Activation is write-only on
the active pointer and never re-serializes the profile set, so switching
cannot silently drop a profile korvid failed to parse.

A profile that failed to parse stays listed and deletable but cannot be
activated: hiding it would leave an operator unable to fix their config
from the UI, and activating it would guarantee a failed rebuild. Deleting
removes both halves and clears the active pointer when it pointed there.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 5, 8. **Blocks:** Task 11.

---

### Task 10: Model search as the first question

Not a vendor list. A search box over every chat model LiteLLM ships, ranked, with manual entry always available.

**Files**

- `src/korvid/ui/widgets/model_search_screen.py` (new)
- `tests/ui/test_model_search_screen.py` (new)

**Interfaces**

```python
class ModelSearchScreen(ModalScreen["str | None"]):
    """Free-text model search returning a `provider/model` reference.

    Args:
        catalog: Searched on every keystroke. Search is synchronous and
            in-memory, so it is safe on the input handler.
        initial_query: Prefilled when editing an existing profile, so
            editing starts from the current model rather than blank.
        discovered: Entries a prior discovery produced, merged ahead of
            catalog results.
    """
```

**Steps**

- [ ] Step 1 — RED. Create `tests/ui/test_model_search_screen.py`:

```python
async def test_typing_filters_and_selecting_returns_a_reference() -> None:
    ...
    assert result == "anthropic/claude-sonnet-4-5"


async def test_the_screen_opens_on_search_not_on_a_provider_list() -> None:
    """The first focused widget is the query box. There is no provider
    OptionList to focus, because there is no provider step."""
    ...
    assert isinstance(screen.focused, Input)
    assert screen.query("#provider-list") == []


async def test_results_group_by_provider_for_reading_but_do_not_filter_by_it() -> None:
    """A provider name is a label and a search term. It is never a gate:
    a query matching models across providers shows all of them."""
    ...
    providers = {entry.provider_id for entry in shown}
    assert len(providers) > 1


async def test_an_unmatched_query_still_offers_the_typed_reference() -> None:
    """Manual entry is a first-class path, not an error state - a private
    or brand-new model is exactly what the catalog will not know."""
    ...
    assert "use \"company/internal-v2\"" in rendered.lower()
    ...
    assert result == "company/internal-v2"


async def test_a_manual_reference_without_a_slash_is_refused_with_the_reason() -> None:
    ...
    assert "provider/model" in screen.query_one("#search-status", Static).renderable


async def test_search_is_bounded_so_a_broad_query_cannot_stall_the_ui() -> None:
    ...
    assert len(shown) <= 50


async def test_editing_prefills_the_current_model() -> None:
    ...
    assert screen.query_one("#model-query", Input).value == "ollama/qwen3:8b"


async def test_a_colon_in_a_model_tag_survives_search_and_selection() -> None:
    """`ollama/qwen3:8b` is the shape colon separators could not express;
    it must round-trip through the UI unchanged."""
    ...
    assert result == "ollama/qwen3:8b"


async def test_the_screen_never_calls_the_network() -> None:
    """Search reads in-memory tables. A catalog whose `discover` fails
    the test if awaited proves the screen does not reach for it."""
```

- [ ] Step 2 — Run the RED, expect `ModuleNotFoundError`.

- [ ] Step 3 — GREEN. `Input#model-query`, `OptionList#model-results`, `Static#search-status`. `on_input_changed` calls `self._catalog.search(value, limit=50)`, merges `self._discovered` ahead of it deduplicating on `reference`, and repopulates. When the query contains a slash and matches nothing, append a final synthetic option labelled `Use "<query>" (not in catalog)` carrying the raw reference.

Selecting dismisses with the reference. Submitting the input directly dismisses with the raw query after validating it: non-empty, contains a slash, no whitespace, and the prefix matches `^[a-z0-9][a-z0-9_-]*$`. A failure writes the reason into `#search-status` and does not dismiss — reuse `split_reference` so the UI and the catalog agree on what a reference is.

Result rows render `f"{entry.reference}  {entry.display_name}"` plus a compact capability suffix built only from **known** facts: `" · 200k ctx"`, `" · tools"`, `" · reasoning"`. An unknown capability renders nothing at all rather than "no" — the distinction is the whole point of the tri-state.

- [ ] Step 4 — Verify (same five commands as Task 9, with this test path).

- [ ] Step 5 — Commit:

```bash
git add -A
git commit -m "feat: make model search the first question in agent setup

A search box over the whole catalog replaces the vendor list. A
provider name is a label and a search term, never a gate - a query
matching models across providers shows all of them.

Manual entry is a first-class result rather than an error state, because
a private or brand-new model is exactly what the catalog will not know.
Capability badges render only facts a source asserted; an unknown
capability renders nothing rather than 'no'.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 5, 8. **Blocks:** Task 11.

---

### Task 11: Descriptor-driven detail stages, apply, persist, controller

The remaining stages are generated from `AuthMethodDescriptor` and `SetupField` — no stage in this screen knows a vendor exists.

**Files**

- `src/korvid/ui/widgets/agent_setup_screen.py` (rewrite)
- `src/korvid/ui/agent_ui_controller.py` (extend)
- `src/korvid/ui/messages.py` (extend)
- `tests/ui/test_agent_setup_screen.py` (**migrate**, not delete)
- `tests/ui/test_agent_ui_controller_profiles.py` (new)

**Steps**

- [ ] Step 1 — RED, part 1. Migrate `tests/ui/test_agent_setup_screen.py`. This file currently pins the vendor wizard. Do **not** delete it: rewrite each test to the profile-first flow so the behaviour it protected (checklist rendering, cancel semantics, retry binding, apply-before-persist, the `AgentSettings | None` dismiss contract) keeps a test. Delete a test only when the behaviour it pins is itself removed, and say which in the commit body.

- [ ] Step 2 — RED, part 2. Add the new stage tests:

```python
async def test_stages_are_generated_from_descriptors_not_hardcoded() -> None:
    """A fake catalog returning one exotic auth method with one field
    must produce exactly that prompt. Nothing in the screen may special
    case an id it has not been given."""
    catalog = _FakeCatalog(
        auth=(AuthMethodDescriptor(
            id="mtls-cert",
            display_name="Client certificate",
            fields=(SetupField(key="cert_path", label="Certificate path",
                               kind=SetupFieldKind.TEXT, required=True),),
        ),)
    )
    ...
    assert "Client certificate" in rendered
    assert "Certificate path" in rendered


async def test_a_required_field_blocks_progress_and_says_why() -> None: ...


async def test_a_secret_ref_field_stores_the_name_and_never_the_value() -> None:
    """The wizard writes `api_key_env: OPENAI_API_KEY`. It must never
    write the variable's value into the profile."""
    monkeypatch.setenv("SOME_KEY", "sk-secret-value")
    ...
    assert result.auth.key == "SOME_KEY"
    assert "sk-secret-value" not in json.dumps(_as_dict(result))


async def test_option_fields_are_seeded_from_the_edited_profile() -> None:
    """Editing must start from the profile's current options, not from
    descriptor defaults - otherwise editing silently resets them."""
    profile = ModelConnectionConfig(
        model="x/y", options={"num_ctx": 8192, "native_thinking": True}
    )
    ...
    assert screen.query_one("#field-num_ctx", Input).value == "8192"
    assert screen.query_one("#field-native_thinking", Checkbox).value is True


async def test_an_integer_field_refuses_a_non_integer_without_losing_the_stage() -> None: ...


async def test_an_endpoint_stage_appears_only_when_the_descriptor_requires_it() -> None:
    assert_stage_present(EndpointRequirement.REQUIRED)
    assert_stage_absent(EndpointRequirement.UNSUPPORTED)


async def test_a_required_endpoint_blocks_completion_when_empty() -> None: ...


async def test_discovery_failure_falls_through_to_manual_entry() -> None:
    """`discover` returning () is the normal offline case, not an error."""
    ...
    assert screen.query_one("#search-status", Static).renderable != ""
    assert screen.query("#error-dialog") == []


async def test_a_device_login_prompt_renders_the_uri_and_code() -> None: ...


async def test_the_setup_screen_source_names_no_vendor() -> None:
    source = Path("src/korvid/ui/widgets/agent_setup_screen.py").read_text(encoding="utf-8")
    for vendor in ("openai", "anthropic", "azure", "bedrock", "gemini",
                   "ollama", "copilot", "vllm", "mistral", "cohere"):
        assert vendor not in source.lower()


async def test_applying_settings_precedes_persisting_them() -> None:
    """Preserved from the current wizard: a refused swap must leave the
    wizard open and the config unwritten."""
    ...
    assert saved == []
    assert screen.is_running
```

- [ ] Step 3 — RED, part 3. Create `tests/ui/test_agent_ui_controller_profiles.py`:

```python
def test_the_controller_opens_the_profile_manager_when_profiles_exist() -> None: ...


def test_the_controller_opens_setup_directly_when_no_profile_exists() -> None:
    """A first run must not show an empty list with nothing to pick."""


def test_activating_a_profile_rebuilds_and_persists_only_the_pointer() -> None:
    ...
    assert written["agent"]["profiles"]["active"] == "staging"
    assert written["agent"]["profiles"]["profiles"].keys() == {"default", "staging"}


def test_a_refused_rebuild_keeps_the_previous_session_and_pointer() -> None:
    """Preserved: apply-before-persist. A profile that cannot build a
    provider must not become the persisted active one."""
    ...
    assert controller.active_profile == "default"
    assert save_calls == []


def test_the_configured_tier_survives_a_profile_switch() -> None:
    """`self._configured_tier`, not `self._model_tier` - the wizard's
    field is a draft; the controller's is the persisted choice."""
    ...
    assert controller.configured_model_tier == "high"


def test_agent_model_tier_and_agent_follow_still_come_from_settings() -> None:
    """Profiles replace the transport scalars only. The controller keeps
    reading KorvidConfig for tier and follow."""


def test_a_missing_agent_extra_reports_the_install_hint_and_does_not_crash() -> None: ...


def test_a_profile_with_a_config_error_is_never_handed_to_the_factory() -> None: ...
```

- [ ] Step 4 — Run all three:

```bash
uv run pytest -p no:tach tests/ui/test_agent_setup_screen.py tests/ui/test_agent_ui_controller_profiles.py -q
```

- [ ] Step 5 — GREEN, part 1: rewrite `agent_setup_screen.py` as a stage machine driven by data.

Stages in order: **model** (Task 10's screen, pushed first), **endpoint** (only when `catalog.endpoint_requirement(reference)` is not `UNSUPPORTED`; skipped when `OPTIONAL` and the operator presses enter on an empty box), **auth method** (from `catalog.auth_methods(reference, endpoint=self._endpoint)`), **auth fields** (from the chosen descriptor's `fields`), **options** (from `catalog.option_fields(reference)`), **tier**, **test**.

**The endpoint stage runs before the auth-method stage, and that ordering is load-bearing.** The catalog offers `none` only when it is handed a non-empty endpoint, so asking for the auth method first would mean asking with `endpoint=None` and never offering keyless auth to the local-endpoint operator it exists for. Pass the value the endpoint stage collected — `self._endpoint`, which is `None` until that stage completes and the entered string afterwards — not the profile being edited, and not a re-read of anything. When the operator goes back and changes the endpoint, recompute the auth-method list rather than reusing the earlier one; a stale list is the same trap in slower motion. Pin it:

```python
async def test_the_endpoint_stage_runs_before_the_auth_method_stage() -> None:
    """`none` is offered only when an endpoint is known, so the order of
    these two stages decides whether a local-endpoint operator is ever
    offered keyless auth at all."""
    seen: list[str | None] = []

    class _RecordingCatalog(_FakeCatalog):
        def auth_methods(
            self, reference: str, *, endpoint: str | None = None
        ) -> tuple[AuthMethodDescriptor, ...]:
            seen.append(endpoint)
            return super().auth_methods(reference, endpoint=endpoint)

    async with AgentSetupApp(catalog=_RecordingCatalog()).run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://localhost:11434")
        assert seen == ["http://localhost:11434"]


async def test_changing_the_endpoint_recomputes_the_auth_methods() -> None:
    """Going back and clearing the endpoint must withdraw `none`."""
    async with AgentSetupApp(catalog=_FakeCatalog()).run_test() as pilot:
        await _advance_to_auth_method(pilot, endpoint="http://localhost:11434")
        assert "none" in _offered_method_ids(pilot)
        await _go_back_and_clear_the_endpoint(pilot)
        assert "none" not in _offered_method_ids(pilot)
```

Read the real helper names off `tests/ui/test_agent_setup_screen.py` before writing these; `_advance_to_auth_method`, `_go_back_and_clear_the_endpoint` and `_offered_method_ids` are the shapes needed, not necessarily the names already there. Use `tests/ui/waits.py::until()` for any condition polling — never a bare `pilot.pause()` loop, and never a wall-clock assertion.

Rendering is a single dispatch on `SetupFieldKind`, and this is the one place a `match` on an enum is correct — it is exhaustive over a closed set korvid owns, not over a vendor list:

```python
def _widget_for(self, field: SetupField, seed: object | None) -> Widget:
    match field.kind:
        case SetupFieldKind.BOOLEAN:
            return Checkbox(field.label, value=bool(seed), id=f"field-{field.key}")
        case SetupFieldKind.CHOICE:
            return OptionList(*field.choices, id=f"field-{field.key}")
        case SetupFieldKind.INTEGER | SetupFieldKind.TEXT | SetupFieldKind.SECRET_REF:
            return Input(
                value="" if seed is None else str(seed),
                placeholder=field.help_text or "",
                id=f"field-{field.key}",
            )
```

Seeds come from `self._profile.options.get(field.key)` when editing and from `field.default` otherwise — profile first, so an edit never silently resets a value.

`SECRET_REF` inputs are labelled "environment variable **name**", validated as `^[A-Z][A-Z0-9_]*$`, and their value is written to `auth.key`. The screen never calls `os.environ` — resolution happens in the factory, at build time. `INTEGER` inputs parse with `int(value.strip())` inside `try/except ValueError`, reporting into the stage's status line and staying put.

Keep the existing `_mark_done` checklist, the `escape` cancel binding, the `ctrl+r` retry binding, and the `apply_settings`-then-dismiss ordering exactly as they are — three of the migrated tests pin them.

Extract the stage table and the field-to-value conversion into module-level helpers rather than nested functions: ruff C901 counts nested `def`s toward complexity, and this screen has enough stages to trip it.

- [ ] Step 6 — GREEN, part 2: `agent_ui_controller.py`.

`_open_setup` becomes: if `self._profiles.profiles or self._profiles.unparsed`, push `ProfileManagerScreen`; else push `AgentSetupScreen` directly. Handle the manager's result — `activated` calls `self._activate_profile(name)`; `edited` calls `self._persist_profiles(edited)`.

`_activate_profile` builds the provider **first** via the factory (Task 15), applies it through the existing `apply_settings` path, and only persists `agent.profiles.active` when the swap succeeded. On failure it notifies with the factory's message and leaves both the session and the pointer untouched.

`_persist_profiles` calls `save_model_connections` (Task 3) and refreshes the in-memory set from what was written, so the `unparsed` round-trip stays authoritative.

Keep `settings: KorvidConfig` on the constructor. `agent_model_tier` and `agent_follow` are **not** profile fields and still come from there. Continue using `self._configured_tier` for the persisted tier; the wizard's `_model_tier` is a draft and must not be read here.

- [ ] Step 7 — Verify, including the whole UI suite since this rewires a 2,151-line controller:

```bash
uv run pytest -p no:tach tests/ui/ -q
uv run ruff check --fix src/korvid/ui/ tests/ui/
uv run ruff format src/korvid/ui/ tests/ui/
uv run mypy src/korvid/ui/
uv run tach check
```

- [ ] Step 8 — Commit:

```bash
git add -A
git commit -m "feat: generate the setup stages from catalog descriptors

Endpoint, auth and option stages are now rendered from AuthMethodDescriptor
and SetupField. A fake catalog offering an mTLS method korvid has never
heard of produces exactly that prompt, which is the property that makes
adding a provider stop being a korvid source edit. A test greps the screen
for ten vendor names and finds none.

Secret fields collect a variable *name*; the screen never reads the
environment, so a value cannot reach the config file. Option fields are
seeded from the profile being edited rather than from descriptor
defaults, so editing does not silently reset them.

The controller opens the profile manager when profiles exist and setup
directly on a first run. Activation still applies before it persists: a
refused rebuild leaves both the session and the active pointer untouched.
Tier and follow keep coming from KorvidConfig - profiles replace the
transport scalars only.

tests/ui/test_agent_setup_screen.py is migrated rather than deleted;
every behaviour it pinned that still exists still has a test.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 3, 9, 10. **Blocks:** Task 16.

---

## Commit group 4 — The LiteLLM transport (Tasks 13–16)

### Task 13: Build the request, once, in one place

Separating request construction from streaming keeps both testable and keeps the outbound snapshot honest — the snapshot must show what was actually sent.

**Files**

- `src/korvid/providers/litellm_request.py` (new)
- `tests/providers/test_litellm_request.py` (new)

**Interfaces**

```python
class _OmitApiKey:
    """Sentinel: pass no `api_key` argument at all.

    Distinct from `None`, and it has to be. `provider-default` means "let
    the vendor SDK use its own environment/default credential chain".
    Passing `api_key=None` does not do that — the SDK sees an explicit
    argument and stops consulting its chain — and passing the keyless
    sentinel string would send a literal bogus credential. The only
    behaviour that delegates is the argument being *absent* from the call,
    so the plan needs a third state that `call_kwargs` can act on.
    """

    def __repr__(self) -> str:
        return "OMIT_API_KEY"


#: The "do not pass `api_key`" marker. Compared with `is`.
OMIT_API_KEY: Final = _OmitApiKey()


@dataclass(frozen=True, slots=True)
class RequestPlan:
    """A fully resolved LiteLLM call, as data.

    Built once per request so the outbound snapshot and the wire payload
    are the same object rather than two constructions that can drift.
    """

    model: str
    #: Tri-state. A `str` is the resolved credential; `None` means "no
    #: credential was resolved" (a keyless endpoint, which still needs the
    #: sentinel on the wire because OpenAI-shaped clients refuse to build
    #: without one); `OMIT_API_KEY` means "pass nothing".
    api_key: str | None | _OmitApiKey
    base_url: str | None
    api_version: str | None
    extra: Mapping[str, object]

    def call_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool,
    ) -> dict[str, Any]: ...


def build_plan(
    *,
    model: str,
    api_key: str | None | _OmitApiKey,
    base_url: str | None,
    options: Mapping[str, object],
    supported: Sequence[str],
) -> RequestPlan:
    """Resolve config into a plan, dropping options the provider rejects."""
```

**Steps**

- [ ] Step 1 — RED. Create `tests/providers/test_litellm_request.py`:

```python
def test_option_keys_are_filtered_to_what_the_provider_accepts() -> None:
    """An unsupported parameter is a 400 from the vendor. Dropping it
    locally is better than a failed request the operator cannot explain."""
    plan = build_plan(
        model="anthropic/claude-sonnet-4-5",
        api_key="k",
        base_url=None,
        options={"temperature": 0.2, "num_ctx": 8192},
        supported=["temperature", "max_tokens"],
    )
    assert plan.extra == {"temperature": 0.2}


def test_korvid_owned_option_keys_never_reach_the_wire() -> None:
    """`native_thinking` selects a transport; it is not a model
    parameter. Leaking it would be a vendor-side 400."""
    plan = build_plan(..., options={"native_thinking": True}, supported=["native_thinking"])
    assert "native_thinking" not in plan.call_kwargs([], [], stream=True)


def test_the_argument_names_match_litellms_actual_signature() -> None:
    """`base_url` and `api_version` are named parameters of acompletion;
    `api_base` is only reachable through **kwargs. Verified against
    1.98.0 by inspecting the signature."""
    kwargs = build_plan(
        model="openai/gpt-4o", api_key="k", base_url="https://h/v1",
        options={}, supported=[],
    ).call_kwargs([{"role": "user", "content": "hi"}], [], stream=True)
    assert kwargs["base_url"] == "https://h/v1"
    assert "api_base" not in kwargs


def test_streaming_requests_ask_for_usage() -> None:
    """LiteLLM passes provider usage through verbatim only when it
    arrives on a choices-free chunk, which requires include_usage."""
    kwargs = build_plan(...).call_kwargs([], [], stream=True)
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}


def test_a_non_streaming_request_omits_stream_options() -> None:
    kwargs = build_plan(...).call_kwargs([], [], stream=False)
    assert kwargs["stream"] is False
    assert "stream_options" not in kwargs


def test_an_empty_tool_list_is_omitted_rather_than_sent_empty() -> None:
    """Several providers reject `tools: []`."""
    assert "tools" not in build_plan(...).call_kwargs([], [], stream=True)


def test_the_key_is_passed_explicitly_so_no_ambient_key_can_be_used() -> None:
    """Passing api_key=None would let the SDK fall back to
    OPENAI_API_KEY. A profile that asked for no credential must send
    none, not whichever key happens to be exported."""
    kwargs = build_plan(..., api_key=None, base_url="http://localhost:8000/v1",
                        options={}, supported=[]).call_kwargs([], [], stream=True)
    assert kwargs["api_key"] == KEYLESS_API_KEY_SENTINEL


def test_provider_default_auth_passes_no_api_key_argument_at_all() -> None:
    """`provider-default` means "use the vendor SDK's own credential
    chain". An explicit argument - `None` or a sentinel - stops that chain
    being consulted, so the only correct behaviour is absence.

    The assertion is unconditional on purpose: `"api_key" not in kwargs or
    plan.api_key is None` would pass for *any* implementation that leaves
    the key out **or** sets it to None, which is exactly the bug.
    """
    plan = build_plan(
        model="bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
        api_key=OMIT_API_KEY,
        base_url=None,
        options={},
        supported=[],
    )
    kwargs = plan.call_kwargs([], [], stream=True)
    assert "api_key" not in kwargs
    assert plan.api_key is OMIT_API_KEY


def test_the_omit_sentinel_is_distinguishable_from_no_credential() -> None:
    """Collapsing the two states is the defect this sentinel prevents."""
    keyless = build_plan(..., api_key=None, base_url="http://localhost:8000/v1",
                         options={}, supported=[])
    delegated = build_plan(..., api_key=OMIT_API_KEY, base_url=None,
                           options={}, supported=[])
    assert keyless.api_key is not delegated.api_key
    assert "api_key" in keyless.call_kwargs([], [], stream=True)
    assert "api_key" not in delegated.call_kwargs([], [], stream=True)


def test_the_plan_is_frozen_so_a_snapshot_cannot_drift_from_the_wire() -> None:
    with pytest.raises(AttributeError, match="cannot assign"):
        build_plan(...).model = "other"  # type: ignore[misc]


def test_options_are_deep_copied_out_of_the_frozen_profile_mapping() -> None:
    """Profile options are MappingProxy-wrapped; litellm may mutate what
    it is handed."""
    kwargs = build_plan(..., options={"extra_headers": {"x": "1"}},
                        supported=["extra_headers"]).call_kwargs([], [], stream=True)
    kwargs["extra_headers"]["x"] = "2"
    assert build_plan(...).extra["extra_headers"] == {"x": "1"}
```

- [ ] Step 2 — Run the RED, expect `ModuleNotFoundError`.

- [ ] Step 3 — GREEN. `build_plan` drops any key in `_KORVID_OWNED_OPTIONS = frozenset({"native_thinking", "ca_bundle", "num_ctx_source"})`, then keeps only keys present in `supported` (an empty `supported` means "the lookup failed" and keeps everything, because dropping the operator's explicit settings on a lookup miss is worse than a vendor 400 that names the parameter). Values are deep-copied through `copy.deepcopy` so a frozen profile's `MappingProxyType` never reaches an SDK that may mutate.

`call_kwargs` assembles exactly:

```python
kwargs: dict[str, Any] = {
    "model": self.model,
    "messages": messages,
    "stream": stream,
}
if self.api_key is not OMIT_API_KEY:
    # Absent, not None: `provider-default` delegates to the vendor SDK's
    # own credential chain, and an explicit argument - even None - stops
    # that chain being consulted. Every other method passes the resolved
    # key, or the keyless sentinel when the profile genuinely has none,
    # so the SDK's OPENAI_API_KEY lookup can never smuggle an unrelated
    # ambient key onto the wire.
    kwargs["api_key"] = self.api_key or KEYLESS_API_KEY_SENTINEL
if tools:
    kwargs["tools"] = tools
if self.base_url:
    kwargs["base_url"] = self.base_url
if self.api_version:
    kwargs["api_version"] = self.api_version
if stream:
    kwargs["stream_options"] = {"include_usage": True}
kwargs.update(self.extra)
return kwargs
```

`api_version` is lifted out of `options` in `build_plan` when present, because it is a named `acompletion` parameter rather than a passthrough.

- [ ] Step 4 — Verify and commit:

```bash
uv run pytest -p no:tach tests/providers/test_litellm_request.py -q
uv run ruff check --fix src/korvid/providers/litellm_request.py tests/providers/test_litellm_request.py
uv run ruff format src/korvid/providers/litellm_request.py tests/providers/test_litellm_request.py
uv run mypy src/korvid/providers/litellm_request.py
git add -A
git commit -m "feat: resolve each request into one frozen plan

Building the call once means the outbound snapshot and the wire payload
are the same object rather than two constructions that can drift.

Parameter names are taken from acompletion's real signature in 1.98.0:
base_url and api_version are named parameters, api_base is only reachable
through **kwargs, so korvid uses the named ones. Streaming asks for
include_usage because LiteLLM only passes provider token counts through
verbatim when they arrive on a choices-free chunk - otherwise it
substitutes its own tokenizer estimate.

api_key is tri-state. A resolved key is passed; a genuinely keyless
private endpoint gets a sentinel so the SDK's own OPENAI_API_KEY lookup
cannot smuggle an unrelated ambient key onto the wire; and
provider-default passes no api_key argument at all, because an explicit
argument - None included - stops the vendor SDK consulting its own
credential chain, which is the entire point of that method.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 5B, 6. **Blocks:** Task 14.

---

### Task 14: The provider — normalize the stream, preserve the contract

One `LLMProvider` for every model reference. This is the file that replaces four vendor adapters.

**Files**

- `src/korvid/providers/litellm_provider.py` (new)
- `tests/providers/test_litellm_provider.py` (new)

**Interfaces**

```python
class LiteLLMProvider(LLMProvider):
    """`LLMProvider` over `litellm.acompletion`.

    Args:
        plan: The resolved request plan (Task 13).
        descriptor: What the UI and the router display.
        capabilities: Translated from catalog data, never inferred from
            the model name.
        client: An optional pre-built SDK client. Only used by tests, and
            passed through `acompletion(client=...)` - which is a
            kwargs-only parameter in 1.98.0.
    """

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]: ...
```

**Steps**

- [ ] Step 1 — RED. Create `tests/providers/test_litellm_provider.py`. Use the verified mock-transport seam:

```python
def _provider(handler, **kwargs) -> LiteLLMProvider:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        base_url="https://mock.invalid/v1",
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return LiteLLMProvider(plan=..., client=client, **kwargs)


async def test_request_sent_is_yielded_after_the_transport_accepted() -> None:
    """`await acompletion(stream=True)` raises before returning on a
    connection failure, so REQUEST_SENT immediately after the await means
    'sent', not 'intended' - which is exactly the contract."""
    events = [e async for e in provider.complete([{"role": "user", "content": "hi"}], [])]
    assert events[0] == {"type": REQUEST_SENT}


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: (_ for _ in ()).throw(
            httpx.ConnectError("refused", request=request)
        ),
        lambda request: (_ for _ in ()).throw(
            httpx.ReadError("reset", request=request)
        ),
        lambda request: (_ for _ in ()).throw(
            httpx.ConnectTimeout("timed out", request=request)
        ),
        lambda request: (_ for _ in ()).throw(
            httpx.ReadTimeout("timed out", request=request)
        ),
    ],
    ids=["connect-error", "read-error", "connect-timeout", "read-timeout"],
)
async def test_a_transport_failure_yields_no_request_sent(failure) -> None:
    """Nothing reached the provider, so the outbound panel must not claim
    a payload was delivered."""
    collected: list[dict[str, object]] = []
    with pytest.raises(ProviderRequestError, match="refused|reset|connect|time"):
        async for event in _provider(failure).complete([], []):
            collected.append(event)
    assert collected == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
async def test_an_answered_error_status_still_yields_request_sent(status: int) -> None:
    """`agent/provider.py`: REQUEST_SENT fires "as soon as the transport
    has accepted the request (response headers received), before the
    status code is judged: an HTTP 500 answer still means the provider
    has the payload."

    A refused connection and a genuine 500 are indistinguishable by
    exception type - litellm reports both as InternalServerError with
    status_code=500 - so this cannot be keyed on isinstance(exc,
    openai.APIStatusError). It is keyed on httpx.HTTPStatusError appearing
    in the exception's __context__ chain, which only an answered request
    produces.
    """
    def answered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "no"}}, request=request)

    collected: list[dict[str, object]] = []
    with pytest.raises(ProviderRequestError):
        async for event in _provider(answered).complete([], []):
            collected.append(event)
    assert collected == [{"type": REQUEST_SENT}]


async def test_an_error_with_neither_marker_is_treated_as_not_sent() -> None:
    """A request litellm refused before building it never left. Defaulting
    to "sent" would raise a false alarm on every routing rejection."""
    collected: list[dict[str, object]] = []
    with pytest.raises(ProviderRequestError):
        async for event in _provider(_raise_bare_bad_request).complete([], []):
            collected.append(event)
    assert collected == []


async def test_text_deltas_stream_through_in_order() -> None:
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Hello world"


async def test_a_fragmented_tool_call_is_reassembled_and_emitted_once() -> None:
    """Verified against 1.98.0: the id and name arrive on the first
    fragment and the arguments arrive split across later ones with
    id=None, name=None."""
    calls = [e for e in events if e["type"] == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["id"] == "c1"
    assert calls[0]["name"] == "get_pods"
    assert json.loads(calls[0]["arguments"]) == {"ns": "kube-system"}


async def test_two_interleaved_tool_calls_are_keyed_by_tool_call_index() -> None:
    """`choice.index` is 0 on every chunk when n=1, so keying on it would
    merge two parallel calls into one malformed call. The index that
    distinguishes them is `delta.tool_calls[*].index`."""
    calls = [e for e in events if e["type"] == "tool_call"]
    assert [c["id"] for c in calls] == ["c1", "c2"]
    assert json.loads(calls[0]["arguments"]) == {"ns": "kube-system"}
    assert json.loads(calls[1]["arguments"]) == {"ns": "default"}


async def test_a_tool_call_with_unparseable_arguments_surfaces_the_raw_text() -> None:
    """Truncation mid-stream is real. The harness must see what arrived
    and refuse it, rather than the provider inventing `{}`. This is the
    *complete* call whose JSON is bad - distinct from the partial call
    below, which is never emitted at all."""


async def test_a_partial_tool_call_is_dropped_when_the_stream_fails() -> None:
    """A half-received call is not a call. Emitting one would hand the
    harness arguments the model never finished writing, and the harness
    has no way to tell that from a model that meant to send them."""
    collected: list[dict[str, object]] = []
    with pytest.raises(ProviderRequestError):
        async for event in _provider(_truncated_after_first_fragment).complete([], []):
            collected.append(event)
    assert [e for e in collected if e["type"] == "tool_call"] == []


async def test_usage_from_a_choices_free_chunk_is_passed_through_verbatim() -> None:
    """Verified: with include_usage and usage on a chunk carrying no
    choices, LiteLLM reports the provider's own 11/7/18."""
    assert usage == {"type": "usage", "prompt_tokens": 11,
                     "completion_tokens": 7, "total_tokens": 18}


async def test_a_stream_with_no_usage_chunk_reports_none_rather_than_zero() -> None:
    """Zero tokens and unknown tokens are different facts."""
    assert [e for e in events if e["type"] == "usage"] == []


async def test_reasoning_content_is_surfaced_as_a_distinct_event() -> None:
    """Delta.reasoning_content exists in 1.98.0; folding it into text
    would put chain-of-thought in the transcript as if it were an answer."""
    assert {"type": "reasoning", "text": "thinking..."} in events


async def test_cancelling_mid_stream_closes_the_wrapper() -> None:
    """CustomStreamWrapper exposes aclose(), not close()."""
    ...
    assert wrapper.aclose_called is True


async def test_cancellation_propagates_rather_than_being_swallowed() -> None:
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_prepare_messages_is_the_identity() -> None:
    """LiteLLM already translates the OpenAI dialect per provider, so
    korvid must not reshape messages - anything added here would bypass
    the outbound policy."""
    assert provider.prepare_messages(msgs) == msgs


async def test_capabilities_are_never_inferred_from_the_model_name() -> None:
    provider = LiteLLMProvider(
        plan=_plan(model="openai/gpt-4o-with-tools-2000k"),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o-with-tools-2000k"),
        capabilities=ModelCapabilities.unknown(),
    )
    assert provider.capabilities().supports_tools is None


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        ("AuthenticationError", "credential"),
        ("RateLimitError", "rate limit"),
        ("ContextWindowExceededError", "context window"),
        ("APIConnectionError", "could not reach"),
        ("BadRequestError", "rejected"),
    ],
)
async def test_sdk_errors_become_actionable_messages(raised: str, expected: str) -> None:
    """The operator sees what to do, not a stack trace naming an SDK
    they did not install on purpose."""


async def test_every_sdk_error_the_transport_must_map_is_actually_caught() -> None:
    """The `except` clause has to name a base these classes inherit from.

    Measured on 1.98.0: `litellm.exceptions.APIError` is a base for only
    itself, so `except exceptions.APIError` would let every one of these
    escape the transport unmapped and make the REQUEST_SENT branch dead
    code. The transport catches `ProviderSDKError` (openai.OpenAIError).
    """
    for name in (
        "AuthenticationError",
        "RateLimitError",
        "ContextWindowExceededError",
        "APIConnectionError",
        "BadRequestError",
        "NotFoundError",
        "PermissionDeniedError",
        "InternalServerError",
        "ServiceUnavailableError",
        "Timeout",
    ):
        assert issubclass(getattr(exceptions, name), ProviderSDKError), name


async def test_an_unmapped_auth_failure_never_escapes_the_transport() -> None:
    """The end-to-end version of the test above: drive a real 401 through
    `complete` and assert korvid's own error type comes out, not the
    SDK's. This is the assertion that fails if someone narrows the
    `except` clause back to a litellm-rooted base."""
    provider = _provider_answering(status=401)
    with pytest.raises(ProviderRequestError, match="credential"):
        async for _ in provider.complete(messages=[], tools=()):
            pass


async def test_the_transport_does_not_catch_korvids_own_bugs() -> None:
    """`except Exception` would report a korvid TypeError to the operator
    as a provider failure. The clause is scoped to the SDK's base class,
    so a programming error propagates unchanged."""
    provider = _provider_whose_plan_raises(TypeError("korvid bug"))
    with pytest.raises(TypeError, match="korvid bug"):
        async for _ in provider.complete(messages=[], tools=()):
            pass


async def test_no_secret_appears_in_any_error_message() -> None:
    assert "sk-secret-value" not in str(excinfo.value)
```

These need names the rest of the file may not already import. Add them explicitly at the top rather than relying on what is in scope:

```python
import asyncio

import pytest

from korvid.agent.provider import ModelCapabilities, ModelDescriptor
from korvid.providers.litellm_provider import LiteLLMProvider, ProviderRequestError
from korvid.providers.litellm_runtime import ProviderSDKError, exceptions
```

`ProviderRequestError` is korvid's own transport-error type, introduced by this task in `litellm_provider.py` — it replaces `providers/openai_compat.py`'s `ProviderError`, which Task 18 deletes with that module. `ProviderSDKError` and `exceptions` both come from `litellm_runtime` so this file, like the transport itself, names one module for everything LiteLLM-shaped; importing `openai` here directly would put a second SDK name in `providers/` for no gain.

- [ ] Step 2 — Run the RED, expect `ModuleNotFoundError`.

- [ ] Step 3 — GREEN. `complete` is an `async def` with `yield` (an async generator — a plain async function returning an iterator fails the override check under mypy, and `provider.py`'s docstring says so).

Shape:

```python
_MAX_CONTEXT_DEPTH: Final = 8


def _request_reached_the_provider(exc: BaseException) -> bool:
    """Did response headers come back before this failed?

    `agent/provider.py` defines REQUEST_SENT as "the transport has
    accepted the request (response headers received), before the status
    code is judged". litellm gives no direct signal: a refused connection
    and a genuine HTTP 500 are *both* `InternalServerError` with
    `status_code=500`, so `isinstance(exc, openai.APIStatusError)` reports
    a refused connection as sent. The chain does distinguish them -
    `httpx.HTTPStatusError` only exists once a response arrived, and
    `httpx.TransportError` only when one did not. They are disjoint.

    Walks `__context__`, not `__cause__`: measured on 1.98.0, `__cause__`
    is None for every one of these. Bounded so a self-referential chain
    cannot spin.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CONTEXT_DEPTH):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            return True
        if isinstance(current, httpx.TransportError):
            return False
        current = current.__context__
    # Neither marker: litellm rejected the request before building it, so
    # nothing left. Defaulting to "not sent" keeps a false alarm off the
    # outbound panel for every routing rejection.
    return False


async def complete(self, messages, tools, *, stream=True):
    kwargs = self._plan.call_kwargs(messages, tools, stream=stream)
    if self._client is not None:
        kwargs["client"] = self._client  # kwargs-only in 1.98.0
    try:
        response = await acompletion(**kwargs)
    except asyncio.CancelledError:
        raise
    except ProviderSDKError as exc:
        if _request_reached_the_provider(exc):
            yield {"type": REQUEST_SENT}
        raise _translate(exc) from exc
    yield {"type": REQUEST_SENT}
    ...
```

**Catch `ProviderSDKError`, not `exceptions.APIError`.** `ProviderSDKError` is `openai.OpenAIError`, re-exported by `litellm_runtime.py` (Task 6) so this module still imports everything LiteLLM-shaped from one place. The distinction is not stylistic: measured on 1.98.0, `litellm.exceptions.APIError` is a base class for **only itself** — `AuthenticationError`, `RateLimitError`, `NotFoundError`, `BadRequestError`, `ContextWindowExceededError`, `Timeout`, `APIConnectionError`, `InternalServerError`, `ServiceUnavailableError` and `PermissionDeniedError` all inherit from the `openai` hierarchy instead (`AuthenticationError` → `openai.AuthenticationError` → `openai.APIStatusError` → `openai.APIError` → `openai.OpenAIError`) and none of them subclasses `litellm.exceptions.APIError`.

With the wrong base, every clause inside this `except` is unreachable: `_request_reached_the_provider` never runs, `REQUEST_SENT` is never emitted on an answered error, `_translate` never maps anything, and a raw `openai.AuthenticationError` propagates out of the transport into korvid's engine, which has no contract for it. The `REQUEST_SENT` rule below would be dead code that reads as if it worked.

Do **not** "fix" this by widening to `except Exception`. That would swallow `asyncio.CancelledError` on Python 3.7 semantics grounds only by accident (it is a `BaseException` in 3.11+, so the explicit re-raise above stays), but more importantly it would capture korvid's own bugs — a `TypeError` in `call_kwargs` would be reported to the operator as a provider failure. Catch the SDK's base class, and let everything else propagate.

The `yield` on the success path sits **after** the `await`, which is what makes `REQUEST_SENT` mean "sent". The failure path yields it too, but only for an answered request: an HTTP 401 or 500 means the provider has the payload, and the outbound-snapshot panel must show *that* payload rather than a stale one. This is verified, not assumed — see the API baseline table's *Streaming failure timing* row for the measured exception chains. `agent/provider.py`'s existing contract wording already says exactly this ("before the status code is judged"), so no source change is needed there; this is the transport finally honouring it.

`httpx` is imported directly for the two marker classes. It is declared in `[agent]` (Task 5B), so this is a first-class dependency, not a transitive reach into `openai`'s internals — `deptry` would flag the latter.

Consumption accumulates tool calls in `dict[int, _PartialToolCall]` keyed by **`tool_call.index`** — the index of the call within the message, taken from `delta.tool_calls[*].index`, **not** `choice.index`, which is `0` on every chunk when `n=1` and would merge two parallel calls into one malformed call. Arguments are appended in arrival order and `id`/`name` are taken from the first fragment that carries them. Completed calls are emitted **at stream end**, in ascending index order, so the harness always sees whole calls.

When the stream raises mid-iteration, accumulated calls are **dropped** and the error is surfaced. A half-received call is not a call: emitting one would hand the harness arguments the model never finished writing, and the harness cannot distinguish that from arguments the model meant to send. This is distinct from a *complete* call whose arguments do not parse, which is emitted with its raw text so the harness can refuse it explicitly.

Text is `delta.content`; reasoning is `delta.reasoning_content` when present and non-empty. Usage comes from a chunk whose `choices` is empty and which carries `usage` with integer fields; absent that, no usage event is emitted at all.

Cleanup is a `finally` that calls `await response.aclose()` inside `try/except Exception` — the wrapper has `aclose()` and no `close()`, and a close failure must not mask the original error. `asyncio.CancelledError` is re-raised everywhere it is caught.

`_translate` maps the SDK exception classes to a korvid `ProviderRequestError` with a written message, and **never** interpolates `plan.api_key` or any option value into it. It dispatches on `litellm.exceptions` classes by `isinstance` — those are the concrete types LiteLLM raises — while the `except` clause names their shared `openai.OpenAIError` base. The two are not interchangeable: the base is what *catches*, the concrete classes are what *distinguish*. `ProviderRequestError` is defined here and replaces `openai_compat.py`'s `ProviderError`, which Task 18 deletes.

- [ ] Step 4 — Verify and commit:

```bash
uv run pytest -p no:tach tests/providers/test_litellm_provider.py -q
uv run ruff check --fix src/korvid/providers/litellm_provider.py tests/providers/test_litellm_provider.py
uv run ruff format src/korvid/providers/litellm_provider.py tests/providers/test_litellm_provider.py
uv run mypy src/korvid/providers/litellm_provider.py
git add -A
git commit -m "feat: one LLMProvider for every model reference

Replaces four hand-written vendor adapters with one normalizer over
litellm.acompletion.

REQUEST_SENT means the provider has the payload. It is yielded after the
await returns a wrapper, and also when the await raises with an
httpx.HTTPStatusError in its context chain - a 401 or a 500 is an answer,
and the outbound-inspection UI must show what was actually delivered. It
is not yielded for a transport error or a timeout.

The distinction cannot be made on exception type: verified against
1.98.0, a refused connection and a genuine HTTP 500 both surface as
InternalServerError with status_code=500. Only the __context__ chain
separates them - httpx.HTTPStatusError for an answered request,
httpx.TransportError for one that never arrived - and __cause__ is None
for both, so the walk is over __context__.

Tool calls arrive fragmented, with the id and name on the first chunk and
arguments split across later ones. They are accumulated by the tool call's
own index - not choice.index, which is 0 for every chunk at n=1 and would
merge parallel calls - and emitted whole at stream end, so the harness
never sees half a call. A stream that fails mid-flight drops its partial
calls entirely. Unparseable arguments on a complete call surface raw
rather than being replaced with {} - a truncated call must be refused,
not silently repaired.

Usage is reported only from a choices-free chunk, which is where LiteLLM
passes provider counts through verbatim; anywhere else it substitutes a
tokenizer estimate. No usage chunk means no usage event: unknown and zero
are different facts.

reasoning_content is a distinct event, never folded into text - chain of
thought is not an answer.

The except clause names openai.OpenAIError, re-exported by
litellm_runtime as ProviderSDKError. litellm.exceptions.APIError would
have looked right and caught almost nothing: measured against 1.98.0 it
is a base class for only itself, while AuthenticationError, RateLimitError
and the rest inherit from the openai hierarchy. With the wrong base the
REQUEST_SENT branch and the whole translation table would have been
unreachable code, and a raw SDK error would have reached the engine.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 5B, 13. **Blocks:** Tasks 15, 16, 17.

---

### Task 15: The factory — profile to provider, with the refusals

Where a profile becomes a live provider, and where every "this must not be built" rule lives.

**Files**

- `src/korvid/providers/litellm_factory.py` (new)
- `src/korvid/__main__.py` (extend)
- `tests/providers/test_litellm_factory.py` (new)

**Interfaces**

```python
def create_provider_from_profile(
    profile: ModelConnectionConfig,
    *,
    catalog: ModelCatalog | None = None,
    flows: SpecialFlowRegistry | None = None,
    credentials: CredentialStore | None = None,
) -> LLMProvider | None:
    """Build a provider, or None when the profile is unusable.

    Returns None - never raises - for a profile that is merely
    unconfigured or misconfigured: a bad profile disables the agent, it
    does not stop korvid from starting.
    """
```

**Steps**

- [ ] Step 1 — RED. Create `tests/providers/test_litellm_factory.py`. It needs these imports; add all of them rather than discovering them one `NameError` at a time:

```python
import ast
import inspect
import logging
import os
from pathlib import Path

import pytest

from korvid.agent.model_profiles import AuthMethodDescriptor, EndpointRequirement
from korvid.core.config import ConnectionAuthConfig, ModelConnectionConfig
from korvid.providers.litellm_factory import OMIT_API_KEY, create_provider_from_profile
from korvid.providers.special_flows import SpecialFlowRegistry
```

`litellm` is installed from Task 5B onward, so this file needs no `importorskip`; if a test here skips, the extra is missing and the environment is wrong.

```python
def test_a_profile_with_a_config_error_is_refused() -> None:
    """The parser already decided this profile is unusable. Building
    from it anyway would send a request shaped by values korvid could not
    validate."""
    profile = ModelConnectionConfig(model="openai/gpt-4o", config_error="bad options")
    assert create_provider_from_profile(profile) is None


def test_a_profile_without_a_model_is_refused() -> None: ...


@pytest.mark.parametrize("model", ["", "   ", "/", "openai/", "/gpt-4o", "gpt 4o"])
def test_a_malformed_reference_is_refused_rather_than_guessed(model: str) -> None: ...


def test_routing_is_delegated_to_litellm_not_to_a_provider_table() -> None:
    """The point of the whole change: there is no dict mapping a provider
    name to a class."""
    source = Path("src/korvid/providers/litellm_factory.py").read_text(encoding="utf-8")
    assert "get_llm_provider" in source
    for vendor in ("openai", "anthropic", "azure", "bedrock", "ollama", "copilot"):
        assert vendor not in source.lower()


@pytest.mark.parametrize(
    "reference", ["github-copilot/gpt-4o", "github_copilot/gpt-4o"]
)
def test_a_special_flow_claims_the_reference_before_litellm_routes_it(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    """Critical: LiteLLM's own get_llm_provider("github_copilot/...")
    starts an interactive device-login flow *inside the routing call* and
    writes a credential file. korvid must reach its own flow first, and
    it must do so for **both** spellings - the underscore form is the one
    LiteLLM's own tables publish, so it is the one a reference is most
    likely to arrive in.
    """

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_llm_provider must not be reached")

    monkeypatch.setattr(
        "korvid.providers.litellm_runtime.get_llm_provider", _explode
    )
    flows = SpecialFlowRegistry([_flow("github-copilot")])
    provider = create_provider_from_profile(_profile(reference), flows=flows)
    assert isinstance(provider, _FakeFlowProvider)


@pytest.mark.parametrize(
    "reference", ["github-copilot/gpt-4o", "github_copilot/gpt-4o"]
)
def test_a_claimed_prefix_with_no_flow_installed_is_refused_not_routed(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    """The deny-list has to bite even when no flow is registered.
    Otherwise removing the Copilot plugin turns a claimed prefix back into
    a device-login trap."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_llm_provider must not be reached")

    monkeypatch.setattr(
        "korvid.providers.litellm_runtime.get_llm_provider", _explode
    )
    assert create_provider_from_profile(
        _profile(reference), flows=SpecialFlowRegistry()
    ) is None


def test_korvid_never_routes_to_litellms_own_copilot_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioural, not a source grep. A grep over korvid's own files
    cannot see a reference that arrived from LiteLLM's tables, from a
    config file, or from a search result - which is every way this
    reference actually shows up."""
    seen: list[str] = []
    monkeypatch.setattr(
        "korvid.providers.litellm_runtime.get_llm_provider",
        lambda model, **kwargs: seen.append(model) or ("m", "p", None, None),
    )
    for reference in ("github_copilot/gpt-4o", "github-copilot/gpt-4o"):
        create_provider_from_profile(
            _profile(reference), flows=SpecialFlowRegistry([_flow("github-copilot")])
        )
    assert seen == []


def test_an_environment_auth_method_reads_the_named_variable(monkeypatch) -> None:
    monkeypatch.setenv("MY_KEY", "sk-live")
    plan = _plan_for(_profile("openai/gpt-4o", auth=ConnectionAuthConfig(method="environment", key="MY_KEY")))
    assert plan.api_key == "sk-live"


def test_an_unset_named_variable_is_refused_with_the_variable_name(monkeypatch) -> None:
    monkeypatch.delenv("MY_KEY", raising=False)
    assert create_provider_from_profile(...) is None
    assert "MY_KEY" in caplog.text
    assert "sk-" not in caplog.text


def test_environment_auth_never_falls_back_to_another_variable(monkeypatch) -> None:
    """An explicit name that is unset must fail loudly. Falling back to
    OPENAI_API_KEY would send a credential the operator did not choose."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient")
    monkeypatch.delenv("MY_KEY", raising=False)
    assert create_provider_from_profile(...) is None


def test_provider_default_passes_no_key_so_the_sdk_chain_applies(monkeypatch) -> None:
    """This is the method that *deliberately* delegates: AWS profiles,
    Azure managed identity and gcloud ADC all live below the SDK.

    Unconditional: `... not in kwargs or plan.api_key is None` would pass
    for an implementation that sets api_key=None, which does *not*
    delegate - the SDK sees an explicit argument and stops consulting its
    own chain. That is the bug the assertion has to be able to fail on.
    """
    plan = _plan_for(_profile("bedrock-x/model", auth=ConnectionAuthConfig(method="provider-default")))
    assert "api_key" not in plan.call_kwargs([], [], stream=True)
    assert plan.api_key is OMIT_API_KEY


@pytest.mark.parametrize(
    "reference",
    [
        "openai/gpt-4o",
        "anthropic/claude-sonnet-4-5",
        "azure/gpt-4o",
        "gemini/gemini-2.5-pro",
        "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
        "ollama/llama3",
        "groq/llama-3.3-70b-versatile",
        "xai/grok-4",
        "hosted_vllm/qwen",
        "company/internal-v2",
    ],
)
@pytest.mark.parametrize(
    ("endpoint", "allowed"),
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("http://localhost:8000/v1", True),
    ],
)
def test_keyless_auth_requires_an_explicit_endpoint(
    reference: str, endpoint: str | None, allowed: bool
) -> None:
    """Keyless is allowed only when the operator named the host.

    Routing is deliberately **not** patched: these are real references
    resolved by the real `get_llm_provider`, so the test would catch a
    rule that had quietly acquired a provider dimension. Forty
    combinations, one answer per endpoint column, whatever the prefix.

    The earlier revision of this rule asked LiteLLM whether the provider
    "has a default host" via `dynamic_api_base`, and monkeypatched that
    value in the test. Measured on 1.98.0 the field is a *dynamic
    override*: `None` for openai, anthropic, azure, gemini and bedrock,
    and `http://localhost:11434` for ollama. So the old rule allowed a
    keyless POST to `api.openai.com` and refused the local Ollama case it
    was written to permit, and the monkeypatch hid it by asserting
    against invented values instead of real ones.
    """
    profile = _profile(reference, base_url=endpoint, auth=_none_auth())
    assert (create_provider_from_profile(profile) is not None) is allowed


def test_the_keyless_refusal_names_the_missing_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator has to be able to act on it. "Refused" is not a
    message; "keyless auth needs an endpoint" is."""
    profile = _profile("openai/gpt-4o", base_url=None, auth=_none_auth())
    with caplog.at_level(logging.WARNING, logger="korvid.providers.litellm_factory"):
        assert create_provider_from_profile(profile) is None
    assert "endpoint" in caplog.text.lower()
    assert "base_url" in caplog.text


def test_the_none_auth_rule_reads_one_profile_field_and_nothing_else() -> None:
    """A substring check on the source proves nothing; parse it.

    The rule must be a test of the profile's own endpoint. Anything that
    consults routing, a provider set, or a hostname is the inversion this
    revision removed, so assert on the AST of the function that
    implements it: no vendor-shaped string constants, and no call to
    `get_llm_provider` reachable from it.
    """
    import ast
    import inspect

    import korvid.providers.litellm_factory as factory

    tree = ast.parse(inspect.getsource(factory))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_refuse_keyless_without_endpoint"
    )
    vendors = {"openai", "anthropic", "azure", "gemini", "bedrock", "ollama", "groq", "xai"}
    literals = {
        node.value.lower()
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not (literals & vendors)
    called = {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "get_llm_provider" not in called


def test_a_keyring_lookup_failure_disables_rather_than_crashes() -> None: ...


def test_capabilities_come_from_the_catalog_and_stay_unknown_without_one() -> None:
    provider = create_provider_from_profile(_profile("x/y"), catalog=None)
    assert provider is not None
    caps = provider.capabilities()
    assert caps.supports_tools is None
    assert caps.context_window_tokens is None
    assert caps.provenance == {}


def test_provenance_records_where_each_fact_came_from() -> None:
    """`ModelCapabilities.provenance` is a per-fact mapping, not one
    source field: a context window from the catalog and a tool flag the
    operator set are two different provenances on one object."""
    caps = provider.capabilities()
    assert caps.provenance["supports_tools"] is CapabilitySource.CATALOG


def test_a_profile_option_overrides_a_catalog_capability() -> None:
    """An operator who sets num_ctx knows their deployment better than a
    table does."""


def test_the_factory_never_logs_a_secret(caplog) -> None:
    monkeypatch.setenv("MY_KEY", "sk-super-secret")
    create_provider_from_profile(...)
    assert "sk-super-secret" not in caplog.text


def test_a_broken_flow_registry_does_not_prevent_normal_profiles() -> None: ...
```

- [ ] Step 2 — Run the RED, expect `ModuleNotFoundError`.

- [ ] Step 3 — GREEN. Order of operations, and each refusal is a `logger.warning` plus `return None`:

1. `profile.config_error is not None` → refuse.
2. `split_reference(profile.model)` → refuse on empty prefix-with-slash, empty tag, or whitespace.
3. `flows.claim(reference)` and `flows.claim_by_option(reference, profile.options)` → if either matches, delegate to the flow's provider builder and return. **Before any LiteLLM call.** `claim` normalizes `_`→`-`, so the underscore spelling LiteLLM's own tables publish claims the same flow as korvid's hyphen spelling.
4. `normalize_prefix(reference_prefix) in flows.claimed_prefixes` → refuse. This is the case where a prefix is claimed *by the deny-list* but no flow is installed to serve it: without this step, uninstalling the Copilot plugin would turn `github_copilot/...` back into a device-login trap on the next routing call. Steps 3 and 4 together are what make step 5 safe, and both run before it.
5. **Apply the `none`-auth rule, before anything is resolved or routed:** refuse when `auth.method == "none"` **and** `profile.base_url` is falsy (absent, empty, or whitespace-only). Allow otherwise. It reads one field of the profile, so it needs nothing from the steps below and belongs above them — a refusal should cost nothing:

```python
def _refuse_keyless_without_endpoint(profile: ModelConnectionConfig) -> str | None:
    """`none` auth is only meaningful against an endpoint the operator named.

    With no endpoint the request goes to whatever default host the SDK
    picks, and an unauthenticated request to a host the operator did not
    choose is a request to somebody else's service. With an endpoint it
    is their own gateway, proxy or local runtime, which is the entire
    reason `none` exists.

    Deliberately no provider dimension. An earlier revision asked
    `get_llm_provider` whether the provider had a default host and read
    `dynamic_api_base`; that field is a dynamic *override*, None for
    openai/anthropic/azure/gemini/bedrock and set for ollama, so the rule
    inverted on every reference that mattered.
    """
    if profile.auth.method != "none":
        return None
    if profile.base_url and profile.base_url.strip():
        return None
    return (
        "keyless auth ('none') requires an endpoint: set base_url on this "
        "profile, or choose an auth method that supplies a credential"
    )
```

6. Resolve the credential from `profile.auth` — five methods, each explicit, none falling back to another. `provider-default` resolves to `OMIT_API_KEY`, not `None`.
7. `get_llm_provider(model=reference, api_base=profile.base_url or None)` inside `try/except Exception` → on failure, refuse with the reference in the message. This call is now safe because steps 3 and 4 already handled every reference that makes it dangerous. Its `dynamic_api_base` return value is **not** consulted by any refusal; the routing call is here to validate the reference and name the provider for `supported_params`, nothing more.
8. Refuse a profile whose resolved provider genuinely cannot be reached without an endpoint but has none — this is where Azure's real requirement lives. It is a *build-time* refusal with the missing field named, not a catalog hint, because this is the point at which the consequence exists and the routing result is in hand.
9. `supported_params(...)` → `build_plan(...)` → `LiteLLMProvider(...)`.

Auth resolution:

| method | resolution |
|---|---|
| `none` | `None`; refused by step 5 whenever the profile has no `base_url` — before any credential resolution or routing happens |
| `environment` | `os.environ[auth.key]`, refused when unset — never another variable |
| `keyring` | `keyring.get_password("korvid", auth.key)`, `except Exception` → refuse |
| `provider-default` | `OMIT_API_KEY`, so `call_kwargs` passes **no** `api_key` argument and the SDK's own chain applies |
| `device-login` | only reachable when a flow claimed the reference |

`provider-default` resolving to a distinct sentinel rather than `None` is load-bearing: `None` and "omit" would collapse into the same `call_kwargs` branch, and passing `api_key=None` stops the vendor SDK consulting its own credential chain — which is the only thing this method is for.

`provider-default` is the only method that lets ambient credentials through, and only because the operator chose it by name. That is the difference between delegation and leakage.

The `none`-auth rule reads `profile.base_url` and nothing else — not a vendor set, and not a routing result. A vendor set is the table this whole change removes and would be wrong the day LiteLLM adds a provider; a routing result was tried and measured wrong in the opposite direction (`dynamic_api_base` is `None` for exactly the hosted vendors the rule exists to protect). One field of the operator's own profile is the only input that answers the actual question: *did the operator choose this host?*

The cost of the stricter rule is stated plainly rather than hidden: an operator running a keyless local Ollama must now write `base_url = "http://localhost:11434"` in the profile. That is a real extra keystroke, and it is the right trade — it makes "no credential" a statement about a host the operator named, instead of an accident of which SDK default happened to apply. The setup UI's endpoint stage runs before its auth-method stage precisely so this is a normal step rather than a surprise refusal (Task 11), and the migration in Task 2B carries the legacy `base_url` across, so an existing keyless Ollama config keeps working without operator action.

Capability translation: start from `ModelCapabilities.unknown()`, fill only from the catalog entry's non-`None` fields recording `CapabilitySource.CATALOG` in `provenance`, then let explicit profile options override with `CapabilitySource.USER`. The field names are korvid's, not the catalog's — `ModelEntry.context_window_tokens` maps onto `ModelCapabilities.context_window_tokens`, and `ModelEntry.max_output_tokens` has no capability counterpart and is display-only. `CapabilitySource` has exactly four members (`USER`, `PROVIDER`, `CATALOG`, `FALLBACK`); do not invent a fifth. Never infer from the reference.

- [ ] Step 4 — Interim wiring in `__main__.py`. The legacy `create_provider` path stays live until Task 18: profiles route through `create_provider_from_profile`, and a config with no profiles keeps using the old path. Both are wired, one is chosen per call. Say so in the commit body — a reviewer seeing two factories should read it as a deliberate two-step, not a leftover.

- [ ] Step 5 — Verify and commit:

```bash
uv run pytest -p no:tach tests/providers/ tests/test_main_wiring.py -q
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
uv run mypy src/
uv run tach check
git add -A
git commit -m "feat: build providers from profiles, with the refusals explicit

Routing is delegated to litellm.get_llm_provider. There is no dict from a
provider name to a class, and a test greps this file for six vendor names
and finds none.

The special-flow registry gets the reference first, and that ordering is
load-bearing rather than stylistic: LiteLLM's own
get_llm_provider('github_copilot/...') starts an interactive GitHub
device-login flow *inside the routing call*, blocks polling for a code,
and writes ~/.config/litellm/github_copilot/api-key.json before raising.
korvid must never reach that path, so it claims the prefix first and a
test asserts the string github_copilot/ appears nowhere in src/.

Every auth method resolves exactly what it names and never falls back:
an explicit environment variable that is unset is a refusal, not a quiet
switch to OPENAI_API_KEY. provider-default is the single method that lets
an ambient credential through, and only because the operator picked it by
name - that is delegation, not leakage.

Keyless auth is allowed only when the profile names an endpoint. The
rule reads one field of the operator's own profile and runs before any
routing, so it costs nothing and cannot be inverted by an SDK detail. An
earlier draft asked litellm whether the provider had a default host and
read get_llm_provider's dynamic_api_base; measured against 1.98.0 that
field is a dynamic override - None for openai, anthropic, azure, gemini
and bedrock, and http://localhost:11434 for ollama - so that rule would
have permitted a keyless POST to api.openai.com while refusing the local
Ollama it existed to allow. The tests now parametrize over real
references with routing unpatched.

A misconfigured profile returns None and disables the agent rather than
stopping korvid from starting.

The legacy create_provider path stays wired until Task 18 deletes it.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 5B, 13, 14. **Blocks:** Tasks 16, 17, 18.

---

### Task 16: Prove the safety boundary survived the transport swap

The invariants in AGENTS.md are not the transport's to keep — but a transport swap is exactly when they break. This task adds no feature. It proves the ones that matter still hold end to end.

**Files**

- `tests/agent/test_litellm_boundary.py` (new)
- `tests/tools/test_executor.py` (extend if a fake needs a new member)

**Steps**

- [ ] Step 1 — RED. Create `tests/agent/test_litellm_boundary.py`, driving a real `NativeAgentEngine` with a `LiteLLMProvider` on a mock transport:

```python
async def test_the_outbound_policy_still_runs_before_the_wire() -> None:
    """Order is: prepare_messages -> OutboundPolicy -> transport. A
    provider that reshapes messages inside complete() would bypass
    sanitization, size checks and the payload snapshot."""
    assert "sk-leaked" not in sent_body
    assert policy_calls == 1


async def test_the_payload_snapshot_matches_the_bytes_actually_sent() -> None:
    """The user-visible 'what was sent' panel must not be a
    reconstruction."""
    assert json.loads(sent_body)["messages"] == snapshot["messages"]


async def test_a_write_tool_still_requires_approval_through_the_transport() -> None:
    """The model asking is not the model doing."""
    assert bridge.confirm_calls == 1
    assert write_ops.calls == []


async def test_a_denied_approval_is_reported_back_to_the_model_as_denied() -> None: ...


async def test_an_audit_write_failure_still_blocks_the_action() -> None:
    """Fail-closed. Unchanged by the transport, and worth a test that
    says so at this seam."""
    assert write_ops.calls == []


async def test_conversation_repair_still_pairs_tool_calls_with_results() -> None:
    """A fragmented tool call reassembled by the new provider must
    produce the same repaired history the old adapters did."""


async def test_a_cancelled_turn_leaves_no_orphaned_stream() -> None: ...


async def test_request_sent_still_distinguishes_sent_from_intended() -> None: ...


async def test_masking_still_applies_to_a_sensitive_read_result() -> None: ...
```

- [ ] Step 2 — Run it. Some will pass immediately (the invariants live above the transport, which is the point); the ones that fail identify real regressions. Fix the provider, never the invariant.

- [ ] Step 3 — Run the whole suite with coverage, because this is the last task in the group and the group added a lot of code:

```bash
uv run pytest --cov -q
```

Coverage gate is 80%. If the transport modules drag it down, add tests — do not lower the gate, and do not add `# pragma: no cover` to anything reachable.

- [ ] Step 4 — Commit:

```bash
git add -A
git commit -m "test: prove the safety boundary survived the transport swap

Approval, audit fail-closed, masking, outbound-policy ordering, payload
snapshot fidelity, conversation repair, cancellation and the
sent-vs-intended distinction, all driven end to end through the LiteLLM
provider on a mock transport.

These invariants live above the transport by design, which is exactly why
a transport swap is when they break. Adding no feature here is the point.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 11, 15. **Blocks:** Task 18.

---

## Commit group 5 — The two real exceptions, then the deletion (Tasks 17–18)

### Task 17: Implement the two special flows

Two flows exist. Both are things LiteLLM structurally cannot own for korvid, and both are declared as data through the existing `korvid.provider` entry point group. Neither is a step towards a provider list — a test asserts there is no way to enumerate flows at all.

**Files**

- `src/korvid/providers/flow_copilot.py` (new, from `github_copilot.py`)
- `src/korvid/providers/flow_ollama_thinking.py` (new, from `ollama.py`)
- `pyproject.toml` (entry points)
- `tests/providers/test_flow_copilot.py` (new, migrating `tests/providers/test_github_copilot.py`)
- `tests/providers/test_flow_ollama_thinking.py` (new, migrating the thinking half of `tests/providers/test_ollama.py`)

**Why each one exists — state this in the module docstring, not just here:**

**GitHub Copilot.** LiteLLM has a `github_copilot` provider, and korvid must not use it. Verified against 1.98.0: calling `get_llm_provider(model="github_copilot/gpt-4o")` starts an interactive device-login flow *inside the routing call* — it prints a verification URL and code to stdout, blocks polling for the user to complete it, writes `~/.config/litellm/github_copilot/api-key.json`, and then raises. In a TUI that is a corrupted screen, a hung event loop and a credential file written without consent. korvid owns the device flow so it can render the code in a modal, poll on a worker, store the token in the existing credential store, and let the user cancel.

**Native Ollama thinking.** Ollama's native `/api/chat` exposes a `thinking` field that its OpenAI-compatible `/v1/chat/completions` endpoint does not. korvid's existing adapter surfaces it and users rely on it. LiteLLM routes `ollama/*` through one or the other, but the parity is korvid's behaviour to keep, so the flow claims the **option** `native_thinking` rather than the `ollama/` prefix: with the option off — the default — `ollama/qwen3:8b` goes through LiteLLM like everything else.

**Steps**

- [ ] Step 1 — RED, part 1. Migrate `tests/providers/test_github_copilot.py` to `test_flow_copilot.py`. Keep every existing assertion about the device flow (code rendering, polling, expiry, token storage, cancellation) and add:

```python
def test_the_flow_declares_itself_as_data() -> None:
    flow = copilot_flow()
    assert isinstance(flow, SpecialFlow)
    assert flow.prefix == "github-copilot"
    assert {m.id for m in flow.auth_methods} == {"device-login"}


def test_the_prefix_is_korvids_not_litellms() -> None:
    """LiteLLM's own name is `github_copilot`. korvid declares the hyphen
    spelling because that is what an operator writes and what the docs
    show."""
    assert copilot_flow().prefix == "github-copilot"
    assert "_" not in copilot_flow().prefix


@pytest.mark.parametrize(
    "reference", ["github-copilot/gpt-4o", "github_copilot/gpt-4o"]
)
def test_both_spellings_reach_this_flow(reference: str) -> None:
    """Declaring the hyphen spelling is not enough on its own: LiteLLM's
    tables publish the underscore form, so a reference arriving from
    search or from a config file will often carry it. If that form did
    not fold onto this claim it would fall through to `get_llm_provider`
    and start the SDK's device flow - the exact failure this module
    exists to prevent."""
    registry = SpecialFlowRegistry([copilot_flow()])
    assert registry.claim(reference) is not None


def test_the_device_prompt_is_returned_not_printed(capsys) -> None:
    """LiteLLM's implementation prints to stdout, which corrupts a TUI."""
    prompt = await flow_provider.begin_auth(profile)
    assert isinstance(prompt, DeviceLoginPrompt)
    assert capsys.readouterr().out == ""


def test_no_credential_file_is_written_outside_korvids_store(tmp_path) -> None:
    """LiteLLM writes ~/.config/litellm/github_copilot/api-key.json."""
    assert not (tmp_path / ".config" / "litellm").exists()


def test_a_cancelled_device_login_stores_nothing() -> None: ...
```

- [ ] Step 2 — RED, part 2. `test_flow_ollama_thinking.py`, migrating the thinking assertions from `tests/providers/test_ollama.py`:

```python
def test_the_flow_claims_an_option_not_a_prefix() -> None:
    flow = ollama_thinking_flow()
    assert flow.claims_option == "native_thinking"
    assert flow.prefix == "ollama"


def test_the_option_defaults_off_so_ollama_routes_through_litellm() -> None:
    """Parity is opt-in. The default path for ollama/* must be the same
    path every other model takes."""
    assert registry.claim_by_option("ollama/qwen3:8b", {}) is None


def test_thinking_content_is_surfaced_when_the_option_is_on() -> None: ...


def test_num_ctx_still_reaches_the_native_endpoint() -> None: ...


def test_a_colon_tagged_model_reaches_the_native_endpoint_intact() -> None:
    """`ollama/qwen3:8b` — the tag colon must survive both the reference
    split and the native request body."""
    assert sent["model"] == "qwen3:8b"
```

- [ ] Step 3 — Run both REDs.

- [ ] Step 4 — GREEN. Move the two modules and reshape their public surface to a `SpecialFlow` plus a provider builder. Register in `pyproject.toml`:

```toml
[project.entry-points."korvid.provider"]
github-copilot-flow = "korvid.providers.flow_copilot:flow"
ollama-thinking-flow = "korvid.providers.flow_ollama_thinking:flow"
```

Registering korvid's own flows through the public extension point is deliberate: it means the extension point is exercised by the shipped build, so a third-party flow cannot be a second-class path that only works in theory.

- [ ] Step 5 — Verify and commit:

```bash
uv run pytest -p no:tach tests/providers/ -q
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
uv run mypy src/
uv run tach check
git add -A
git commit -m "feat: move the two irreducible flows onto the extension point

Copilot device login and native Ollama thinking are the only two things
LiteLLM cannot own for korvid, and both now load through the existing
korvid.provider entry point group as data.

Copilot is not a preference. LiteLLM's github_copilot provider runs an
interactive device login inside get_llm_provider: it prints a URL and
code to stdout, blocks polling, and writes a credential file before
raising. In a TUI that is a corrupted screen, a hung loop and a file
written without consent. korvid claims the github-copilot prefix - its
own spelling, so a reference can never fall through to the SDK's
version - renders the code in a modal, polls on a worker, and stores the
token where every other korvid credential lives.

Ollama thinking claims the native_thinking *option* rather than the
ollama/ prefix, so parity is opt-in and the default path for ollama/*
is the same path every other model takes.

korvid's own flows register through the public entry point, so the
extension point is exercised by the shipped build rather than only in
theory.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 8, 14, 15. **Blocks:** Task 18.

---

### Task 18: Delete the vendor table and guard against its return

The payoff commit. Everything the new architecture replaced comes out, and a test makes growing it back a CI failure.

**Files deleted**

- `src/korvid/providers/openai_compat.py` (184 lines)
- `src/korvid/providers/registry.py` (255 lines) — including `BUILTIN_ADAPTERS` and `create_provider`
- `src/korvid/providers/configurator.py` (211 lines)
- `src/korvid/providers/github_copilot.py`, `src/korvid/providers/ollama.py` (superseded by Task 17)
- `src/korvid/agent/model_catalog.py` (57 lines) if fully superseded — **check first**; if `ModelDescriptor` still lives there, keep the module and delete only the catalog table

**Files changed**

- **`src/korvid/evals/__main__.py`** — **migrated onto `create_provider_from_profile` in Step 1, before any deletion.** It imports `OllamaProvider` and `OpenAICompatProvider` at module scope and branches on `provider_id == "ollama"` inside `provider_factory_from_env`
- **`tests/evals/test_cli.py`** — imports both transports directly; migrated alongside the module it tests
- `src/korvid/core/config.py` — remove the legacy scalars once the migration has run
- `src/korvid/__main__.py` — remove the interim dual wiring from Task 15
- `src/korvid/providers/plugin_registry.py` — drop `OPENAI_COMPAT_ALIASES` and friends, keep `RESERVED_PROVIDER_NAMES`
- `tests/test_vendor_neutrality.py` (new)

**Measured blast radius** — see **File Structure § Measured blast radius** for the full measured table, covering **both `src/` and `tests/`**, and the re-measurement script. Re-run it before starting.

The `src/` half is the part that cannot be deferred: a source module still importing a deleted symbol fails `mypy src/` and breaks pytest **collection**, so the suite cannot even report which tests would have failed. `src/korvid/evals/__main__.py` is the one module outside the routing surface in that position — six references, a module-scope import of both deleted transports, and a live `provider_id == "ollama"` branch. It is easy to miss because `evals/` is outside the vendor guard's scan surface and its tests do not appear in a "test functions touching a removed symbol" count. Step 1 migrates it first, for exactly that reason.

Migrate a test when the behaviour it pins still exists under a new name. Delete it only when the behaviour is genuinely gone, and name each deletion in the commit body. A test count that drops silently is a coverage loss wearing a refactor's clothes.

**Steps**

- [ ] Step 1 — **Migrate `evals/` onto the profile factory, before deleting anything.**

`provider_factory_from_env` builds a provider from `KORVID_EVAL_*` environment variables using the transports this task deletes. Rebuild it as a profile plus one factory call, so the eval harness exercises the same path the TUI does:

```python
def provider_factory_from_env() -> LLMProvider | None:
    """Build the eval harness's provider from KORVID_EVAL_* variables.

    Goes through the same `create_provider_from_profile` the TUI uses,
    so an eval run cannot pass against a transport the product no longer
    ships. The vendor name in `KORVID_EVAL_PROVIDER` is operator input
    that becomes a reference prefix; it is not a branch korvid takes.
    """
    provider_id = os.environ.get("KORVID_EVAL_PROVIDER", "").strip()
    model = os.environ.get("KORVID_EVAL_MODEL", "").strip()
    if not provider_id or not model:
        return None
    base_url = os.environ.get("KORVID_EVAL_BASE_URL", "").strip() or None
    key_var = os.environ.get("KORVID_EVAL_API_KEY_ENV", "").strip()
    profile = ModelConnectionConfig(
        model=f"{provider_id}{MODEL_REFERENCE_SEPARATOR}{model}",
        base_url=base_url,
        auth=ConnectionAuthConfig(method="environment", key=key_var)
        if key_var
        else ConnectionAuthConfig(method="none"),
    )
    return create_provider_from_profile(profile)
```

Three things to get right, each of which is a real behaviour change rather than a rename:

1. **The `provider_id == "ollama"` branch disappears.** It exists only to pick `OllamaProvider` over `OpenAICompatProvider`; with one factory there is nothing to pick. An eval run that wants native thinking sets the `native_thinking` option and Task 17's flow claims it, exactly as the TUI does.
2. **A keyless eval run must now set `KORVID_EVAL_BASE_URL`.** The `none`-auth rule refuses keyless without an endpoint (Task 15), and the harness's default has always been a local endpoint anyway. `tests/evals/test_journeys_cli.py` sets `KORVID_EVAL_PROVIDER: ollama`; give it a base URL in the same commit or the journey silently stops building a provider.
3. **`tests/evals/test_cli.py` imports `OllamaProvider` and `OpenAICompatProvider` at module scope.** Those imports fail at *collection* once the modules are gone, so migrate them here, in the same step, not in the general blast-radius pass. Replace the transport assertions with assertions on the profile the factory receives — that is what the function actually decides now.

Verify the migration before touching anything else:

```bash
uv run pytest -p no:tach tests/evals/ -q
uv run python -c "import korvid.evals.__main__"
```

Then run the full blast-radius script from **File Structure** again and confirm `src/korvid/evals/__main__.py` no longer appears. Any remaining `src/` line is a `mypy src/` failure waiting in Step 4.

- [ ] Step 2 — RED. Create `tests/test_vendor_neutrality.py`. The guard must be **AST-region scoped**, not a raw grep: a substring search over whole files produces false positives on docstrings, on `k8s/csp.py` (cloud *providers* for cluster detection — an entirely unrelated domain), and on any module that legitimately documents a vendor name in prose.

**The scan surface is the routing surface, and it is deliberately narrow.** `src/korvid/providers/`, `src/korvid/agent/`, `src/korvid/ui/` and `src/korvid/core/config.py`. That is where the claim "korvid owns no per-vendor branch" is meaningful. `src/korvid/evals/` runs local benchmark harnesses whose CLI defaults and base URLs legitimately name `ollama` and `openai`; `src/korvid/k8s/` names cloud vendors to detect the *cluster's* CSP; `src/korvid/obs/` and `__main__.py` are wiring. Scanning those would produce a long allow-list of unrelated files, and an allow-list that long is a guard that has stopped saying anything.

**The allowances below are premeasured against the tree this plan starts from, minus what Tasks 16–18 delete.** Run this before writing the file and reconcile any difference — the guard must pass on arrival, not be debugged into passing:

```bash
uv run python - <<'PY'
import ast, collections
from pathlib import Path
TOKENS = {"openai","anthropic","claude","azure","bedrock","gemini","vertex",
          "cohere","mistral","groq","together","ollama","copilot","vllm",
          "deepseek","xai"}
roots = [Path("src/korvid/providers"), Path("src/korvid/agent"), Path("src/korvid/ui")]
paths = sorted({p for r in roots for p in r.rglob("*.py")} | {Path("src/korvid/core/config.py")})
hits = collections.defaultdict(list)
for p in paths:
    tree = ast.parse(p.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
            and isinstance(n.body[0].value.value, str)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            if any(t in n.value.lower() for t in TOKENS):
                hits[p.as_posix()].append((n.lineno, n.value[:60]))
for k in sorted(hits):
    print(k, len(hits[k]), sorted(hits[k])[:3])
PY
```

**Recorded output on the tree this plan starts from** — ten offending modules on the routing surface:

| Module | Disposition |
| --- | --- |
| `providers/configurator.py` | **Deleted by this task** — no allowance needed |
| `providers/github_copilot.py` | **Deleted** (superseded by `flow_copilot.py`, Task 17) |
| `providers/ollama.py` | **Deleted** (superseded by `flow_ollama_thinking.py`, Task 17) |
| `providers/openai_compat.py` | **Deleted** |
| `providers/registry.py` | **Deleted** — this is `BUILTIN_ADAPTERS` itself |
| `providers/entra.py` | **Allowed**: the literal Entra OAuth scope string |
| `providers/plugin_registry.py` | **Allowed**: reserved names third-party plugins may not squat |
| `agent/prompt_harness.py` | **Allowed**: cluster CSP display names, not model routing |
| `agent/model_catalog.py` | **Allowed**, conditionally — drop the line if this task deletes the table and keeps only the dataclass |
| `ui/widgets/agent_setup_screen.py` | **Rewritten** by Tasks 9–11; a surviving vendor token is a bug in those tasks, not an allowance here |
| `core/config.py` | **Region-scoped**, never whole-file — see `_MIGRATION_REGION_NAMES` below |

So: five deletions, four whole-file allowances, one rewrite, one region-scoped module. Re-run the script and reconcile any difference *before* writing the guard; an allowance added later to make a red test pass is how this guard stops meaning anything.

Two consequences of that table are worth stating outright, because both are easy to get backwards:

- **`core/config.py` is not on the allow-list.** It is scoped by AST region. Measured on the starting tree, the region exemption leaves exactly two unexempt hits, both inside `load_config`: the `"github-copilot"` device-login comparison and the `agent_raw.get("ollama")` legacy read. **Task 2B moves both**, which is why `test_load_config_itself_names_no_vendor` below can be asserted rather than aspired to. If Task 2B was skipped, this guard fails on arrival — that is the intended coupling, not a bug.
- **`litellm_catalog.py`'s allowance is scoped by token, not by file.** It is allowed to name the Copilot prefixes it rewrites and nothing else (`ALLOWED_TOKENS` below). Without that scoping, the module most likely to grow a provider table would be the one module the guard could not see into.

```python
"""Vendor names must not reappear as routing decisions.

Scope: this checks *executable* regions of korvid's routing surface -
assignments, comparisons and dict literals - not docstrings or comments.
Prose may name a vendor; code may not branch on one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

VENDOR_TOKENS = frozenset({
    "openai", "anthropic", "claude", "azure", "bedrock", "gemini",
    "vertex", "cohere", "mistral", "groq", "together", "ollama",
    "copilot", "vllm", "deepseek", "xai",
})

#: The routing surface. Not the whole tree - see the task notes for why
#: evals/, k8s/, obs/ and __main__.py are out of scope.
SCANNED_ROOTS: tuple[str, ...] = (
    "src/korvid/providers",
    "src/korvid/agent",
    "src/korvid/ui",
)
SCANNED_FILES: tuple[str, ...] = ("src/korvid/core/config.py",)

#: Modules that legitimately contain a vendor token in executable code,
#: each for a reason unrelated to model routing. Every entry needs a
#: reason; an entry without one is how a guard dies.
ALLOWED: frozenset[str] = frozenset({
    # --- pre-existing, measured on the tree this plan starts from ---
    # ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default" - the
    # literal OAuth scope string Entra requires. Not a branch: it is the
    # identifier of an external protocol, like a URL.
    "src/korvid/providers/entra.py",
    # OPENAI_COMPAT_ALIASES and RESERVED_PROVIDER_NAMES: the plugin
    # registry must refuse third-party plugins that squat on a name
    # korvid ships. Removing the names would remove the protection.
    "src/korvid/providers/plugin_registry.py",
    # Cluster CSP *display* names ("azure" -> "Azure") for the system
    # prompt. This describes the Kubernetes cluster, not the model.
    "src/korvid/agent/prompt_harness.py",
    # ModelDescriptor's fallback catalog. If Task 18 deletes the table
    # and keeps only the dataclass, delete this line with it.
    "src/korvid/agent/model_catalog.py",
    # --- added by this plan ---
    # The two flows LiteLLM structurally cannot own (Task 17).
    "src/korvid/providers/flow_copilot.py",
    "src/korvid/providers/flow_ollama_thinking.py",
    # Reads LiteLLM's shipped tables; the vendor names are *data* these
    # modules iterate and rewrite, never a branch they take. The
    # github_copilot exclusion in litellm_catalog.py is exactly such a
    # rewrite, and it must be able to name the string it excludes.
    "src/korvid/providers/litellm_catalog.py",
    "src/korvid/providers/litellm_runtime.py",
    # RETIRED_PROVIDER_ALIASES: the migration-only map from korvid's old
    # adapter names to LiteLLM prefixes (Task 3).
    "src/korvid/providers/litellm_settings.py",
})

#: `core/config.py` is *not* whole-file allowed. Only the legacy-migration
#: region may name a vendor, and the region is computed from the module's
#: AST rather than by line or by a "legacy" substring - a migration
#: function's body and a migration-only alias table name providers on
#: lines that do not themselves say "legacy". Nested definitions count,
#: because `ast.walk` reaches them.
#:
#: Every name here must exist in the module **at the commit this guard
#: lands in**. Names that earlier tasks introduce and *this* task deletes
#: - `_PREFIXES_WITHOUT_LEGACY_TRANSPORT` and `_legacy_azure_base_url` -
#: must not appear, or the guard fails the moment it arrives.
_MIGRATION_MODULE = "src/korvid/core/config.py"
_MIGRATION_REGION_NAMES: frozenset[str] = frozenset({
    "_migrate_legacy_agent",
    "_migrate_azure_endpoint",
    "_legacy_model_reference",
    "_legacy_auth",
    "_legacy_options",
    "_legacy_ollama_options",
    "_legacy_ollama_number",
    "_LEGACY_OPENAI_COMPAT_NAMES",
    # Migration-only: names whose credential handling changed, warned on
    # load. Measured offender at the pre-plan tree.
    "_LEGACY_REVIEW_NAMES",
    # Warning text mentions the legacy `agent.ollama.num_predict` key.
    "_parse_num_predict",
})


def _migration_line_span(tree: ast.Module) -> set[int]:
    """Every line belonging to a named migration function or assignment."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        name: str | None = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            name = targets[0] if targets else None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        if name in _MIGRATION_REGION_NAMES and node.end_lineno is not None:
            lines.update(range(node.lineno, node.end_lineno + 1))
    return lines


def _scanned_paths() -> list[Path]:
    paths = {p for root in SCANNED_ROOTS for p in Path(root).rglob("*.py")}
    paths.update(Path(name) for name in SCANNED_FILES)
    return sorted(paths)


def _executable_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """`(lineno, value)` for string constants in executable positions.

    Docstrings are excluded by identity, so a module that *documents* a
    vendor name in prose is not an offender. Comments never reach the
    AST at all, which is why this is an AST walk and not a grep.
    """
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_module_branches_on_a_vendor_name() -> None:
    offenders: list[str] = []
    for path in _scanned_paths():
        posix = path.as_posix()
        if posix in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exempt = _migration_line_span(tree) if posix == _MIGRATION_MODULE else set()
        for lineno, value in _executable_strings(tree):
            if lineno in exempt:
                continue
            lowered = value.lower()
            if any(token in lowered for token in VENDOR_TOKENS):
                offenders.append(f"{posix}:{lineno}: {value!r}")
    assert offenders == []


def test_load_config_itself_names_no_vendor() -> None:
    """The pre-plan tree names two vendors inline in `load_config`: it
    infers `device-login` from `provider == "github-copilot"`, and it
    reads the legacy `agent.ollama` sub-mapping by key. **Task 2B moves
    both** into `_legacy_auth` and `_legacy_ollama_options`, where the
    migration exemption covers them. Exempting `load_config` instead
    would exempt the module's largest function - 165 lines - which is
    not a region, it is a hole.
    """
    tree = ast.parse(Path(_MIGRATION_MODULE).read_text(encoding="utf-8"))
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "load_config"
    )
    offenders = [
        f"{lineno}: {value!r}"
        for lineno, value in _executable_strings(target)
        if any(token in value.lower() for token in VENDOR_TOKENS)
    ]
    assert offenders == []


def test_the_migration_exemption_is_a_region_not_the_whole_file() -> None:
    """A vendor name added anywhere in core/config.py outside the named
    migration functions must still fail. Whole-file allowance would make
    the largest module in the change a permanent blind spot."""
    tree = ast.parse(Path(_MIGRATION_MODULE).read_text(encoding="utf-8"))
    exempt = _migration_line_span(tree)
    total = {lineno for lineno, _ in _executable_strings(tree)}
    assert exempt, "the migration region resolved to nothing - names drifted"
    assert not total <= exempt, "the exemption swallowed the whole module"


def test_every_migration_region_name_still_exists() -> None:
    """If a migration helper is renamed, the exemption must move with it
    rather than silently covering nothing. This also catches the reverse
    mistake: naming a helper that *this* task deletes, which would fail
    the guard on the commit that introduces it."""
    tree = ast.parse(Path(_MIGRATION_MODULE).read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    assert _MIGRATION_REGION_NAMES <= defined


def test_every_allowance_names_a_file_that_exists() -> None:
    """A stale allowance silently widens the guard."""
    missing = [name for name in ALLOWED if not Path(name).exists()]
    assert missing == []


def test_the_allowance_is_a_strict_subset_of_the_scan() -> None:
    """The allow-list must not be able to swallow the scan: if a path
    were listed twice, or a directory prefix crept in, the guard would
    quietly stop checking whole subtrees."""
    scanned = {p.as_posix() for p in _scanned_paths()}
    assert ALLOWED < scanned
    assert not any(name.endswith("/") or "*" in name for name in ALLOWED)


#: What each allowed module is allowed to say. An allowance is not a
#: blank cheque: a module may name the vendors its stated reason covers
#: and no others, so a static provider frozenset appearing inside an
#: allowed file fails here instead of hiding behind the allowance.
ALLOWED_TOKENS: dict[str, frozenset[str]] = {
    "src/korvid/providers/entra.py": frozenset({"azure"}),
    "src/korvid/providers/flow_copilot.py": frozenset({"copilot"}),
    "src/korvid/providers/flow_ollama_thinking.py": frozenset({"ollama"}),
    # The catalog's only *written* vendor tokens are the Copilot prefixes
    # it rewrites, because resolving them through litellm starts a device
    # login. Everything else it handles is data it iterates, not a name it
    # spells. If a provider frozenset ever lands here, this fails.
    "src/korvid/providers/litellm_catalog.py": frozenset({"copilot"}),
}


@pytest.mark.parametrize("module", sorted(ALLOWED_TOKENS))
def test_an_allowed_module_only_names_the_vendors_its_reason_covers(
    module: str,
) -> None:
    """Give the guard a chance to see inside its own exceptions.

    `litellm_catalog.py` is allowed because it must name the
    `github_copilot` prefix it rewrites. That reason does not extend to a
    hand-written table of every other provider, which is exactly the
    thing this plan removes and exactly the thing an unscoped allowance
    would hide.
    """
    permitted = ALLOWED_TOKENS[module]
    tree = ast.parse(Path(module).read_text(encoding="utf-8"))
    offenders = [
        f"{lineno}: {value!r}"
        for lineno, value in _executable_strings(tree)
        for token in VENDOR_TOKENS
        if token in value.lower() and token not in permitted
    ]
    assert offenders == []


def test_every_scoped_allowance_is_actually_allowed() -> None:
    """A path in ALLOWED_TOKENS that is not in ALLOWED is a scoping rule
    that never runs, and a file in neither is unscanned by accident."""
    assert set(ALLOWED_TOKENS) <= ALLOWED


def test_the_hand_written_adapter_table_is_gone() -> None:
    for name in ("BUILTIN_ADAPTERS", "create_provider("):
        assert not any(
            name in p.read_text(encoding="utf-8")
            for p in Path("src/korvid").rglob("*.py")
        ), name


def test_the_deleted_modules_are_actually_deleted() -> None:
    for name in ("registry.py", "configurator.py", "openai_compat.py"):
        assert not (Path("src/korvid/providers") / name).exists()
```

- [ ] Step 3 — Run it. Every assertion fails, because nothing has been deleted yet. That is the RED.

```bash
uv run pytest -p no:tach tests/test_vendor_neutrality.py -q
```

- [ ] Step 4 — GREEN. Delete the modules listed above and remove the interim dual wiring so `__main__.py` has exactly one factory. Then work the blast radius file by file:

```bash
uv run pytest -p no:tach -q 2>&1 | tail -40
```

Fix imports, migrate tests, and re-run until green. Resist deleting a failing test to make the suite pass — that is the failure mode this whole task is guarding against.

- [ ] Step 5 — Confirm the deletion is real and the suite did not shrink:

```bash
uv run pytest -p no:tach -q --collect-only 2>&1 | tail -3
git diff --stat HEAD
uv run pytest --cov -q
```

Record the before/after test counts in the commit body.

- [ ] Step 6 — Commit:

```bash
git add -A
git commit -m "refactor: delete the hand-written provider table

Removes registry.py (including BUILTIN_ADAPTERS and create_provider),
configurator.py, openai_compat.py, and the legacy transport scalars whose
migration has already run.

evals/__main__.py is migrated first, before anything is deleted. It
imported both transports at module scope and branched on
provider_id == 'ollama', so deleting them first would have broken pytest
collection for the whole eval suite rather than failing a test. It now
builds a profile from the KORVID_EVAL_* variables and calls the same
create_provider_from_profile the TUI uses, which also means an eval run
can no longer pass against a transport the product does not ship.
tests/evals/test_cli.py moved with it.

tests/test_vendor_neutrality.py makes the table's return a CI failure. It
is AST-scoped to executable string positions rather than a grep over
whole files: prose may name a vendor, code may not branch on one. The
scan surface is the routing surface - providers/, agent/, ui/ and
core/config.py - because that is where the claim means something;
evals/ and k8s/ legitimately name vendors for benchmark endpoints and
cluster detection, and scanning them would produce an allow-list long
enough to say nothing.

Modules allowed by exception each carry a written reason, and the
allowance is scoped by token as well as by path: litellm_catalog.py may
name the Copilot prefixes it rewrites and nothing else, so a static
provider frozenset landing there fails the guard instead of hiding
behind the allowance. core/config.py is not allowed at all - it is
scoped to the named legacy-migration functions and the migration-only
alias table, computed from the module's AST, so a vendor name added
anywhere else in that file still fails.

Seven meta-tests keep the guard honest: every allowance must name a file
that exists, the allowance set must be a strict subset of the scanned
set so a stray directory prefix cannot disable whole subtrees, every
scoped allowance must itself be allowed, each allowed module must stay
within its declared tokens, the migration exemption must not cover the
whole module, every migration helper named in the exemption must still
be defined in the module's AST, and load_config itself must name no
vendor - which is what forced Task 2B to move the device-login
inference and the legacy agent.ollama read into migration helpers rather
than exempting the module's largest function.

Tests referencing deleted symbols were migrated, not dropped, wherever
the behaviour still exists; the commit body lists each genuine deletion
with its reason.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Tasks 16, 17. **Blocks:** Tasks 19, 20.

---

## Commit group 6 — Documentation, gates, and the review loop (Tasks 19–21)

### Task 19: Document what changed, honestly

Including the parts that are worse.

**Files**

- `docs/agent.md` (246 lines) — rewrite the setup walkthrough
- `docs/provider-plugins.md` (433 lines) — rewrite around special flows
- `docs/airgap.md` (187 lines) — the offline story, which is now *better*
- `docs/threat-model.md` (219 lines) — models.dev, the dependency surface, the Copilot hazard
- `docs/dev/agent-decisions.md` (357 lines) — the decision record
- `docs/release-notes/unreleased.md`
- `AGENTS.md` — the extras paragraph
- `docs/dev/specs/…` — link the new design doc

**Steps**

- [ ] Step 1 — `docs/agent.md`. Replace the vendor-first walkthrough with: profiles, `provider/model` references, the five auth methods, model search, multiple profiles and switching. Show a full `config.yaml` with three profiles. Every model reference uses a **slash**.

- [ ] Step 2 — `docs/provider-plugins.md`. The framing changes from "write a plugin to add a provider" to "you almost certainly do not need a plugin any more — every model LiteLLM ships works out of the box; write a flow only when your auth or transport is genuinely non-standard." Document `SpecialFlow`, the entry point, prefix claiming, option claiming, and the deny-list on retired aliases. Keep the existing `ProviderPlugin` section marked as the compatibility path.

- [ ] Step 3 — `docs/airgap.md`. The catalog's primary layer ships inside the wheel, so model search now works with no network at all — an improvement worth stating plainly. Document that models.dev is optional, never fetched at startup, and how to disable it permanently. Document the cache location.

- [ ] Step 4 — `docs/threat-model.md`. Add three entries:

- **Dependency surface.** `[agent]` now pulls 55 distributions including `boto3`, `openai`, `tiktoken` and `tokenizers`. This is a real increase and the mitigation is scope: the extra is optional, the base TUI install is unchanged, and a test proves the base import graph never reaches `litellm`. Also record the counter-argument for why it is worth it — a hand-maintained vendor table is a *correctness* risk that silently rots, and rotted routing sends credentials to the wrong host.
- **LiteLLM lockdown.** Telemetry, message logging and every callback list are disabled at import, before a call can be made. List the flags. Note that `_async_success_callback` is a private attribute, that korvid sets it deliberately, and that a rename upstream fails a test rather than silently reopening the channel.
- **models.dev.** One conditional GET of one public document, no credentials, no prompts, no korvid state, 10s timeout, 12 MiB streaming ceiling, `application/json` only, redirects refused, schema-validated, cached `0600`, never at startup, never routing. State the residual risk plainly: a network observer learns that a korvid instance refreshed its model metadata.
- **The Copilot routing hazard.** Document that LiteLLM's own `github_copilot` provider triggers an interactive device login and writes a credential file from inside a routing call, and that korvid claims the prefix first so that path is unreachable.

- [ ] Step 5 — `docs/dev/agent-decisions.md`. Append the decision record: what was rejected (Pydantic AI, OpenAI Agents SDK, aisuite, a hand-maintained table), why LiteLLM won, the dependency tradeoff and its bound, why the special-flow registry is not a provider list in disguise, and why references use a slash (`ollama/qwen3:8b` — colon-separated references cannot express a model tag that itself contains a colon).

- [ ] Step 6 — Licensing. LiteLLM is MIT; models.dev is MIT. korvid links LiteLLM as a normal PyPI dependency and fetches models.dev's published JSON over its public API — neither is vendored, so no attribution file is added or needed. Record that reasoning in `docs/dev/agent-decisions.md` so the next person does not have to re-derive it. If the repository later grows a `THIRD_PARTY.md`, both belong in it.

- [ ] Step 7 — `docs/release-notes/unreleased.md`. Lead with the user-visible change (profiles, model search, 2,000+ models), then the breaking change (config migrates automatically; the old scalars are gone), then the dependency note.

- [ ] Step 8 — Verify docs build and links resolve:

```bash
uv run mkdocs build --strict 2>&1 | tail -20
rg -n '[a-z0-9-]+:[a-z0-9.-]+' docs/agent.md docs/provider-plugins.md | rg -v 'https?:|^\s*#' | head
```

The second command is a manual check that no colon-form model reference survived in the docs.

- [ ] Step 9 — Commit:

```bash
git add -A
git commit -m "docs: document profiles, the catalog, and the tradeoffs

Rewrites the agent and provider-plugin guides around profiles and model
search, and records the decision history in docs/dev/agent-decisions.md.

The threat model gains four entries, including the ones that are not
flattering: [agent] grew from a handful of packages to 55, and a network
observer can tell that a korvid instance refreshed its model metadata.
Both are stated with their mitigations rather than omitted.

The airgap guide gets a genuine improvement: the catalog's primary layer
ships inside the wheel, so model search now works with no network at all.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Dependencies:** Task 18. **Blocks:** Task 20.

---

### Task 20: The full gate, on every interpreter

**Steps**

- [ ] Step 1 — Local gate:

```bash
make check
uv run pytest --cov -q
uv run deptry src
uv run mkdocs build --strict
```

- [ ] Step 2 — Confirm the structural guards actually run and pass, by name. These are the tests that make the architecture enforceable rather than aspirational:

```bash
uv run pytest -p no:tach -q \
  tests/test_vendor_neutrality.py \
  tests/providers/test_litellm_catalog.py::test_exactly_one_korvid_module_imports_litellm \
  tests/providers/test_litellm_catalog.py::test_exactly_one_korvid_module_imports_the_wrapper \
  tests/providers/test_litellm_catalog.py::test_no_test_asserts_a_catalog_size \
  tests/providers/test_litellm_offline_import.py \
  tests/agent/test_model_profiles.py::test_the_catalog_contract_has_no_adapter_list \
  tests/providers/test_special_flows.py::test_only_the_resolved_entry_point_is_ever_loaded \
  tests/test_optional_extras.py \
  -v
```

- [ ] Step 3 — Confirm no suite is silently skipping:

```bash
uv run pytest -p no:tach -q -rs 2>&1 | rg -i 'skipped' | head -20
```

Every remaining skip must be one that predates this branch, or have a written reason. A skip introduced by this work is a test that is not running.

- [ ] Step 4 — Push and check CI across 3.11/3.12/3.13 and Windows:

```bash
git push
gh pr view --json number,statusCheckRollup \
  --jq '.number, (.statusCheckRollup[] | "\(.name) \(.conclusion // .status)")'
```

- [ ] Step 5 — Fix anything red and repeat. Windows is the usual surprise: path separators in the AST guards (`Path.as_posix()` is used everywhere for exactly this reason) and the cache-directory fallback.

- [ ] Step 6 — Make sure the pull request exists, then mark it ready for review only when every check is SUCCESS.

Task 4 opens the PR as a draft after commit group 1. If that gate was skipped — because the groups were landed back-to-back, or because `gh` was unavailable then — this is the step that creates it. **Exactly one PR carries this change either way**, so check before creating:

```bash
gh pr view --json number --jq .number 2>/dev/null || gh pr create \
  --base main \
  --head agents/provider-neutral-profiles \
  --title "Provider-neutral model profiles" \
  --body "$(cat <<'BODY'
Replaces korvid's single hard-coded provider configuration and CSP-oriented
`:ai` wizard with named model connection profiles.

Provider and model selection become data-driven: the catalog is read from
LiteLLM's shipped offline tables and optionally enriched from models.dev, and
routing is delegated to `litellm.get_llm_provider` / `litellm.acompletion`.
korvid ships no provider class table and no per-vendor extras.

`NativeAgentEngine`, `RequestGateway`, `OutboundPolicy`, `ToolHarness`, the
approval gate, the audit log and the outbound-snapshot contract are unchanged.

Design:
`docs/superpowers/specs/2026-09-05-provider-neutral-model-profiles-design.md`
BODY
)"
```

Then, once every check is SUCCESS:

```bash
gh pr ready
```

`gh pr ready` on an already-ready PR is a no-op, so it is safe to run whichever path created the PR. Do not merge — see AGENTS.md § Pull Requests. This task ends by handing the PR back.

**Dependencies:** Task 19. **Blocks:** Task 21.

---

### Task 21: The review loop

Follow **AGENTS.md § Review Loop** exactly. Restated here only for what is specific to this change.

- [ ] Step 1 — Request review:

```bash
gh pr view --json number --jq .number
gh api -X POST "repos/$(gh repo view --json nameWithOwner --jq .nameWithOwner)/pulls/<number>/requested_reviewers" \
  -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
```

- [ ] Step 2 — Poll. Reviews land in 5–10 minutes and often need two waits:

```bash
gh api graphql -f query='
{
  repository(owner: "OWNER", name: "REPO") {
    pullRequest(number: NUMBER) {
      reviewRequests(first: 10) { nodes { requestedReviewer { ... on Bot { login } } } }
      reviews(last: 5) { nodes { author { login } state submittedAt } }
      reviewThreads(first: 50) { nodes { id isResolved comments(first: 5) { nodes { body path } } } }
    }
  }
}'
```

- [ ] Step 3 — Read **every** comment, including the suppressed low-confidence ones inside the review body's `<details>` block. Classify by confidence and impact.

- [ ] Step 4 — Fix credible findings with TDD: the failing test first, then the fix. Always address correctness, security, data-loss, architecture-invariant and required-check findings.

For this change specifically, expect and take seriously any finding about: a vendor name that slipped back into executable code; the `_async_success_callback` private-attribute access; the `except Exception` seams (there are exactly three, each with a written reason — LiteLLM's unmapped-model `Exception`, third-party entry-point loading, and stream cleanup); the models.dev byte ceiling; and whether a deleted test in Task 18 had a live behaviour behind it.

- [ ] Step 5 — Run the full gate before each commit:

```bash
make check
```

If the pre-commit ruff-format hook rewrites files on the first attempt, `git add -A` and commit again. Never `--amend`, never `--no-verify`.

- [ ] Step 6 — Reply to each comment individually, naming the commit and the test:

```bash
gh api "repos/OWNER/REPO/pulls/<number>/comments/<comment_id>/replies" -f body='Fixed in <sha>; covered by <test>.'
```

- [ ] Step 7 — Resolve each addressed thread:

```bash
gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "PRRT_..."}) { thread { isResolved } } }'
```

- [ ] Step 8 — Re-request review and repeat from Step 2.

- [ ] Step 9 — Track consecutive rounds containing only suppressed low-confidence findings and no unresolved blocking findings. After **two** such rounds, stop making speculative changes and stop requesting further reviews. Any new credible blocking finding resets the counter.

- [ ] Step 10 — At the limit, resolve or document the remaining advisory findings.

- [ ] Step 11 — Verify every required check:

```bash
gh pr view --json statusCheckRollup --jq '.statusCheckRollup[] | "\(.name) \(.conclusion // .status)"'
```

All SUCCESS.

- [ ] Step 12 — Report that the pull request is ready and **stop**.

> **The maintainer merges. You never do.** Not when every check is green, not when the change looks trivial, not when you were told to "finish" it. Do not run `gh pr merge`, do not enable auto-merge, do not add a workflow, action, script or scheduled job that merges, and do not call the REST or GraphQL merge endpoints. This loop ends in a report.

**Dependencies:** Task 20. **Blocks:** nothing. This is the last task.

---

## Task dependency graph

```
1 (landed) ─→ 2 (landed) ─→ 2B ─→ 3 ─→ 4 (draft PR, if instructed)
                             │
                             └──→ 5 ─→ 5B ─┬─→ 6 ─→ 7 ─→ 8 ─┬─→ 9 ──┐
                                (dependency)│                │       ├─→ 11 ──┐
                                            │                └─→ 10 ─┘        │
                                            │                                 │
                                            │              (11 also needs 3)  │
                                            └─→ 13 ─→ 14 ─→ 15 ←──────────────┘
                                                              │
                                     ┌────────────────────────┴────────────────┐
                                     ↓                                         ↓
                                    16  ←── (also needs 11)                   17  ←── (also needs 8, 14)
                                     └────────────────────┬────────────────────┘
                                                          ↓
                                                         18 ─→ 19 ─→ 20 ─→ 21
```

There is no Task 12: it was the "take the dependency" task and now lands as
**Task 5B**, immediately after the vocabulary and before the first module that
imports `litellm`. The number is retired rather than reused, so any surviving
reference to "Task 12" is unambiguously stale rather than silently pointing at
different work. Commit group 2 is therefore Tasks 5, 5B, 6–8; commit group 4 is
Tasks 13–16.

Reading the edges that are not obvious:

- **Task 3 depends on 2B, not 2.** The reference-format correction has to land before anything writes a profile back to disk, or the writer serialises the wrong separator and Task 2B becomes a data migration instead of a test fix.
- **Task 5B hangs off Task 5, and everything LiteLLM hangs off Task 5B.** The dependency lands between the vocabulary and the first module that imports it, so Tasks 6–8 and 13–16 all have a genuinely executable suite. Nothing in Task 5B imports `litellm`; the ordering exists so that everything after it can.
- **Task 16 depends on 11 and 15** — it drives the real engine through the real UI-facing wiring, so both halves must exist.
- **Task 18 depends on 16 and 17**, because deletion is only safe once the replacement is proved end to end *and* the two irreducible flows have somewhere else to live.

Tasks 6–8 are written test-first against the API baseline table **and are executable when written**, because Task 5B lands the dependency before the first of them. Task 5B Step 9 proves the library imports and that Task 2B's Azure test stops skipping; each catalog task then runs its own suite with `-rs` and treats a skip as a failure.

## Verification summary

| Property | Where it is proved |
|---|---|
| No hand-written adapter table | Task 5 `test_the_catalog_contract_has_no_adapter_list`; Task 18 `test_the_hand_written_adapter_table_is_gone` |
| No vendor branch in executable code | Task 18 `test_no_module_branches_on_a_vendor_name` + seven meta-tests, on a premeasured routing surface. Allowances are scoped by **token** as well as by path, so `litellm_catalog.py` may name only the Copilot prefixes it rewrites |
| `evals/` migrated before the transports are deleted | Task 18 **Step 1** — `tests/evals/` green and `import korvid.evals.__main__` clean *before* any file is removed. The blast-radius scan covers `src/` as well as `tests/`, because a source module importing a deleted symbol breaks pytest **collection**, not a test |
| No provider/CSP selection in the UI | Tasks 9, 10, 11 source-grep tests |
| Routing delegated to LiteLLM | Task 15 `test_routing_is_delegated_to_litellm_not_to_a_provider_table` |
| Exactly one module imports litellm, and one imports the wrapper | Task 6 `test_exactly_one_korvid_module_imports_litellm`, `test_exactly_one_korvid_module_imports_the_wrapper` |
| The import is offline and quiet | Task 6 `tests/providers/test_litellm_offline_import.py` — a subprocess that counts zero socket connections and asserts no `StreamHandler` survives |
| LiteLLM locked down at import, loudly | Task 6 `test_importing_the_runtime_locks_litellm_down`, `test_a_renamed_lockdown_flag_fails_the_import_loudly` |
| Nothing LiteLLM prints reaches the terminal Textual owns | Task 6 `test_a_mapped_provider_error_prints_nothing_to_stdout` (`capsys`, driving a real mapped failure) and `test_the_flag_that_protects_stdout_is_not_quietly_droppable`. `suppress_debug_info` gates bare `print()` calls in two `litellm_core_utils` modules that never touch `verbose_logger`, so handler detaching cannot substitute for the flag |
| Provider errors are caught by a base that actually catches them | Task 6 `test_the_runtime_reexports_the_base_class_that_actually_catches_errors`; Task 14 `test_every_sdk_error_the_transport_must_map_is_actually_caught`, `test_an_unmapped_auth_failure_never_escapes_the_transport`, `test_the_transport_does_not_catch_korvids_own_bugs`. `litellm.exceptions.APIError` is a base for only itself; the transport catches `openai.OpenAIError`, re-exported as `ProviderSDKError` |
| Base install has no litellm | Task 5B `test_the_base_install_does_not_import_litellm`, re-run as a gate in Task 6 |
| Slash references, colon tags intact | Task 2B parametrize table (`load_config` round-trip plus the `_legacy_model_reference` unit); Tasks 10, 17 colon-tag tests |
| models.dev bounded, non-routing, provenance-honest | Task 7 — including `test_provenance_stays_litellm_when_the_overlay_adds_nothing` |
| Catalog works offline | Task 6 (no network in any test); Task 8 wiring test |
| `endpoint_requirement` invents no provider table | Task 6 `test_endpoint_is_optional_for_every_reference_no_flow_claims` (Azure included, asserted OPTIONAL) and `test_a_flow_declaration_is_the_only_source_of_a_non_optional_requirement`; Task 8 proves the flow-declared `REQUIRED`/`UNSUPPORTED` cases. `model_cost` records carry no host field, so no such table is derivable |
| Special flows cannot become a list | Task 8 `test_the_registry_is_not_a_provider_list` |
| Only the selected entry point is loaded | Task 8 `test_only_the_resolved_entry_point_is_ever_loaded` (an exploding fake proves it, not a source grep) |
| Copilot never routed through LiteLLM, in either spelling | Task 15 `test_korvid_never_routes_to_litellms_own_copilot_provider` and the parametrized claim/refusal pair — behavioural, with `get_llm_provider` monkeypatched to raise; Task 17 `test_both_spellings_reach_this_flow` |
| Approval, audit, masking intact | Task 16, end to end through the real engine |
| REQUEST_SENT means the bytes left korvid | Tasks 14, 16. Decided by walking `__context__` for `httpx.HTTPStatusError` (sent) versus `httpx.TransportError` (not sent) — **not** by `isinstance(exc, APIStatusError)`, which is true for a refused TCP connection: LiteLLM wraps `httpx.ConnectError` as `InternalServerError` with `status_code=500`, indistinguishable by type or status from a real HTTP 500. Proved by a 4-way parametrized transport table and a 7-way answered-status table |
| `provider-default` omits `api_key` entirely | Task 13 `test_provider_default_auth_passes_no_api_key_argument_at_all`; Task 15 `test_provider_default_passes_no_key_so_the_sdk_chain_applies` — unconditional, using the `OMIT_API_KEY` sentinel so `api_key=None` cannot pass |
| Streaming tool calls survive interleaving and failure | Task 14 `test_two_interleaved_tool_calls_are_keyed_by_tool_call_index`, `test_a_partial_tool_call_is_dropped_when_the_stream_fails` |
| Keyless auth requires an explicit endpoint | Task 15 `test_keyless_auth_requires_an_explicit_endpoint` — parametrized over ten **real** references × four endpoint values, with `get_llm_provider` **unpatched**; plus `test_the_keyless_refusal_names_the_missing_field` and `test_the_none_auth_rule_reads_one_profile_field_and_nothing_else` (AST, not a substring). The catalog mirrors it exactly in Task 6 `test_none_auth_is_offered_only_once_an_endpoint_is_known`, and Task 8 proves a plugin cannot widen it |
| No catalog cardinality is asserted anywhere | Task 6 `test_no_test_asserts_a_catalog_size`; the plan and PR body quote no counts |
| Secrets never persisted or logged | Tasks 3, 11, 14, 15 |
| Rejected profiles remain deletable | Tasks 3, 9 |
| Coverage ≥ 80% | Tasks 16, 18, 20 |
| Exactly one pull request | Task 4 Step 2 opens it under instruction; Task 20 Step 6 opens it only if that gate was skipped, checking first |
