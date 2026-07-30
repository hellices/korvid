# korvid — AI-Native Kubernetes TUI Design Document

- **Date**: 2026-07-23
- **Status**: Draft (awaiting review)
- **Project name**: `korvid` (corvid = the crow family, tool-using birds — naming research confirmed no conflicts on GitHub/PyPI; finalized 2026-07-23)

---

## 1. Vision

**A Kubernetes diagnostic/operations cockpit where keyboard-first cluster control and "a Claude Code-grade conversational AI agent" coexist on a single screen.**

- Day-to-day: a fast, keyboard-first resource navigation/operation TUI, like established terminal Kubernetes tools
- With the agent enabled: the agent investigates the cluster and **drives the TUI directly** (switching views, applying filters, opening logs) while showing its evidence as it diagnoses. Command execution goes through an approval gate
- The user can take over with the keyboard at any moment — the TUI experience is never lost

### Why now (research findings, as of 2026-07)
- No mature tool yet combines "full-screen TUI + conversational LLM + command execution" (the only attempt, ks-ai, remains a 7★ early-stage PoC)
- The existing TUI ecosystem has no AI-integration roadmap (community native-integration attempts did not reach merge) — this combination is naturally achievable when starting from a fresh design
- The Python/Textual stack is proven in production-grade TUIs (posting 12k★, elia 2.5k★, parllama active)
- The biggest threat is general-purpose agents (Claude Code + K8s MCP) → differentiate through domain specialization: **automatic screen-context injection + TUI driving + approval/audit system**

---

## 2. Target Users / Core Scenarios

**Target**: SREs, platform engineers, and backend developers who operate Kubernetes from the terminal

Core scenarios:
1. **Explore**: `:pods`, `/filter`, log tailing — as fast as or faster than existing terminal tools
2. **Conversational diagnosis**: open the agent panel and ask "why is the checkout service returning 5xx?" → the agent investigates events/logs/specs, bringing relevant views onto the screen as evidence → proposes root cause + fix
3. **Approval-based mutation**: the agent proposes `kubectl rollout restart ...` → diff/command preview → user approves → execution → audit log entry
4. **Screen-context questions**: with the cursor on a CrashLoopBackOff pod, press a shortcut → "why is this pod dying?" is sent with context (full resource spec, recent events, log tail) attached automatically

---

## 3. Approaches Considered

| | A. Sidecar chat | B. Agent-first shell | **C. Dual-mode hybrid (chosen)** |
|---|---|---|---|
| Summary | TUI + read-only Q&A panel | Conversation is the primary UI; widgets are artifacts | Keyboard-first TUI + an agent that can drive the TUI |
| Pros | Simple, low risk | Maximum AI differentiation | Satisfies both needs; structural differentiation |
| Cons | Underserves the requirements; no different from kubectl-ai | Loses the TUI experience; competes head-on with general-purpose agents | Highest complexity |
| Verdict | ❌ | ❌ | ✅ manage complexity with a phased roadmap |

---

## 4. Architecture

### 4.1 Overall structure (single process, asyncio)

```
┌─────────────────────────────────────────────────────────────┐
│                     Textual App (asyncio)                   │
│  ┌────────────────────────────┐  ┌───────────────────────┐  │
│  │        UI Layer            │  │    Agent Panel        │  │
│  │  WorkspaceScreen           │  │  chat / tool-call log │  │
│  │  ├─ Pane(ResourceTable)    │  │  approval dialogs     │  │
│  │  ├─ Pane(LogView)          │  │  streaming markdown   │  │
│  │  └─ StatusBar/CmdPalette   │  └──────────┬────────────┘  │
│  └──────────┬─────────────────┘             │               │
│             │ UI Bus (commands/events)      │               │
│  ┌──────────┴─────────────────┐  ┌──────────┴────────────┐  │
│  │      Core Services         │  │    Agent Runtime      │  │
│  │  WatchManager (selective)  │  │  agentic loop         │  │
│  │  ResourceStore (cache)     │  │  ToolRegistry         │  │
│  │  LogStreamer (multi-pod)   │  │  ├─ k8s read tools    │  │
│  │  ActionExecutor            │  │  ├─ k8s write tools ⚠ │  │
│  │  ContextManager (kubecfg)  │  │  ├─ ui control tools  │  │
│  │  AuditLog                  │  │  └─ shell tool ⚠      │  │
│  └──────────┬─────────────────┘  │  LLM ProviderAdapter  │  │
│             │                    └──────────┬────────────┘  │
│  ┌──────────┴────────────────────────────── ┴────────────┐  │
│  │  kubernetes.aio (async client, watch streams)         │  │
│  │  LLM APIs (OpenAI/Anthropic/Gemini/Azure/Ollama)      │  │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
⚠ = approval gate required
```

**Core design principle — the UI Bus**: user keystrokes and the agent's UI-control tools pass through **the same command bus** (`navigate(view=pods, ns=prod)`, `set_filter(...)`, `open_logs(...)`). This single entry point:
- Implements the agent's "direct TUI handling" naturally (the agent issues the same actions a human would)
- Unifies all state changes into the audit log
- Guarantees testability (inject commands into the bus → assert on state)

### 4.2 Technology stack

| Component | Choice | Rationale |
|---|---|---|
| Language | Python ≥3.11 | asyncio TaskGroup, mature ecosystem |
| TUI | Textual (v8+) | 36.7k★, actively maintained, composable layout (split panes for free), async-native |
| K8s client | kubernetes-client/python v36+ (`kubernetes.aio`) | Official, async watch streams, integrates naturally with Textual's event loop |
| LLM adapter | Thin in-house adapter (OpenAI-compatible + Anthropic + Gemini + Ollama) | litellm is heavy with dependency risk; the tool-use loop must be owned in-house |
| Packaging | uv + `pipx/uvx` install; single binary (PyApp/PEX) considered later | Pursue the "single binary" strength that leading terminal tools demonstrate |
| Configuration | **Single `~/.config/korvid/config.yaml`** | A single file from day one — no config sprawl (§5 #7) |

---

## 5. Unmet Needs in Existing Tools → Design Requirements (Day-1)

We verified, with measured data (issue numbers, 👍 counts), the needs that the community has long asked for in terminal Kubernetes workflows but that remain unmet. The issue trackers of existing terminal Kubernetes tools are the best available data source for this demand — a map of proven demand that korvid, being a fresh design, can address from the start:

| # | Unmet need (demand evidence) | korvid design response | Phase |
|---|---|---|---|
| 1 | Advanced log workflows — merged multi-pod view (#827, 62👍), JSON log parsing (#364, 147👍), reliable streaming (#1228 83👍, #1399) | **Logs as a first-class citizen**: merged multi-pod stream (pod prefixes), auto JSON detection + field extraction **with a formatted↔raw toggle (`f` in the log view — auto-detect never forces a rendering)**, previous-container logs (`p`, `--previous`), explicit buffer-overflow banner, reconnect status indicator, search hit count + n/N navigation | MVP |
| 2 | Simultaneous multiple views (#351, #1430 40👍) — hard to build on a single-view stack architecture | **Split-pane architecture from Day 1**: WorkspaceScreen manages N panes (watch the pod list while reading logs). Built on Textual Containers | MVP (2-pane) |
| 3 | Freely remappable keybindings (#625) | Every action is a named command on the command bus → fully remappable via the `keybindings:` config section | MVP |
| 4 | Stronger guardrails for dangerous operations (#1016, #319) | **Layered confirmation**: normal delete = dialog; cluster-scoped delete = type-the-resource-name confirmation; protected contexts = extra confirmation + red header; `--readonly` mode | MVP |
| 5 | Clear surfacing of RBAC/auth errors (#3730) | Explicit API error parsing: "no pods/exec permission (ns: prod)", token-expiry detection → re-auth guidance, unauthorized actions dimmed | MVP |
| 6 | Minimal API-server load (#3603, 28👍) | **Selective watch**: only watch resources visible on screen, pause when unfocused, exponential backoff, watch bookmarks | MVP |
| 7 | A simple configuration story | Single config.yaml + `contexts.<name>:` override sections; runs with zero config (kubeconfig auto-detection) | MVP |
| 8 | Plugins that can extend the UI (#771, 160👍 — existing plugin models are shell-out only) | Python plugin API: register custom panels/columns/commands/agent tools. Schema-validated manifest | Phase 3 |
| 9 | Automatic Secret base64 decoding (#1017, 42👍) | Auto decode↔encode round-trip by default when viewing/editing Secrets | Phase 2 |
| 10 | Runtime resilience (#2465, 69👍) — highlights the importance of isolating UI and data layers | UI/data task separation + global exception boundary: data-layer exceptions render as error cards inside the affected panel; the app survives | MVP |
| 11 | Reliable metrics sorting (#3793, 24👍) | Data model / rendering separation + unit test coverage for sorting/metrics | Phase 2 |
| 12 | A sustainable maintenance structure — a common challenge for small-maintainer projects | Minimal core + extension via plugins; CI/test automation to lower the contribution barrier | Operating policy |
| 13 | Keybinding discoverability | Context-aware help + fuzzy command palette (search by action name → shows the key) + **ask the agent** | Phase 2 |
| 14 | Broad terminal compatibility (#3598) | Use Textual's theme/color system (safer than managing colors in-house) | Free win |
| 15 | Deeper Helm workflows (#1841, 28👍) | Non-goal (MVP) — via plugins in Phase 3+ | Phase 3+ |

**Proven terminal-TUI UX conventions to inherit**: the `:` command bar + vim-style navigation, instant ctx/ns switching, fast startup, `/` filtering (regex/fuzzy/label), one-key shell-in (`s`, `kubectl exec -it` via PTY suspend), describe view (`d`, `kubectl describe`-equivalent rendered from the object + events), previous logs (`p`), port-forward management, read-only mode. We follow these conventions as-is to minimize switching cost for users — **`d` stays describe; the debug dialog uses `D` (all bindings remappable, §5 #3)**.

**Universal resource browsing (Day-1)**: any Kubernetes object kind — built-ins and CRDs — is reachable from the `:` command bar via API discovery (`:pods`, `:deploy`, `:crd`, `:<any-kind-or-shortname>`). Views are generic over a resource descriptor (columns + kind metadata), so new kinds cost data-mapping only, not new UI. **Namespace scope is first-class**: a view can target one namespace or all namespaces (`:pods all`, the `0` convention), and `/` filtering works identically in both scopes; all-namespace views add a NAMESPACE column.

---

## 6. Agent Design (the differentiating core)

### 6.1 UX — "feels like Claude Code, keeps the classic TUI experience"

- `Ctrl-A` (tentative) toggles the agent panel — slides in as a right-side 30–40% panel. The rest of the screen remains a live TUI
- **The panel is collapsible at any time** (same `Ctrl-A` toggle). Collapsing reclaims the full screen width for the resource views; the agent session — conversation history, in-flight tool calls, pending state — persists in the background
- **Collapsed-state indicator**: while collapsed, a compact status badge lives in the status bar (e.g., `⚡AI ● working` / `⚡AI ✓ done` / `⚡AI ⏸ approval pending`). New agent output or a pending approval surfaces as a badge change (never a modal steal-focus), and re-expanding restores the full conversation exactly where it was. Approval dialogs are never auto-opened from the collapsed state — the badge invites the user to expand first
- Panel contents: streaming markdown responses + a **tool-call log** (collapsible entries like Claude Code: "🔧 get_pod_logs(checkout-7d9f…) ✓") + an input box
- **Token/cost visibility** (patterns validated against Claude Code, Gemini CLI, Aider, Cline as of 2026-07): the panel header shows an always-on context gauge — `34.2k / 200k tok · ~$0.11` with color thresholds (default: amber at 50%, red at 90% of the session budget, Gemini CLI-style). Each assistant turn carries a compact annotation (`↑1.2k ↓450`, Aider-style, expandable for cache stats later). While collapsed, the status-bar badge includes an abbreviated count (`⚡AI ● 34k`)
- **Two-tier context injection** (keeps input tokens bounded): the always-injected system context is *lightweight only* — active view, selected resource kind/name/status, filters, ns/ctx (hundreds of tokens). Full resource specs, log tails, and events are **not** auto-attached; the agent fetches them on demand via read tools (which are ingest-capped, §6.2). Exception: the "what's wrong with this?" shortcut on a selected resource pre-attaches rich context (spec + recent events + log tail, each trimmed) for that one turn
- **Agent-drive mode**: when the agent manipulates the screen via UI-control tools, the affected panel is visually marked (border highlight + an "agent" badge). Any user keystroke immediately takes priority

### 6.2 Agentic loop & tools

An in-house tool-use loop (max iterations default 15, configurable):

| Tool group | Examples | Gate |
|---|---|---|
| k8s read | `list_resources`, `get_resource`, `get_logs`, `get_events`, `diagnose_pod`, `top_pods`, `explain_rbac` | None (RBAC applies) |
| k8s write | `apply`, `delete`, `scale`, `rollout_restart`, `cordon` | **Approval required** — command/diff preview dialog |
| debug | `launch_debug_session` (configures image/target/profile), `suggest_debug_commands` | **Approval required** (pod spec mutation) |
| UI control | `navigate`, `set_filter`, `open_logs`, `split_pane`, `highlight_resource` | None (screen-only changes, visually marked) |
| shell | `run_kubectl(args)` (allowlist-validated) | Triple validation (verb × resource × flags); write verbs require approval |

- Combines a **read-only-by-default** philosophy (HolmesGPT style) with **approval-based execution** (kubectl-ai style)
- **Shell tool validation is not verb-only**: `run_kubectl` validates the (verb × resource × flags) triple. Read verbs can still be dangerous — e.g., `get secrets -o yaml` (leaks secret payloads into LLM context), `proxy`/`port-forward` (long-running, network exposure), `--raw`, or flag smuggling into exec-like paths. Reads of sensitive resources (Secrets, ServiceAccount tokens) are forced through the masking pipeline; long-running and raw-API verbs are rejected outright
- Approval dialog: shows the exact command to run + target resource + dry-run diff (when available); Y/n/edit. **Approval dialogs can only be confirmed by a user keystroke — no agent tool can open, focus, or confirm an approval dialog** (see UI-control hardening below)
- **Audit log**: every write execution (user- or agent-initiated) is recorded in `~/.local/state/korvid/audit.jsonl` (who/when/command/approved-by). The file is created with `0600` permissions and rotated by size (default 50 MB, configurable retention). Auditing is **fail-closed**: if the audit entry cannot be written, the write action is blocked
- **UI-control hardening**: UI-control tools are ungated (screen-only), but cluster-sourced content (labels, annotations, log lines) is a prompt-injection vector that could steer the agent into misleading screen manipulation. Therefore every UI-control action is also recorded in the tool-call log (§6.1), agent-driven panels are visually marked, and — as above — approval confirmation is reserved exclusively for user input
- Privacy: an anonymize option for data sent to the LLM (k8sgpt style); Secret values masked by default
- **Token/cost budget**: the iteration cap alone does not bound spend — each iteration re-sends the conversation history, so early large attachments are re-billed every turn. A per-session token budget (`agent.max_tokens_per_session`, default 200k) tracks cumulative input+output tokens; on breach the loop pauses and asks the user whether to continue (showing tokens consumed so far). Consumption is surfaced continuously in the panel-header gauge (§6.1). **Counting method**: provider `usage` fields from API responses are authoritative when present; for streaming responses that omit usage, a local estimate (chars/4 heuristic, or the provider's tokenizer when available) fills in and is reconciled against the final usage report when it arrives — the gauge marks estimated values with `~`
- **Input-token growth control** (three tiers, cheapest first — mirrors the Gemini CLI/Cline/Claude Code split):
  1. **Ingest caps (free)**: every tool result is truncated at ingest (default ~2k tokens per result; log tails and event lists trimmed to configurable line/entry caps). Full outputs are kept outside the conversation (viewable in the TUI) and referenced by ID, so the agent can re-query a narrower slice instead of carrying the full dump
  2. **Tool-result compaction (free)**: as history grows, older tool results are the first thing replaced — with a one-line digest + reference (newest results are kept verbatim within a rolling budget)
  3. **Conversation summarization (costs one LLM call)**: at a threshold of the session budget (default 70%) or via a manual `/compact [focus…]` command, the older portion of the conversation is summarized into a structured snapshot, preserving the recent turns verbatim. Thrashing protection: if the context refills immediately after summarization, the loop stops and asks the user rather than looping
- **Prompt caching**: the adapter interface carries provider cache markers (e.g., Anthropic `cache_control` on the system prompt and stable history prefix) so providers that support prefix caching cut re-billed input cost; surfacing cache-hit stats in the UI is deferred (Phase 2)

### 6.3 LLM providers & activation model

- Adapter interface: `complete(messages, tools, stream=True)` — **no provider is bundled or defaulted; every provider is a pluggable adapter activated purely by config injection**
- Two adapter families:
  - **API-key adapters**: OpenAI-compatible (OpenAI/Azure/Ollama/vLLM), Anthropic, Gemini — keys via config or env
  - **Subscription-backed adapters**: Claude Code (reuses the `claude` CLI / Agent SDK credentials) and GitHub Copilot (reuses `gh` auth) — lets users ride existing subscriptions without managing raw API keys
- With no provider configured, **the agent mode and all agent-dependent features are disabled cleanly** (no dead buttons: agent keybindings, panel, and agent-only commands are hidden or show the setup hint) and the TUI is fully functional (**it must be a useful tool even without an LLM**)
- **Install-time recommendation**: first run without a provider shows a one-time onboarding hint strongly recommending provider setup (with copy-paste config examples per provider). Dismissible and never blocking

**Activation model — "provide a provider and it merges in" (config-detected auto-activation + explicit safety switches)**:

1. **Single package**: the agent runtime always ships included (adapters are a thin in-house HTTP implementation, so a separate optional extra offers little benefit)
2. **Auto-merge**: when a provider is detected in config/env, the agent activates — the `Ctrl-A` panel comes alive and the status bar shows the model name. Adding one config file is all it takes; no separate install or flag
3. **Guidance when unset**: with no provider, pressing `Ctrl-A` shows "configure a provider to activate the agent" plus a config example in the panel (making the feature discoverable)
4. **Explicit off switch**: even with a provider present, `agent.enabled: false` disables the agent globally. Per-context overrides (`contexts.<name>.agent.enabled`) can turn it off for specific clusters only
5. **Protected-context integration**: in clusters designated as protected contexts, agent write tools can never bypass the approval gate (§6.2), and an option to auto-disable the agent entirely (`agent.disable_in_protected: true`) is available — structurally preventing "accidentally sending cluster data to an LLM" in regulated environments

### 6.4 Live debugging — `kubectl debug` integration (not previously available in TUIs)

Non-disruptive debugging of running pods is a first-class feature. All three kubectl debug modes are covered, introduced in stages.

| Mode | Purpose | UX | Phase |
|---|---|---|---|
| **Ephemeral container** | Non-disruptive debugging of running pods. Essential for distroless/minimal images with no shell | `D` in the pod view → debug dialog → attach (`d` stays describe) | **MVP** |
| Copy-of-pod (`--copy-to`) | Experiments without touching the original (swap command/image) | Mode selection in the dialog. On session end, confirm cleanup of the copied pod | Phase 2 |
| Node debug | Node-level diagnosis (host namespaces) | `D` in the node view | Phase 2 |

**Debug dialog** — solves kubectl debug's complex flag combinations with a form UI:
- Debug image: presets (busybox, nicolaka/netshoot, ubuntu) + custom (register internal-registry images in the config's `debug.images`)
- `--target` container selection (process namespace sharing target)
- Profiles: `general` (default) / `netadmin` / `sysadmin` / `restricted` — the permission differences of each profile are explained in the dialog

**Terminal attach**: MVP suspends the TUI → runs `kubectl debug -it` on a PTY → returns to the TUI on exit (the same proven shell-in pattern). An embedded terminal inside a split pane is considered in Phase 3.

**Safety design**:
- Injecting an ephemeral container mutates the pod spec (a write), so it goes through the **approval dialog + audit log**
- ⚠️ **An injected ephemeral container cannot be removed until the pod restarts** — this caveat is stated in the approval dialog, and pods with an active debug container get a badge in list views
- RBAC pre-check: without `pods/ephemeralcontainers` update permission, show "missing permission: pods/ephemeralcontainers" explicitly (the §5 #5 principle)
- Cluster version check: EphemeralContainers is stable in K8s 1.25+ — on older versions the feature is disabled with the reason shown

**🌟 Agent integration (the differentiating killer workflow)**:
- `launch_debug_session` tool: the agent decides and proposes a debug configuration that fits the diagnostic context — "why is this pod's network broken?" → composes netshoot image + `--target app` + netadmin profile → user approves → shell opens
- `suggest_debug_commands` tool: proposes a sequence of diagnostic commands to run after entry (e.g., `nslookup svc`, `ss -tlnp`, `tcpdump -i eth0`)
- Scenario completeness: "symptom question → agent investigation → auto-configured debug session → verification in the shell" flows as one continuous experience. This connects what previously lived in separate tools: conversational diagnosis (REPL tools) and manual shell-in (TUIs)

### 6.5 Extended diagnostics — vanilla K8s API only (researched 2026-07-23)

Five diagnostic features deliverable at the level of the **vanilla Kubernetes API + metrics-server (a de facto standard)**, without external ecosystem tooling (Helm/GitOps/security scanners/cost/multi-cluster). Each was selected because demand is proven by popular krew plugins, the capability has not been available in existing TUIs, and agent synergy is high.

| # | Feature | Demand evidence | APIs used | Phase |
|---|---|---|---|---|
| 1 | **Event intelligence** | Existing TUI event views are plain lists; events are the #1 troubleshooting source | core v1 Events (`fieldSelector`, watch) | **MVP** |
| 2 | **Ownership tree** | kubectl-tree 3.4k⭐ + kube-lineage 0.5k⭐ | recursive `ownerReferences` traversal | **MVP** |
| 3 | **RBAC analysis** | rakkess 1.4k⭐ + who-can 0.9k⭐ + rbac-lookup 1.0k⭐ | `SubjectAccessReview`, `SelfSubjectRulesReview`, Roles/Bindings | Phase 2 |
| 4 | **Usage vs requests/limits** | kube-capacity 2.7k⭐ | `metrics.k8s.io/v1beta1` + Pod spec resources | Phase 2 |
| 5 | **PDB/Quota awareness + drain simulation** | A tooling gap; an essential SRE task | `policy/v1` PDB, ResourceQuota, LimitRange | Phase 2 |

**Design notes:**

- **Event intelligence**: object-scoped event view (only the selected resource's events via `fieldSelector`), a Warning timeline, repeat-pattern badges (count-based). Interpreting the unstructured `.message` field is **where the LLM is strongest** — "summarize the last 30 minutes of Warnings" delivers immediate value. Combined with the ownership tree, incident timelines can be assembled automatically
- **Ownership tree**: MVP is forward-only (Deployment→RS→Pod, straight from the ownerReferences field). Reverse indexing (full resource scan) lands in Phase 2. Acts as the **agent's context supplier** — during diagnosis the selected resource's ownership chain is automatically included in system context, improving accuracy
- **RBAC analysis**: start with "my permissions" (SelfSubjectRulesReview, one call). Before any agent write tool runs, `SubjectAccessReview` performs a **permission pre-check at the approval gate** — on failure, "missing permission: {verb} {resource}" is surfaced at the approval dialog stage (same principle as §5 #5; a generalization of §6.4's RBAC pre-check)
- **Usage vs requests/limits**: three columns (usage/requests/limits) + percentage bars in pod/node views. Overprovisioning detection (actual usage ≪ requests). Without metrics-server, only the usage column is disabled with the reason shown (graceful degradation)
- **PDB/drain simulation**: before draining from the node view, show "pods on this node ↔ PDB matching → disruptionsAllowed check" results in the approval dialog. **The most concrete application of the approval gate.** ResourceQuota remaining-capacity gauges appear in the ns view

**Additional agent tools** (extending the §6.2 table):

| Tool | Group | Gate |
|---|---|---|
| `get_owner_chain`, `get_object_events`, `summarize_events` | k8s read | None |
| `check_access` (SubjectAccessReview), `list_my_permissions` | k8s read | None |
| `analyze_resource_usage` (metrics + spec comparison) | k8s read | None |
| `simulate_drain` (PDB violation pre-check) | k8s read | None (executing the drain itself is write-gated) |

**Considered and excluded**: rollout management (already well served by existing tools; little room for differentiation), an inline TUI YAML editor (high difficulty — dry-run diff is already part of the approval dialog), NetworkPolicy simulation (the vanilla API only shows "allow rules", not actual flows; enforcement differs by CNI), multi-session port-forward management (asyncio+SPDY+Textual technical risk, low LLM synergy — single port-forward stays in Phase 2).

---

## 7. Data Layer

- **WatchManager**: creates/shares/releases watch tasks per (group, version, kind, ns) subscribed by views. Resources not on screen are not watched (§5 #6). Reconnection uses exponential backoff + resourceVersion bookmarks
- **ResourceStore**: an in-memory cache reflecting watch events. The UI subscribes only to the store's reactive snapshots (data↔rendering separation, §5 #10/#11)
- **LogStreamer**: one stream task per pod, a multi-pod merge queue, a ring buffer (default 50k lines, explicit banner on overflow), reconnect status events
- **Error isolation**: data-task exceptions are converted into structured error events → rendered as error cards in the affected panel. App crashes are not acceptable

## 8. Error Handling Principles

1. K8s API errors are parsed (status code + reason) → translated into user language (401→"authentication expired", 403→"missing permission: {verb} {resource}")
2. Agent tool failures return as error results into the loop (the agent may try alternative routes)
3. LLM API outages are shown inside the panel; the TUI itself is unaffected
4. Global exception boundary: even a last-resort exception saves a crash report file and exits gracefully

## 9. Testing Strategy

- **Core Services**: pytest + pytest-asyncio; unit-test WatchManager/LogStreamer/error mapping against a fake K8s API (respx/recorded fixtures)
- **UI**: Textual's `Pilot` test runner to verify keystroke→screen-state transitions (the command bus makes snapshot testing easy)
- **Agent**: mock the LLM (fixed tool-call sequences) to verify the loop/approval gate/audit log. Tests guaranteeing the approval gate cannot be bypassed are mandatory
- **E2E (lower priority)**: smoke tests against a kind cluster

## 10. Roadmap

| Phase | Scope | Definition of done |
|---|---|---|
| **Phase 1 — MVP** | **universal resource views (any kind incl. CRDs via API discovery) with per-ns and all-namespaces scope**, events/ns/ctx views, `:` palette, `/` filter, 2-pane split, first-class log viewer (multi-pod/JSON formatted↔raw toggle/previous logs/reconnect), **describe view (`d`)**, **shell-in (`s`, exec -it)**, layered guardrails, RBAC error mapping, selective watch, single config, keybinding overrides, **agent panel (read tools + UI control + approval-based kubectl writes)**, **live debugging (ephemeral containers + agent debug tools, §6.4)**, **event intelligence + ownership tree (forward, §6.5)**, audit log | Daily diagnostic workflows fully served by korvid alone |
| **Phase 2** | port-forward, copy-of-pod/node debug, Secret decode editing, metrics (top) sorting, **RBAC analysis + usage vs req/limits + PDB/drain simulation (§6.5)**, reverse ownership-tree indexing, command palette discoverability, session state restore, anonymize | Covers everyday cluster operations end to end, standalone |
| **Phase 3** | Python plugin API (register panels/columns/agent tools), external MCP server connections (agent tool extension), simultaneous multi-cluster views, embedded debug terminal panel, diagnostic playbooks, (evaluation) Cilium/Hubble flow view | Ecosystem expansion begins |
| **Non-goals** | Web UI, in-cluster resident agents (kagent's domain), Helm management (initially), optimization for 1000+ node mega-clusters | — |

## 11. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| General-purpose agents (Claude Code + MCP) erode the niche | Screen-context injection, TUI driving, and the approval/audit system are things general-purpose tools cannot do. In Phase 3, become an MCP client ourselves and absorb the ecosystem |
| Python performance (large-scale watch) | Selective watch + async architecture. Mega-clusters are an explicit non-goal |
| Textual bus factor = 1 | MIT license, large community, fork as a last resort. Manage UI-framework coupling behind an abstraction layer |
| kubectl-ai adds a TUI | Speed of execution. Google is focused on REPL/web |
| Agent misbehavior damaging a cluster | Writes are approval-gated by default. `--skip-approvals` can relax this, but **protected contexts ignore that option and always require approval**. Audit log, read-only mode |

## 12. Open Questions (owner decisions needed)

1. ~~**Product name**~~ → **`korvid` finalized** (2026-07-23; naming research confirmed the GitHub K8s niche and PyPI are conflict-free. corvid = crow family, tool-using birds → a metaphor for agentic tool use)
2. ~~Recommended default LLM provider~~ → **No default — strictly pluggable** (2026-07-24). Config injection activates a provider (Claude Code, GitHub Copilot, OpenAI, Anthropic, Gemini, OpenAI-compatible local); without one, agent mode and dependent features are cleanly disabled. Install/first-run strongly recommends configuring a provider (§6.3)
3. ~~Distribution channels~~ → **PyPI first (install via `uv tool install korvid` / pipx), Homebrew tap in Phase 2** (2026-07-24). krew is out of scope (korvid is a standalone app, not a kubectl subcommand); single-binary packaging (PyInstaller/PyApp) stays a Phase 3 evaluation item
4. ~~License~~ → **Apache-2.0** (2026-07-24). Open source. Matches the ecosystem standard (kubectl-ai, k8sgpt, and similar tools are all Apache-2.0) and avoids corporate AGPL blanket bans that would hurt adoption; a local TUI has little SaaS-wrapping exposure for AGPL to defend against
5. **(Evaluation only) Cilium/Hubble network flow view** — technical feasibility confirmed: Hubble Relay gRPC (`GetFlows` streaming; insecure connection possible after port-forward), Python stubs can be generated from the protos, and the Hubble UI service map is itself built by aggregating flows, so the same approach is available. No existing tool renders a network topology in a TUI (a differentiation opportunity). However, given the fallback story for clusters without Hubble, TLS, and proto-maintenance risks, this stays an evaluation item for Phase 3 only (first candidate for the plugin API)

## 13. Cost Analysis: Extending an Existing Tool vs Building New

We compared three paths to realizing this scope from a cost perspective.

### Cost structure by path

| | A. Upstream contribution (PRs to an existing tool) | B. Fork and modify | **C. Build new (chosen)** |
|---|---|---|---|
| Initial cost | Looks low but carries maximum uncertainty | Onboarding onto a 34k★-scale Go codebase | Everything written from scratch |
| Architectural fit | The agent panel and TUI driving require core structural changes — not possible via plugins (shell-out) | Converting a single-view stack (tview PageStack) into split panes means overhauling the rendering layer. The UI Bus and approval gate are also non-orthogonal to the existing event handling → core redesign | Optimized from the start for the target architecture (UI Bus, agent integration, split panes) |
| Reusable assets | — | Roughly the data layer (informer wrappers). The AI loop, adapters, and approval/audit system would all be new regardless | Textual provides split panes, async, theming, and a test runner at the framework level — much of the UI infrastructure that existing tools had to hand-build comes for free |
| Ongoing cost | No control over whether/when PRs merge (two prior AI-integration PRs did not reach merge) | Permanent divergence from upstream → perpetual cost of merging security patches and features. The positioning burden of being "a fork" | Full ownership of maintenance. Maturity (edge cases) takes time to accumulate |
| Language/capability | Go | Go (the LLM ecosystem is comparatively thinner than Python's) | Python — strong in both Textual and the LLM ecosystem |

### Verdict: C (build new)

The core rationale: **this project's differentiators are not orthogonal to existing architectures**:

1. For the agent to drive the TUI, every UI action must pass through the command bus (§4.1) — this is not an add-on but the **central design**. Retrofitting it onto an existing codebase effectively means rewriting the core → path B converges to "fork, then rewrite", and onboarding + retrofit + permanent divergence costs exceed the total cost of building new
2. Path A (contribution) offers no control over project direction, so the feasibility of this scope itself is uncontrollable
3. The biggest risk of building new (the volume of UI infrastructure) is largely absorbed by the Textual framework; the remaining risk (maturity) is managed incrementally using the measured needs in §5 as test scenarios
4. Even as a new build, **proven UX conventions are inherited** (final paragraph of §5), keeping user switching costs as low as a fork's

Choosing to build new does not mean severing ties with the existing ecosystem — the measured needs in §5 stand on knowledge accumulated by existing tool communities, and Phase 3's plugin API and MCP connectivity aim for interoperability with that ecosystem.
