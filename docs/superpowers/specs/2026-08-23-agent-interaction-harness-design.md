# Korvid Agent Interaction Harness Design

**Date:** 2026-08-23

**Issue:** [#316](https://github.com/hellices/korvid/issues/316)

**Related:** [#307](https://github.com/hellices/korvid/issues/307)

## Product Goal

Build an agentic interaction harness that lets a user move naturally between
direct keyboard control of the korvid TUI and conversational control of the same
korvid session.

A user can inspect and manipulate Kubernetes directly, ask the agent to continue
from the exact screen and resource they are looking at, watch the agent navigate
korvid and gather evidence, approve a proposed write in the TUI, and immediately
resume direct control. The agent is not a separate chatbot next to korvid. It is
an optional, safety-bounded way to operate korvid through natural language.

The harness must also work in an air-gapped environment with a local endpoint
such as Ollama. Low-capability local models are a first-class operating target,
not a degraded afterthought. The existing low-model scenarios and prompt
grinding become the eval corpus for that operating policy.

Users who do not need conversational control must be able to install and run the
complete non-agent TUI without agent frameworks, provider clients, or credential
dependencies.

Implementation may use temporary adapters and side-by-side contract tests on the
development branch, but the pull request represents a complete replacement. It
is opened only after Agent v2 is the sole optional agent implementation and the
old runtime plus transition scaffolding have been deleted.

## Representative Journeys

### Direct control to conversation

1. The user opens Pods, scopes to `default`, filters to `api`, and selects
   `api-1`.
2. The user asks, "Why does this keep restarting?"
3. The agent receives typed state for the current cluster, pane, scope, filter,
   selected resource, namespace, and UID.
4. The agent gathers logs, events, and workload state through korvid tools.
5. The answer cites the gathered evidence and can focus the relevant screen or
   resource in the TUI.
6. The user presses normal TUI keys and continues from the state the agent left.

### Conversation to approved write

1. During diagnosis the user asks the agent to restart or scale a workload.
2. The agent proposes the write through the existing tool executor.
3. The TUI owns confirmation and shows the exact preview.
4. korvid revalidates cluster context, resource identity, UID, and write
   reservation.
5. Audit succeeds before mutation, or the write is blocked.
6. The resulting resource state and timeline are visible to both the user and
   later agent turns.

### Optional installation

1. A user installs base korvid without the agent extra.
2. Startup does not import an agent framework, provider implementation, keyring,
   or agent-only HTTP stack.
3. The Kubernetes TUI behaves as it does today.
4. Agent entry points display the existing unavailable/install guidance rather
   than breaking startup.

### Air-gapped local model

1. korvid and its agent extra are installed from an offline artifact source.
2. The configured provider points to a local Ollama-compatible endpoint.
3. No model metadata, prompt catalog, telemetry, or framework service requires
   internet access.
4. The harness selects the low-model policy, compact prompt pack, bounded tool
   surface, sequential loop, and strict budgets.
5. The same direct-to-conversation and approved-write journeys work within those
   bounds.

## What This Refactor Is

This refactor builds a korvid-specific control harness with five responsibilities:

1. capture and synchronize human-visible TUI state;
2. hand control between the user and the agent without losing context;
3. expose bounded korvid UI and Kubernetes operations to the model;
4. select an execution and prompt policy appropriate to the active model;
5. run that policy through an optional backend while preserving korvid safety.

Prompt composition, high/low routing, and framework selection serve those
responsibilities. None is the product goal by itself.

## Pre-1.0 Change Policy

korvid and its agent feature are still pre-1.0. This work may make deliberate
breaking changes when preserving the current shape would compromise the
interaction harness:

- replace agent-facing Python interfaces rather than wrap every legacy method;
- change provider capability and backend plugin contracts;
- change agent configuration keys and accepted values;
- change internal event, prompt-pack, tool-schema, transcript, and eval formats;
- reorganize TUI/controller boundaries needed for shared human-agent state;
- remove `AgentProfile`, `build_profile`, `AgentRuntime`, and temporary v1
  adapters without a long deprecation period.

Compatibility shims are optional migration tools, not architectural
requirements. Retain one only when it is small, explicit, tested, and scheduled
for removal. Obsolete configuration must fail with an actionable migration
message rather than silently selecting different behavior.

This permission does **not** relax product and security invariants:

- the non-agent TUI must remain usable without agent dependencies;
- air-gapped local-model operation must not acquire a cloud dependency;
- writes still require keystroke approval, context/UID revalidation, and
  fail-closed audit;
- Secret masking, outbound sanitization, exact payload inspection, cancellation
  repair, and evidence identity remain mandatory;
- changes to direct TUI behavior must serve the shared interaction model and be
  covered by user-journey tests.

The project should prefer the clean target contract over compatibility layers,
then document breaking changes in release and migration notes.

## Non-goals

- A general-purpose Kubernetes chatbot independent of the korvid TUI.
- Giving an agent a raw Kubernetes client, shell, write adapter, or Textual app.
- Runtime module unloading or switching frameworks during an active process.
- Third-party backend entry-point discovery in the first Agent v2 release.
- Requiring cloud connectivity for model capability lookup or prompt updates.
- Guessing model quality from unverified substrings or parameter counts in a
  model name.
- A unique prompt fork for every model without eval evidence.
- Durable cross-process resume, multi-agent coordination, or graph workflows.
- Keeping the current agent runtime indefinitely after the replacement passes
  its gates.

## Design Principles

### One shared interaction, not two interfaces

The TUI and agent operate one workspace, one cluster context, and one observable
timeline. Agent UI actions must be visible to the user. Direct user actions must
be reflected in the next agent turn.

### Facts cross boundaries, not prompt prose

The UI provides typed state. It does not construct model-facing delimiters or
instructions. Prompt code converts typed state into bounded, sanitized messages.

### Korvid owns authority

The model may request an operation. korvid decides whether the operation is
available, valid, approved, still targets the same resource, auditable, and safe
to execute.

### Low-capability models shape the harness

The low-model path gets explicit operation phases, narrow tool surfaces, compact
schemas, sequential calls, strict budgets, and deterministic recovery. It is not
implemented as scattered `if small_model` branches in a generic loop.

### Optional means absent

Base startup must not import or initialize agent-only dependencies. "Disabled"
cannot mean that an unused framework is still installed and loaded.

### Frameworks are replaceable mechanics

An engine framework may own generic model/tool loop mechanics. It never owns UI
state, model routing policy, prompt selection, outbound enforcement, approval,
audit, or Kubernetes semantics.

## Architecture

```text
                         direct keyboard input
                                  |
                                  v
                         WorkspaceController
                                  |
                         InteractionContext
                                  ^
                                  |
      typed AgentEvent      AgentUiBridge     typed InteractionContext
TUI <-------------------- AgentSession ----------------------------+
 ^                               |                                 |
 |                               v                                 |
 |                      ResolvedAgentPolicy                         |
 |                      /          |       \                        |
 |             ModelRouter   PromptHarness  ToolHarness             |
 |                               |              |                   |
 |                               v              +--> UIControlPort -+
 |                          AgentEngine           |
 |                               |              +--> ToolExecutor
 |                               v                       |
 |                        RequestGateway                 +--> approval/audit
 |                               |
 |                       OutboundPolicy + exact snapshot
 |                               |
 |                           LLMProvider
 +-----------------------------------------------------------------+
                     visible UI navigation and results
```

The optional agent feature begins at `AgentSession` and its backend wiring. The
workspace, tool executor, approval, audit, masking, and provider authentication
remain existing korvid services.

## Components

### 1. Interaction context

`InteractionContext` is a frozen snapshot of what the user can currently act on.

```python
@dataclass(frozen=True, slots=True)
class ResourceIdentity:
    kind: str
    namespace: str | None
    name: str
    uid: str | None
```

```python
@dataclass(frozen=True, slots=True)
class PaneContext:
    kind: str
    scope: str
    filter_pattern: str | None
    selected: ResourceIdentity | None
```

```python
@dataclass(frozen=True, slots=True)
class InteractionContext:
    kube_context: str | None
    context_epoch: int
    focused_pane: PaneContext
    secondary_pane: PaneContext | None
    timeline_cursor: str | None
```

The workspace/controller layer owns the live state. The agent receives snapshots
and cannot mutate their fields.

`context_epoch` changes when the active Kubernetes context changes. Resource UID
is carried when known. These identities support the existing write revalidation
rules; they do not replace the executor's authoritative checks.

Filters, names, namespaces, aliases, and context names are untrusted text. The
prompt harness bounds and sanitizes them before transmission.

### 2. Agent UI bridge

`AgentUiBridge` is the bidirectional seam between Textual and the pure agent
layer.

```python
class AgentUiBridge(ABC):
    @abstractmethod
    def snapshot(self) -> InteractionContext: ...

    @abstractmethod
    async def apply(self, action: UiAction) -> UiActionResult: ...
```

`snapshot()` lets every turn begin from current human-visible state. `apply()`
accepts only typed, validated UI actions such as:

- focus a resource kind;
- change scope;
- select a resource identity;
- set or clear a filter;
- focus the other pane;
- open evidence supported by the current ledger.

The bridge does not expose arbitrary widget lookup, CSS selectors, command
strings, or the Textual `App`.

An applied action updates the real workspace. The next direct keystroke and the
next context snapshot therefore observe the same state.

### 3. Agent session and handoff

`AgentSession` coordinates one conversation with one live TUI workspace.

```python
class AgentSession(ABC):
    @abstractmethod
    def run_turn(self, user_text: str) -> AsyncIterator[AgentEvent]: ...

    @abstractmethod
    def interrupt(self) -> None: ...

    @abstractmethod
    def finalize_interrupt(self) -> TurnInterrupted: ...

    @abstractmethod
    def retarget(self, policy: ResolvedAgentPolicy) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...
```

The session asks the bridge for a fresh context at turn start. The UI does not
serialize context and pass an ad hoc string.

Direct user navigation between turns needs no transcript event because the next
snapshot is authoritative. A Kubernetes context switch creates a one-shot typed
handoff note and a new context epoch, clears stale evidence, and recomposes the
system message. Context changes during an active turn remain blocked by the
existing coordinator.

Agent navigation actions are emitted as typed events and applied through the
bridge. The transcript can describe them, while the workspace itself remains
the source of truth.

### 4. Tool harness

The model sees a policy-selected set of korvid operations, not internal service
objects.

`ToolHarness` composes three existing categories:

- bounded cluster reads;
- writes that always enter `ToolExecutor` and UI approval;
- typed TUI navigation through `AgentUiBridge`.

The registry remains the owner of tool names, schemas, and descriptions. The
harness owns which tools are armed for the resolved policy and how a backend
invokes them.

The harness records evidence for successful cluster reads. Evidence references
remain turn-scoped and carry resource identity needed for navigation. Model-
authored tool arguments and result excerpts never become trusted prompt prose.

Writes cannot be shadowed, replayed speculatively, or executed by an eval run.

### 5. Model routing and execution policy

```python
class ModelTier(Enum):
    LOW = "low"
    HIGH = "high"
```

```python
class CapabilitySource(Enum):
    USER = "user"
    PROVIDER = "provider"
    CATALOG = "catalog"
    FALLBACK = "fallback"
```

```python
@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    provider: str
    model: str
```

```python
@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    context_window_tokens: int | None
    supports_tools: bool | None
    supports_parallel_tools: bool | None
    supports_reasoning: bool | None
    recommended_tier: ModelTier | None
    provenance: Mapping[str, CapabilitySource]
```

Unknown is distinct from false. Nested mappings are copied and frozen.

Routing precedence is:

1. explicit user tier override;
2. provider-reported tier with provenance;
3. shipped, versioned, eval-backed exact `(provider, model)` catalog entry;
4. conservative low-tier fallback.

No online catalog lookup occurs. No model-name substring or parameter-count
heuristic participates in routing.

The temporary v1 adapter may map legacy `full` to `high` and `small` to `low` so
existing behavior can be compared during development. Agent v2 may replace
those values with explicit `low` and `high` configuration. A removed value must
produce an actionable migration error. A model change always re-runs routing;
it cannot silently keep a stale tier from the previous model.

```python
@dataclass(frozen=True, slots=True)
class ResolvedAgentPolicy:
    model: ModelDescriptor
    capabilities: ModelCapabilities
    tier: ModelTier
    route_source: CapabilitySource
    prompt_pack_id: str
    prompt_overlay_ids: tuple[str, ...]
    tools: tuple[ToolSchema, ...]
    max_iterations: int
    max_history_chars: int
    max_result_chars: int | None
    max_tool_calls_per_iteration: int | None
    allow_parallel_tool_calls: bool
    strict_history_budget: bool
```

`ToolSchema` is the agent layer's immutable/copy-owned view of the existing
OpenAI-compatible tool-schema mapping. It does not introduce a second wire
format.

This immutable value explains how the harness will operate. Cluster-dependent
tool gates create a new whole policy during retarget rather than mutating
individual fields.

### 6. Low- and high-model policies

The policies share the same security and interaction contracts but use different
execution harnesses.

#### Low tier

- bounded operation phases compatible with #307;
- smallest tool surface required for the current phase;
- sequential tool calls;
- compact tool schemas and descriptions;
- strict history and result budgets;
- explicit stop, retry, and ask-for-help rules;
- one diagnosis or operation target at a time;
- deterministic fallback when the model emits malformed or excess calls.

The low tier is the default when capabilities are unknown. Ollama is not
automatically low merely because of its provider name; explicit configuration,
provider facts, or an exact shipped catalog entry supplies the route.

#### High tier

- broader diagnostic and TUI-navigation surface;
- multi-step evidence gathering;
- parallel tool calls only when the provider confirms support;
- larger history and result budgets;
- richer synthesis while retaining citation and write constraints.

Tier selection changes policy, not safety authority. Both tiers use identical
approval, audit, revalidation, masking, and outbound enforcement.

### 7. Prompt harness

The prompt harness is more than a string composer. It binds the selected model
policy to korvid's interaction and tool contracts.

Prompt layers are deterministic:

1. immutable korvid safety, evidence, and control-handoff contract;
2. common role: operate the current korvid session, not an abstract cluster;
3. low- or high-tier operating pack;
4. optional provider overlay;
5. optional exact-model overlay;
6. validated additive user rules;
7. armed tool and UI capability clauses;
8. bounded cluster, evidence, and interaction context.

The initial packs are:

- `low-korvid-operator`;
- `high-korvid-operator`.

Provider and exact-model overlays are sparse. An overlay requires a reproducible
failing scenario before the overlay and a passing result after it. Exact model
matching uses normalized provider ID plus exact provider model ID.

User rules may add domain guidance but cannot replace layer 1.

`PromptHarness` owns final system/user message construction and compact tool
description overrides. The UI supplies typed facts; the tool registry supplies
schemas; the evidence ledger supplies trusted references. None writes final
model-facing framing independently.

The existing low-model scenarios and prompt experiments are migrated into
versioned eval cases. Prompt grinding becomes an evidence-producing engineering
loop rather than untracked wording changes.

### 8. Engine backend and request gateway

The engine contract is framework-neutral and operates inside `AgentSession`.

```python
class AgentEngine(ABC):
    @abstractmethod
    def run(
        self,
        request: AgentTurnRequest,
    ) -> AsyncIterator[AgentEvent]: ...

    @abstractmethod
    def interrupt(self) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...
```

`AgentTurnRequest` contains the resolved policy, composed messages, current tool
surface, and turn-scoped execution handles. It does not contain a raw provider,
Textual app, Kubernetes client, or write adapter.

`RequestGateway` is the only provider handle available to an engine. It:

1. copies and adapts messages and schemas for the provider;
2. applies `OutboundPolicy` fail-closed;
3. records the exact sanitized payload;
4. calls `LLMProvider` only with prepared values;
5. streams provider events to the engine.

An engine framework that cannot operate through this gateway without
reimplementing most of itself is not suitable.

### 9. Optional agent wiring

`__main__.py` lazily constructs the agent feature when enabled. Base UI modules
may reference agent interfaces only behind existing type-checking/lazy import
patterns.

During branch development, a temporary v1 adapter may let shared contracts run
against the old implementation. It is test scaffolding, not a shipped backend.
The replacement PR contains one Agent v2 implementation selected when the
optional agent feature is enabled. Model changes rebuild a session using a newly
resolved policy. Runtime hot-plug is unnecessary.

If the agent extra is missing and the user explicitly enabled it, startup shows
an install hint. If the agent was not requested, the TUI starts without loading
agent dependencies.

For air-gapped use:

- prompt packs and model catalog ship in the installed package;
- no capability or prompt lookup calls an external service;
- Ollama/local endpoints use only the configured local address;
- telemetry is not required for policy, prompts, or eval execution;
- optional dependency import tests run with network unavailable.

## Framework Decision

Agent v2 uses a clean native engine against `AgentEngine` and
`RequestGateway`. Pydantic AI was evaluated and rejected for this replacement:

- its public `Model` and `WrapperModel` hooks receive typed `ModelMessage` and
  `ToolDefinition` values before provider-specific serialization; there is no
  stable hook over the exact wire-shaped payload after serialization and before
  transmission;
- preserving `OutboundPolicy` and the exact `OutboundSnapshot` would therefore
  require reimplementing provider serialization and streaming inside a custom
  Pydantic AI model, bypassing the framework model implementation;
- its interruption repair occurs inside its run lifecycle or on the next run,
  while korvid must repair history immediately after external task
  cancellation and before accepting another turn;
- its agent loop does not expose korvid's per-iteration excess-tool-call
  filtering contract; and
- `pydantic-ai-slim` would add Pydantic, AnyIO, Griffe, and related machinery to
  the optional agent extra without replacing korvid-owned request, history,
  tool, evidence, or cancellation logic.

The native engine retains OpenAI-shaped messages behind the existing provider
ABC, so `RequestGateway` can sanitize and record the exact values passed to
`LLMProvider.complete()`. It implements only the model/tool loop, streaming
translation, history budgets, usage accounting, and interruption mechanics
required by the harness. There is no new framework dependency.

LangGraph remains out of scope until the product needs durable cross-process
resume, explicit branching workflows, joined parallel branches, or multiple
actors. Those are not required for natural handoff between one user, one TUI,
and one conversational agent.

## Data Flow

### Turn start

1. The user submits text from the agent panel.
2. `AgentSession` asks `AgentUiBridge` for a fresh `InteractionContext`.
3. The session starts an empty turn-scoped evidence ledger.
4. `PromptHarness` combines policy, interaction context, cluster context, tools,
   and evidence contract.
5. The engine begins the model/tool loop.

### Agent navigates the TUI

1. The model requests a typed UI tool.
2. `ToolHarness` validates the action against the armed surface.
3. `AgentUiBridge.apply()` changes the real workspace.
4. The action result and updated typed context return to the loop.
5. The user sees the same state and can resume direct control immediately.

### Agent reads the cluster

1. A read request crosses the existing executor.
2. Sensitive values are masked and the result is bounded.
3. The evidence ledger assigns a trusted reference.
4. The next model iteration receives the bounded result and updated evidence
   table.
5. Final diagnostic claims are validated against those references.

### Agent requests a write

1. The model requests an armed write tool.
2. The existing executor reserves the write and asks the TUI for confirmation.
3. Only a user keystroke can confirm.
4. Context, identity, UID, and policy are revalidated.
5. Audit succeeds before mutation or the action is blocked.
6. The session emits typed results and the TUI timeline reflects the outcome.

### User resumes direct control

No transfer protocol is required after a completed or interrupted turn. The
workspace is already authoritative. Direct keystrokes operate the state left by
the agent, and a later conversational turn obtains a new snapshot.

## Error Handling

- Missing agent dependencies produce an install hint without breaking an
  unrequested agent-free startup.
- A model that explicitly lacks tool support cannot create a session.
- Unknown capabilities route conservatively and retain their provenance.
- Invalid policy, prompt pack, overlay, or tool references fail before replacing
  a live session.
- A failed rebuild closes only newly created resources.
- Invalid or stale UI actions return typed failures; they do not silently act on
  another row.
- `OutboundPolicyError` blocks provider transmission.
- Provider/framework errors become visible agent errors, never successful turns.
- Cancellation repairs conversation state and releases pending tool execution.
- Context switch clears old evidence before another turn.
- Approval denial, expiry, context/UID mismatch, or audit failure blocks writes.
- Air-gapped mode never substitutes a cloud call or success-shaped capability
  default when local information is unavailable.

## Testing and Eval Strategy

### Interaction journey tests

- direct selection becomes the next turn's exact typed context;
- split pane, scope, filter, and selected UID survive handoff;
- agent navigation changes the real TUI workspace;
- the next direct keystroke acts on the agent-selected state;
- direct navigation between turns appears in the next snapshot;
- context switch produces one handoff note and invalidates evidence;
- interruption returns control without stale pending state.

Textual tests use condition polling, not wall-clock sleeps.

### Tool and safety tests

- every read/write/UI operation crosses its declared port;
- no backend receives raw Textual, Kubernetes, or write objects;
- Secret, prompt, schema, screen, and result data is sanitized;
- exact payload inspection matches provider handoff;
- writes cannot bypass keystroke approval, revalidation, or audit;
- denial, expiry, cancellation, UID replacement, and context switch never
  mutate;
- retries cannot execute a write twice.

### Model policy tests

Pure table-driven tests cover:

- explicit high/low and legacy full/small mapping;
- provider and exact-catalog precedence;
- offline conservative fallback;
- unsupported tool capability;
- model change re-resolution;
- cluster/read-only/integration tool gates;
- immutable provenance and nested schema ownership.

### Prompt and low-model evals

Each low-model scenario records:

- model descriptor and route provenance;
- prompt pack and overlays;
- interaction starting state;
- expected UI/cluster tools and maximum call count;
- expected evidence and final outcome;
- token/request budget;
- failure class before a prompt or harness change.

An accepted prompt change must fix its target scenario without regressing any
existing scenario. An exact-model overlay must retain the scenario that
justifies it.

Cutover requires v2 to meet or exceed v1 on every deterministic journey and
model scenario individually. Low-tier runs must remain inside their configured
iteration, call, result, and request budgets.

### Backend contract tests

Every engine candidate runs:

- text-only, one-tool, and multiple-tool turns;
- malformed, duplicate, and excess calls;
- history and usage accounting;
- provider failure before and after handoff;
- cancellation at every await boundary;
- retarget, evidence invalidation, and orderly close;
- exact outbound snapshot.

### Optional and air-gapped tests

Subprocess tests prove:

- base, MCP-only, and observability-only installs do not import agent backend
  dependencies;
- disabled agent startup does not load a backend;
- missing requested backend shows an install hint;
- local-provider scenarios perform no external network request;
- the TUI remains usable with the agent entirely absent.

## Implementation and Cutover

The steps below are checkpoints on one branch. Commit and review them
independently, but do not open a pull request while both implementations,
temporary adapters, opt-in flags, or migration-only backend selection remain.

1. **Interaction contracts and v1 adapter**
   - Add typed interaction context, UI bridge, session, engine, policy, and
     gateway contracts.
   - Adapt current behavior without changing the default.
   - Change UI and composition-root dependencies to the contracts.

2. **Model policies and prompt harness**
   - Implement low/high routing and legacy aliases.
   - Add `low-korvid-operator` and `high-korvid-operator`.
   - Move all model-facing context and prompt construction into the harness.
   - Convert existing low-model prompt work into versioned eval cases.

3. **Native engine foundation**
   - Record the Pydantic AI rejection and native-engine decision in the agent
     decision log.
   - Implement the clean native engine through the gateway and backend
     contracts.

4. **Agent v2 session**
   - Implement the selected engine, tool harness, evidence, cancellation,
     usage, rebuild, retarget, and close behavior.
   - Keep all UI and cluster effects on retained korvid ports.

5. **Replacement rehearsal and journey parity**
   - Exercise v2 as the only runtime in test and local-run configurations.
   - Run interaction, safety, optional-install, offline, and low/high eval gates.
   - Fix v2 rather than adding new behavior to v1.

6. **Pre-PR deletion and final gate**
   - Remove v1, temporary adapters, opt-in flags, migration backend selection,
     `AgentProfile`, `build_profile`, and `AgentRuntime`.
   - Make Agent v2 the sole implementation whenever the optional agent feature
     is enabled.
   - Retain public behavior, safety, journey, and eval tests.
   - Remove obsolete aliases and compatibility shims; document exact
     configuration and plugin migration steps.
   - Run the complete repository gate and a real local TUI/Ollama journey.
   - Open the pull request only after the worktree contains no old
     implementation and every gate passes.

The old implementation receives only correctness or security fixes during the
replacement.

## Success Criteria

- A user can alternate between direct TUI control and conversation without
  losing current cluster, pane, scope, filter, selection, or resource identity.
- Agent UI actions change the same workspace the user controls.
- Writes remain confirmed only by user keystrokes and fail closed on
  revalidation or audit failure.
- Agent-free installation and startup do not import agent-only dependencies.
- A local Ollama-compatible endpoint can complete the low-tier interaction
  journeys without internet access.
- One immutable policy explains model tier, prompt pack, tools, budgets, and
  route provenance.
- Model changes re-resolve policy rather than preserving a stale profile.
- Prompt/model specialization is exact-match, versioned, and eval-backed.
- Every backend is forced through the same request, tool, UI, approval, masking,
  and audit boundaries.
- The pull request contains Agent v2 as the sole optional agent implementation;
  the current profile/runtime implementation and all transition scaffolding are
  deleted before review begins.
