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

Legacy `full` maps to `high` and `small` maps to `low` during migration. A model
change always re-runs routing; it cannot silently keep a stale tier from the
previous model.

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

### 9. Optional backend wiring

`__main__.py` lazily constructs the agent feature when enabled. Base UI modules
may reference agent interfaces only behind existing type-checking/lazy import
patterns.

The first release uses explicit shipped backend IDs rather than third-party
discovery:

- `v1`: compatibility adapter around the current implementation;
- `v2`: the selected replacement engine.

The backend is selected at startup. Model changes rebuild a session using the
same backend and a newly resolved policy. Runtime hot-plug is unnecessary.

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

Pydantic AI is the first v2 engine candidate because the product has one
streaming tool agent and can benefit from typed dependencies and generic
model/tool-loop ownership.

A thin vertical slice must prove:

- one text turn and one read-tool turn through `RequestGateway`;
- typed event translation;
- exact outbound snapshot parity;
- cancellation without unmatched tool messages;
- optional-import isolation;
- local Ollama-compatible operation without cloud services;
- no access to raw UI, Kubernetes, credentials, or write adapters.

If the framework fails those constraints or retains most loop mechanics in
korvid adapters, implement a small native v2 engine against the same contracts.
That engine is a clean implementation of the harness, not a rename or gradual
decomposition of `AgentRuntime`.

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

## Migration and Cutover

Each step is a separate reviewable PR under #316.

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

3. **Engine vertical slice**
   - Compare Pydantic AI and a native v2 slice through the same gateway and
     contracts.
   - Decide and document the engine before full implementation.

4. **Agent v2 session**
   - Implement the selected engine, tool harness, evidence, cancellation,
     usage, rebuild, retarget, and close behavior.
   - Keep all UI and cluster effects on retained korvid ports.

5. **Opt-in and journey parity**
   - Expose `agent.backend = "v2"` as an explicit opt-in.
   - Run interaction, safety, optional-install, offline, and low/high eval gates.
   - Fix v2 rather than adding new behavior to v1.

6. **Default and deletion**
   - Make v2 the default after every gate passes.
   - Remove v1, `AgentProfile`, `build_profile`, and `AgentRuntime`.
   - Retain public behavior, safety, journey, and eval tests.
   - Keep documented `full`/`small` configuration aliases for the migration
     window.

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
- Agent v2 becomes the default and the current profile/runtime implementation is
  deleted.
