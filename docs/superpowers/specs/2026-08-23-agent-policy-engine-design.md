# Agent Policy and Engine Boundary Design

**Date:** 2026-08-23  
**Issue:** [#316](https://github.com/hellices/korvid/issues/316)  
**Related:** [#307](https://github.com/hellices/korvid/issues/307)

## Summary

korvid will separate model facts, agent policy, prompt composition, and agent
execution before deciding whether to adopt an external agent framework.

The current `full` and `small` profiles remain the user-facing presets. The
first implementation is behavior-preserving: it moves their decisions into an
immutable resolved policy and makes final prompt construction the responsibility
of a dedicated composer.

The UI will depend on an `AgentEngine` boundary rather than the concrete
`AgentRuntime`. The current runtime remains the reference engine. A framework
backend will be evaluated only after this seam exists, using the same provider,
outbound security boundary, tools, typed UI events, and eval harness.

Pydantic AI is the first framework candidate because korvid currently has one
typed, streaming tool agent. LangGraph is deferred until there is a real
graph-shaped requirement such as durable pause/resume, branching workflows, or
multiple actors.

## Problem

The embedded agent currently combines four kinds of decisions.

1. **Model and profile selection**
   - An unset Ollama setup is offered `small`.
   - Other providers default to `full`.
   - `:model` preserves the previous profile.
   - Provider model metadata is reduced to model names.

2. **Execution policy**
   - Tool surface.
   - Iteration and history limits.
   - Per-result and per-iteration tool-call limits.
   - Strict versus best-effort history budgeting.

3. **Prompt policy**
   - Full and small role instructions.
   - UI and write/no-write clauses.
   - Tool-description overrides.
   - Cloud context, screen context, and evidence instructions.

4. **Runtime mechanics and product safety**
   - Streaming model/tool iteration.
   - History and usage accounting.
   - Cancellation repair.
   - Evidence and citation validation.
   - Outbound redaction and payload inspection.
   - Cluster retargeting.

`AgentProfile` bundles the first three categories. `AgentRuntime` receives the
result as several unrelated constructor arguments while also owning the fourth.
The system works, but a maintainer cannot inspect one value to answer:

- Why did this model receive this tool surface?
- Which context limit was assumed?
- Which prompt fragments formed the final system message?
- Which choices came from user configuration, provider facts, or fallback
  behavior?

Adding a framework directly would not answer those questions. LangGraph
explicitly provides orchestration rather than prompt or agent-architecture
policy. Wrapping the present responsibilities in graph nodes would preserve the
ambiguity and add a second state model.

## Goals

- Give model facts, policy resolution, prompt composition, and execution one
  clear owner each.
- Preserve current `full` and `small` behavior during the boundary migration.
- Make the effective agent policy immutable, inspectable, and testable.
- Keep the external-AI data boundary and write-safety perimeter mandatory for
  every engine.
- Allow an external framework to be evaluated without coupling the UI,
  Kubernetes tools, or provider plugins to it.
- Use measured parity and net simplification, not framework popularity, for the
  adoption decision.

## Non-goals

- Adopting a framework in the initial refactor.
- Introducing a third profile or per-model prompt forks.
- Automatically classifying models from unverified name patterns.
- Replacing the provider plugin API.
- Moving write approval into a framework.
- Adding cross-process conversation persistence or a checkpoint service.
- Adding multi-agent behavior.
- Changing #307's bounded-operator product contract or initial operations.
- Changing any security invariant.

## Architectural Decision

```text
Config -----------+
                  |
Provider facts ---+--> AgentPolicyResolver --> ResolvedAgentPolicy
                  |                                |
Session facts ----+                                +--> armed tools and budgets
                                                   |
Cluster context ---------------------------------->+--> PromptComposer
Evidence state ----------------------------------->+         |
Screen context ----------------------------------->+         v
                                                   |   composed messages
                                                   |         |
                                                   v         v
UI <---- AgentEvent stream ---- AgentEngine --> RequestGateway --> LLMProvider
                                  |                    |
                                  v                    v
                             ToolExecutor       OutboundPolicy and
                                  |             exact payload snapshot
                                  v
                         existing UI approval,
                         audit, and writes
```

The framework-replaceable portion starts at `AgentEngine`. Policy resolution,
prompt composition, outbound enforcement, tool execution, approval, and audit
remain korvid-owned.

### Layer placement

All new contracts live in `korvid.agent`, following the existing import rules.

```text
agent/model_capabilities.py  model facts and provenance
agent/policy.py              policy inputs, resolver, resolved policy
agent/prompting.py           prompt recipes and deterministic composition
agent/engine.py              engine ABC and shared engine-facing types
agent/runtime.py             reference engine implementation
```

- `core/config.py` continues to parse configuration into plain values. It does
  not import agent, provider, or tool policy.
- `providers/` may expose capability facts through agent-layer contracts because
  providers already depend on `agent`.
- `tools/registry.py` remains the source of tool metadata and schemas.
- `ui/` depends on `AgentEngine`, never a framework class.
- `__main__.py` remains the composition root and wires the resolver, composer,
  gateway, and engine.

No framework-named module is added until a candidate is adopted.

## Components

### 1. Model identity and capabilities

`ModelDescriptor` identifies the selected endpoint without making policy
decisions:

```python
@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    provider: str
    model: str
```

`ModelCapabilities` records facts that may affect policy:

```python
@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    context_window_tokens: int | None = None
    supports_tools: bool | None = None
    supports_parallel_tools: bool | None = None
    supports_reasoning: bool | None = None
    provenance: Mapping[str, CapabilitySource] = field(default_factory=dict)
```

An absent value means unknown. Unknown is not false.

`CapabilitySource` distinguishes:

- explicit user input;
- provider metadata;
- a versioned eval-backed catalog;
- conservative fallback.

Provenance is per field because a provider may report a context window but say
nothing about parallel tool calls. Model-name parsing is not a capability
source.

The constructor defensively copies and freezes the provenance mapping, following
the existing `AgentSettings` options pattern. A frozen dataclass containing a
caller-owned mutable mapping would not satisfy the boundary.

The first behavior-preserving change may populate every field as unknown. This
type exists before automatic routing so later metadata cannot leak directly
into UI or runtime conditionals.

### 2. Policy inputs and resolution

`AgentPolicyInputs` contains all facts needed to resolve behavior:

- configured preset (`full`, `small`, or unset);
- model descriptor and capabilities;
- read-only mode;
- cluster capabilities such as Pod resize;
- configured observability backends;
- validated prompt and tool-description overrides.

`AgentPolicyResolver` is pure and has no file, network, provider, UI, or runtime
dependencies.

Resolution precedence is:

1. explicit user policy;
2. trustworthy provider facts;
3. a versioned, eval-backed catalog;
4. conservative fallback.

During the compatibility phase:

- explicit `full` and `small` map exactly to their current behavior;
- an unset startup value retains the current effective default;
- the setup wizard may retain the existing Ollama suggestion;
- no existing configuration is silently rewritten.

Replacing the Ollama heuristic is a later behavior change. It requires provider
facts or measured catalog data and separate user-visible migration notes.

### 3. Resolved agent policy

`ResolvedAgentPolicy` is the complete immutable decision handed to an engine:

```python
@dataclass(frozen=True, slots=True)
class ResolvedAgentPolicy:
    preset: str
    model: ModelDescriptor
    capabilities: ModelCapabilities
    prompt: PromptRecipe
    tools: tuple[ToolSchema, ...]
    max_iterations: int
    max_history_chars: int
    max_result_chars: int | None
    max_tool_calls_per_iteration: int | None
    strict_history_budget: bool
```

The actual implementation should reuse existing schema aliases rather than add
an incompatible public type. The dataclass is a frozen policy envelope; its
schema mappings are privately owned and copied at construction and again at the
provider boundary, so callers cannot mutate live policy through a nested dict.

The resolved object is replaced atomically on provider/model/profile rebuild.
A runtime does not mutate its individual fields.

Cluster retargeting creates a new resolved policy from the same preset and model
facts plus the new cluster facts. The retarget transaction replaces tools,
prompt base, request-budget overhead, and cluster context together.

### 4. Prompt recipe and composer

`PromptRecipe` describes wording choices without inserting runtime facts:

- base role/system instruction;
- optional appended user rules;
- UI instruction variant;
- tool-description overrides.

`PromptComposer` owns all korvid-authored message framing:

```python
class PromptComposer:
    def compose_system(
        self,
        policy: ResolvedAgentPolicy,
        *,
        cluster_context: str | None,
        evidence: Sequence[Evidence],
    ) -> str: ...

    def compose_user(
        self,
        *,
        user_text: str,
        screen_context: ScreenContext,
        context_switch_note: str | None,
    ) -> str: ...
```

The composer deterministically assembles:

- the role and appended rules;
- detected environment context;
- UI instruction only when UI tools are armed;
- write or no-write instruction from the armed surface;
- dynamic evidence instructions and table;
- bounded, sanitized screen-context framing.

The evidence ledger remains responsible for minting and validating references.
It supplies trusted evidence records to the composer; it does not format the
system message.

`AgentUiController` supplies typed screen facts and an optional context-switch
notice. It no longer writes model-facing delimiter text.

This design centralizes composition, not every English string. A tool description
continues to be owned by the tool registry because it documents that tool. The
composer owns how tool descriptions and prompt fragments become one request.

### 5. Request gateway

`RequestGateway` is the mandatory engine-to-provider seam. It:

1. asks the provider to adapt a private message copy to its wire dialect;
2. runs `OutboundPolicy`;
3. records the exact sanitized `OutboundSnapshot`;
4. invokes the provider only with the prepared messages and tools;
5. exposes streaming provider events to the engine.

No engine or framework adapter receives a raw provider that it can call
directly. This makes outbound enforcement structural rather than conventional.

The current `LLMProvider` contract remains unchanged in the first phase. The
gateway extracts existing request-preparation logic from the runtime without
moving sanitization into provider adapters.

### 6. Agent engine

The engine boundary models current application behavior, not framework APIs:

```python
class AgentEngine(ABC):
    @abstractmethod
    def run_turn(
        self,
        user_text: str,
        screen_context: ScreenContext,
    ) -> AsyncIterator[AgentEvent]: ...

    @abstractmethod
    def finalize_interrupt(self) -> TurnInterrupted: ...

    @abstractmethod
    def retarget(
        self,
        policy: ResolvedAgentPolicy,
        *,
        cluster_context: str | None,
    ) -> None: ...
```

It also exposes the existing read-only state needed by the UI:

- total and estimated token usage;
- latest outbound snapshot;
- current evidence ledger or an equivalent navigation interface.

The exact interface will be kept as small as the current `AgentUiController`
usage permits. It will be an `abc.ABC`, consistent with repository boundary
rules.

The existing runtime becomes the reference implementation. Initially it may
implement the ABC without being renamed. Internal extraction follows in
behavior-preserving slices:

- conversation and evidence state;
- request preparation through `RequestGateway`;
- model/tool loop;
- turn lifecycle, cancellation repair, and usage.

Extraction stops when a unit has no independent responsibility. The goal is not
one class per method.

## Runtime Data Flow

### Startup or agent rebuild

1. `core/config.py` supplies parsed preset and override values.
2. The selected provider supplies a `ModelDescriptor` and any trustworthy
   capabilities.
3. The composition root builds `AgentPolicyInputs`.
4. `AgentPolicyResolver` returns `ResolvedAgentPolicy`.
5. `PromptComposer`, `RequestGateway`, `ToolExecutor`, and the reference engine
   are constructed.
6. The UI receives only `AgentEngine` and display metadata derived from the
   resolved policy.

The provider, policy, executor, and engine are built before the live references
are swapped. Existing transactional rebuild behavior remains.

### Turn

1. The UI submits user text plus typed screen context.
2. The engine starts a fresh evidence turn.
3. `PromptComposer` builds the current system and user messages.
4. The engine asks `RequestGateway` to prepare and send the request.
5. The gateway enforces outbound safety and streams provider events.
6. The engine emits existing typed text/tool events.
7. Tool calls use the existing `ToolExecutor` and `UIBridge`.
8. Writes still pause inside the existing UI approval path.
9. Tool results return to the engine and the loop continues within the resolved
   policy limits.
10. The evidence ledger validates final citations and the engine emits
    `TurnComplete`.

### Context switch

1. Cluster discovery produces new session facts.
2. The resolver creates a new policy from the same selected model and preset.
3. The coordinator verifies that no turn or write is active.
4. The engine atomically adopts the new policy and cluster context.
5. Old evidence is cleared before another turn can start.
6. Conversation history remains, matching current behavior, while the system
   message and armed tools describe only the new cluster.

## Error Handling

### Capability and policy errors

- Invalid explicit configuration remains a visible configuration warning or
  error according to current config semantics.
- Unknown capability data is represented explicitly and resolved
  conservatively; it is not silently converted to a success-shaped default.
- Contradictory provider metadata produces a bounded diagnostic and does not
  mutate the live engine.
- A failed rebuild closes only the new provider and leaves the previous engine,
  provider, policy, and UI metadata live.

### Request and provider errors

- `OutboundPolicyError` remains fail-closed and is mapped to the existing
  `AgentError` plus completed-turn semantics.
- Provider preparation or streaming errors preserve token and interruption
  accounting already proven by runtime tests.
- No broad framework exception is converted into a successful turn.

### Tool and write errors

- Framework adapters may only call the existing executor boundary.
- Write tools continue to require the existing UI confirmation, context/UID
  revalidation, and fail-closed audit.
- A framework interrupt or retry mechanism is not used for writes in the first
  candidate spike.
- If a future engine can replay a node or tool call, write dispatch must carry a
  korvid-owned idempotency identity and prove exactly-once admission before that
  engine can be considered.

### Cancellation

- UI cancellation calls the engine contract and receives the current typed
  interruption result.
- The engine must repair history so no unmatched tool call/result remains.
- A framework backend must demonstrate the same behavior under cancellation at
  every await boundary used by the reference-engine contract tests.

## Framework Evaluation

### Why Pydantic AI first

The current topology is one model alternating with one tool executor. Pydantic
AI directly targets typed tool agents and provides:

- typed dependencies and outputs;
- streaming run events;
- composable capabilities;
- deferred tool execution and approval concepts;
- a model abstraction that can be adapted.

The spike must still use korvid's `RequestGateway`, tool executor, events, and
write perimeter. Built-in provider integrations are not a reason to bypass the
plugin contract.

### When LangGraph becomes appropriate

LangGraph should be evaluated when at least one product requirement needs its
graph runtime:

- durable pause/resume across process restarts;
- branching operation workflows with explicit state transitions;
- parallel branches whose state must be joined;
- multiple agents or actors with distinct responsibilities;
- persisted, inspectable long-running jobs.

#307's phase-specific small-operator surface does not alone require LangGraph.
It can be expressed by policy resolution and operation state owned by the
bounded operator. A two-node model/tool cycle would add graph terminology
without removing korvid policy.

LangGraph interrupts restart the interrupted node when resumed. No side effect
may precede an interrupt unless replay is proven safe. This is stricter than the
current in-process write flow and must be tested explicitly if LangGraph is
ever spiked.

### Adoption decision

The framework comparison records:

- custom production lines and responsibilities removed;
- adapter and retained safety code added;
- direct and transitive dependencies;
- import and startup impact;
- engine-contract and eval parity;
- cancellation, retarget, and approval semantics;
- debugging and payload-inspection quality;
- provider-plugin compatibility.

Adopt a framework only if it removes ownership of generic loop mechanics while
leaving korvid policy in one layer. Reject it if most current runtime behavior
survives inside framework nodes, hooks, middleware, or duplicate message types.

Rejection is a successful outcome: the refactored reference engine remains the
production implementation.

## Testing Strategy

### Characterization tests

Before moving behavior, pin:

- exact full/small effective budgets and armed tool names;
- prompt composition with read-only, write, UI, cloud-context, override, and
  evidence combinations;
- screen-context and context-switch framing;
- model switch and profile preservation;
- transactional rebuild and retarget behavior.

Avoid broad prompt snapshots that obscure intent. Assert independently owned
fragments and a small number of representative final compositions.

### Policy tests

Table-driven tests cover:

- explicit preset precedence;
- unknown versus known capabilities;
- read-only and cluster capability gates;
- observability backends;
- prompt and tool-description overrides;
- conservative fallback;
- invalid or contradictory inputs.

The resolver is pure, so these tests require no provider, Kubernetes fake, or
Textual pilot.

### Engine contract tests

One shared suite runs against every engine candidate:

- text-only completion;
- one and multiple tool iterations;
- malformed and excess tool calls;
- history and request budgets;
- usage reported and estimated;
- provider failure before and after transmission;
- cancellation at each await boundary;
- context retarget and evidence invalidation;
- exact outbound snapshot;
- citation completion data.

Framework-specific tests may be added, but they cannot replace the shared
contract.

### Security tests

Retain and extend tests proving:

- prompt, screen, tool-result, and schema content is sanitized;
- Secret data stays masked;
- exact payload inspection matches provider handoff;
- no write bypasses confirmation and audit;
- denial, expiry, cancellation, UID replacement, and context switch never
  mutate;
- replay cannot execute a write twice.

### Integration and eval tests

- Existing deterministic scenario and journey suites remain the quality
  baseline.
- #307 operation journeys exercise the same engine contract.
- Optional-extra import tests prove base, MCP-only, and observability-only
  installs do not import a framework.
- A candidate framework runs the same scripted eval inputs and produces a
  comparison report before any default changes.

## Migration Slices

Each slice is independently releasable and keeps the current engine as default.

1. **Prompt composition characterization**
   - Pin current prompt and context behavior.
   - Introduce `PromptRecipe` and `PromptComposer`.
   - Remove model-facing string assembly from runtime and UI.

2. **Resolved policy**
   - Introduce model/capability and policy types.
   - Make `build_profile` a compatibility adapter to the resolver.
   - Pass one resolved policy into runtime construction and retargeting.

3. **Engine boundary**
   - Introduce `AgentEngine`.
   - Change UI and composition-root annotations to the boundary.
   - Keep `AgentRuntime` as the reference implementation.

4. **Request gateway and runtime decomposition**
   - Extract the mandatory provider handoff.
   - Separate conversation/evidence state and turn lifecycle where contract
     tests justify the boundary.

5. **#307 integration**
   - Express phase-specific small-operator surfaces through policy inputs rather
     than runtime branching.
   - Preserve the same tool executor and write perimeter.

6. **Framework spike**
   - Implement a non-default candidate in an isolated branch.
   - Run shared engine contracts and evals.
   - Record an adopt/reject decision before adding a production dependency.

## Documentation

Update:

- `docs/agent.md` with effective-policy resolution and user-visible behavior;
- architecture documentation with the policy, engine, and outbound boundaries;
- provider plugin documentation only if optional capability reporting becomes
  part of a later plugin API version;
- eval methodology with the framework parity process.

The setup UI should eventually show the effective policy source without
presenting raw implementation details. That UI change is outside the initial
boundary refactor.

## Success Criteria

- A maintainer can inspect one resolved value to explain prompt, tools, and
  budgets for the active model and cluster.
- Runtime and UI no longer assemble model-facing prompt fragments.
- The UI depends on `AgentEngine`, not `AgentRuntime` or a framework.
- Every engine is structurally forced through the same outbound and write
  boundaries.
- Current full/small behavior and security tests remain green.
- #307 can add phase-specific policy without new runtime/profile conditionals.
- A framework receives an evidence-based adopt/reject decision.
- If no framework is adopted, the reference engine is still materially easier
  to understand because policy and composition no longer live inside it.
