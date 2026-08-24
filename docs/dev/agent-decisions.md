# Agent capability decisions

Why korvid's agent is shaped the way it is, and which directions were tried,
measured, or rejected.

Read this **before** proposing new agent capabilities. Several questions here
have already been settled with evidence, and re-deriving that evidence is
expensive. Where a decision is provisional, this document says so and names
what would change it.

This is a living document. Add a decision when one is made; correct one when
measurement overturns it.

---

## 1. Deterministic analyzers: two shipped, expansion stopped

**Decision.** `diagnose_service` and `diagnose_pvc` stay. The four further
analyzer families proposed in #191 — rollout generalization, PodDisruptionBudget
drain analysis, node pressure/scheduling/quota, missing ConfigMap/Secret
references — are **not planned**. #191 is closed.

**The line that was drawn.** Not "no analyzers", but *direct projection versus
inference*:

| Kept | Rejected |
|---|---|
| "No EndpointSlices matched this Service name" | "This rollout is stalled" |
| "PVC phase is Lost" | "This PDB is blocking maintenance" |
| "A ProvisioningFailed event exists" | "This pod cannot be scheduled" |
| "The named StorageClass is not in the cluster's list" | "This ConfigMap is unused" |

The left column restates observed API state. The right column infers intent,
timing, or causality that the API does not carry. Every shipped rule is in the
left column, and the Service rules deliberately stop short of concluding
"selector mismatch" even when that is the likely cause.

**Why, with external evidence.** [k8sgpt](https://github.com/k8sgpt-ai/k8sgpt)
(8k stars, CNCF sandbox, since 2023) is the same architecture at scale — 28
analyzers feeding an LLM — and its bug tracker documents the failure mode
precisely:

- [#849](https://github.com/k8sgpt-ai/k8sgpt/issues/849): on GKE, every managed
  ingress was reported as using a nonexistent IngressClass, and the LLM
  confidently advised `kubectl create ingressclass gce` — actionably wrong on a
  production cluster. Open for **over two years**.
- [#1668](https://github.com/k8sgpt-ai/k8sgpt/issues/1668): the fix for #849 was
  a hardcoded GKE allowlist, which **immediately** created the same false
  positive for AWS Load Balancer Controller users. Fires every 30 minutes.
- [#1723](https://github.com/k8sgpt-ai/k8sgpt/issues/1723): the Job analyzer
  reads `status.Failed > 0` as failure, but that is a cumulative attempt
  counter. A Job that retried twice within `backoffLimit` and succeeded is
  reported as failed.
- [#1720](https://github.com/k8sgpt-ai/k8sgpt/issues/1720): the ConfigMap
  analyzer iterates `Containers` but not `InitContainers`, so a ConfigMap used
  only by an init container is flagged unused.

The mechanism repeats: a rule encodes what "normal" looks like, the assumption
fails on some cluster, the patch is a hardcoded exception, and the exception is
incomplete for the next platform. Closed-world rules in an open world.

**Each deferred family maps onto one of those bugs.** ConfigMap/Secret
references *is* #1720 — and the real surface is wider still (initContainers,
ephemeral containers, projected volumes, external-secrets-operator where the
Secret appears after scheduling, SealedSecrets where the plaintext never
exists). Rollout health is the same class as #1723: `unavailableReplicas > 0`
is normal during a rolling update, a surge window, and HPA scale-up. A PDB
blocking eviction is frequently *correct* — it is protecting a quorum.
Unschedulable pods are the expected steady state while cluster-autoscaler
provisions.

**What keeps the shipped two safe.** Severity and confidence are hedged where
the evidence is weak. `pvc.provisioning_pending` is `info`/`medium` and says
"provisioning has not reported a specific failure" — it declines to assert a
fault. Compare k8sgpt's "Job X has failed" for a Job that succeeded.

**Provisional, and what would change it.** The *value* of the two shipped tools
is unverified. No published benchmark compares analyzer-based against agentic
troubleshooting — [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) chose a
pure agentic loop and publishes
[150+ evaluations](https://holmesgpt.dev/latest/development/evaluations/), but
with no pre-analysis control arm and no published rationale. #176 will run the
first matched comparison. If it shows no benefit, the right response is to
simplify them into bounded evidence-bundling reads, or remove them.

One finding cuts the other way and is worth preserving. The
[STAR](https://arxiv.org/search/?searchtype=all&query=LLM+agent+AIOps+root+cause+analysis)
framework finds structured intermediate representations *do* help — but STAR's
structure is **evidence the agent assembles into conclusions**, not conclusions
handed to the agent. `AnalysisReport` carries both halves: `Evidence` /
`EvidenceGap`, and `Finding`. The evidence favours the first half. A `Finding`
is something the model can anchor on; the k8sgpt Ingress case is exactly an LLM
elaborating a wrong conclusion with confidence.

**Related:** #191 (closed), #213, #216, #221.

---

## 2. Prompts: one pack per tier, additive local rules, lowest-leverage lever

**Decision.** korvid ships one prompt pack per capability tier (`low`, `high`)
and does **not** fork per model. A deployment adds house rules with
`agent.rules`; the role statement and the tool wording are korvid's.

**Superseded, and how.** This decision originally shipped `agent.prompts`,
which let a deployment replace the role statement and reword individual tool
descriptions. Issue #316 removed both. Replacing the role statement made a
deployment's prompt un-measurable against the scoreboard (every published row
assumes the shipped wording), and per-deployment tool wording made the same
tool describe itself differently on the agent, MCP and eval surfaces. What
survives is the part that carried the evidence: additive rules, layered after
the immutable safety contract, plus eval-only pack/overlay grinding so a
wording change ships once numbers support it.

**The ordering matters more than the feature.** Published evidence puts the
levers in this order, and the documentation must present them this way:

1. **Model choice.** Small models' tool-calling weakness is largely a training
   property; no prompt closes it
   ([ToolLLM](https://arxiv.org/abs/2307.16789)).
2. **Tool descriptions.** Documentation quality dominates prompt preamble for
   tool-selection accuracy ([EasyTool](https://arxiv.org/abs/2401.06201),
   [Tool Documentation](https://arxiv.org/abs/2308.00675)).
3. **Context budget.** `agent.model_tier: low` and `agent.ollama.num_ctx`.
4. **Then** the role statement.

This independently confirms korvid's own measurement, recorded in #176: *"deterministic
compound tools and output projections often improve small-model behavior more
reliably than adding prompt text."* Two sources, one internal and one external,
reaching the same conclusion.

**The pattern is established, but not uniform.** Of seven comparable tools, four
expose an override — [kubectl-ai](https://github.com/GoogleCloudPlatform/kubectl-ai)
(`promptTemplateFilePath`), [HolmesGPT](https://github.com/HolmesGPT/holmesgpt)
(`file://` prompts plus `custom_instructions`), Continue.dev
(`baseSystemMessage`), Ollama (Modelfile `SYSTEM`). Cline allows append-only.
Aider and k8sgpt expose none; k8sgpt sends no system role at all.

**Why the override replaces a slot, not the composed prompt.** The effective
prompt for a local model has three layers, and merging them is the source of
risk:

| Layer | Owner |
|---|---|
| Chat-template formatting (`<\|start_header_id\|>`, `<start_of_turn>`) | the serving engine, below korvid |
| Tool-schema injection | korvid, sent as native structured definitions |
| Behavioural framing (role statement) | **configurable** |

kubectl-ai shows the hazard: under `--enable-tool-use-shim` the tool JSON lives
*inside* the system prompt, so an override that drops `{{.ToolsAsJSON}}`
silently disables tool calling. korvid is not exposed to that, and the same
reasoning keeps the write/no-write and UI clauses out of every configurable
layer — the prompt harness derives them from the tools the resolved policy
actually armed.

That preserves two properties: the model is never told about a capability it
was not offered, and a read-only deployment keeps offering the equivalent
`kubectl` command instead of a bare refusal.
`tests/agent/test_prompt_harness.py` pins both, together with the rule that a
house rule cannot widen what the safety contract granted.

**No safety behaviour is reachable from configuration.** Approvals, audit, and
read-only enforcement live in code. "Delete pods without asking" produces a
model that tries and is refused.

**Per-model forks remain unsupported.** Model *families* need different chat
templates — that is well established
([Continue.dev's `autodetect.ts`](https://github.com/continuedev/continue) carries 20+)
— but that is message formatting, handled below korvid. Per-family prompt
*content* forks have no evidence behind them.

**Related:** #71, #176, #177.

---

## 3. Evaluation is the deciding instrument, so its defects are load-bearing

**Decision.** Questions about agent capability value are settled by the eval
harness, not by argument. That makes harness bugs more serious than they look:
a defective grader does not merely under-report, it produces a *confident wrong
answer* to the question the project is using to make decisions.

**A concrete case.** `evals/grader.py` folds identity keys onto `name` so
evidence counts whichever read tool fetched it. The table knew only `pod`, so a
model correctly calling `diagnose_service(service=…)` or `diagnose_pvc(pvc=…)`
was graded as having fetched **no evidence**. Run in that state, #176's matched
comparison would have measured the alias table and concluded the diagnostic
tools were useless.

Fixing that surfaced a second defect underneath: `matches_target` compares
`kind` only when both sides carry one, and diagnostic tools have no `kind`
argument — so `diagnose_pvc(pvc="web")` satisfied evidence about a *Service*
named `web`. That one predated the diagnostic tools entirely
(`get_logs(pod="web")` had always matched Deployment evidence). Aliases now
carry the kind they imply.

**Consequences adopted:**

- **Fingerprint every run.** The eval JSON carries `meta.prompts.source`
  (`default` | `override`) and a sha256 over the composed prompt *and* the
  complete tool schemas. A scoreboard row that cannot say which prompt produced
  it is not comparable with any other row.
- **Publishable rows must be `source: default`.** A run under an override is a
  tuning artifact.
- **Sweeping is first-class.** `--system-prompt-file` / `--prompt-append-file`
  make "find a better default" a measurement loop rather than an argument.
- **The eval CLI deliberately does not read `config.yaml`.** A sweep must be
  reproducible from its command line alone.

**Still open.** Two harness gaps block #176's comparison: the Service scenarios
seed legacy `Endpoints` while `diagnose_service` reads `EndpointSlice`, and no
bundled scenario exercises either diagnostic tool.

**Related:** #69, #176, #219.

---

## 4. Tool surface is a cost, and it has not been paid for

**Open question, recorded so it is not forgotten.** `diagnose_service` and
`diagnose_pvc` were added to every agent surface, the low tier's included:

| low-tier surface | tools | schema |
|---|---:|---:|
| with both | 16 | 8,107 chars |
| without | 14 | 7,046 chars |

`diagnose_pvc` now has the **longest description of all 16 tools**, and the four
`diagnose_*` tools occupy the top four places by description length. The schema
is retransmitted on every request.

The low tier exists to shrink the surface for 3B–14B models, citing BFCL:
they are competitive on single-function calls but fall behind sharply on
**multi-function selection**. Adding two tools may be a net win — one bounded
call replaces a multi-step sequence, which is what small models are bad at — or
a net loss. Both are plausible; neither is measured.

Tracked in #221 as a third arm of #176's evaluation.

---

## 5. What this project competes on

From the [design document](specs/2026-07-23-korvid-tui-design.md) §1, the
differentiators against general-purpose agents (Claude Code plus a Kubernetes
MCP) are:

> automatic screen-context injection + TUI driving + approval/audit system

Deterministic analyzers are **not** on that list, and today no consumer of
`AnalysisReport` exists under `src/korvid/ui/` — they feed neither screen
context nor TUI driving. They live purely on the agent tool surface, which is
where general-purpose agents compete hardest.

This is not an argument to remove them. It is the reason capability proposals
should be checked against the differentiators before they are checked for
technical merit: a capability that a general-purpose agent already has is worth
less to korvid than one that only a TUI-integrated agent can offer.

---

## 6. Recurring failure modes to watch for

Patterns that have already cost this project time:

- **Building structure ahead of measurement.** #191 built two analyzers and
  planned six before anyone asked whether findings improve outcomes. The
  correction was to stop and hand the question to #176.
- **Freezing an unverified abstraction into a public API.** #195 proposes a
  versioned analyzer extension API. Doing that before #176 reports would commit
  korvid — and third-party authors — to a shape there is the most reason to
  doubt, in the same release cycle it was questioned.
- **Trusting the profiler over the stopwatch.** `phase_style` looked like the
  top cost centre in a cold-build profile (0.37 s / 50k calls). Measured without
  the profiler: 6.16 ms → 0.91 ms, about 0.3% of a 1.6 s bootstrap. The
  optimization was dropped. cProfile inflates per-call overhead; confirm hot
  paths with a direct benchmark before acting.
- **Assuming the instrument works.** See §3.
- **Writing eval keywords for a negative diagnosis.** When the correct answer
  is itself negative ("endpoints are *not* ready", "this is *not* a storage
  class problem"), two grader behaviours bite. A negator suppresses every
  keyword after it in the same clause, so a bare positive noun like `endpoint`
  fails on "none of its endpoints are ready". And `must_not_mention` matches
  token *runs*, so `no endpoints` fires on the correct "no endpoints are
  ready", and `no storage class` on "no storage class problem" — the word that
  flips the meaning is never seen. Three review rounds on #227 found one
  instance each. Write required keywords that carry their own negator or are
  stated positively, keep forbidden keywords specific enough that no correct
  sentence starts with them, and validate against several real phrasings
  before trusting the list.

---

## 7. The agent loop stays native; no agent framework is adopted

**Decision.** korvid keeps its own agentic loop. `AgentEngine` /
`NativeAgentEngine` (2026-07, #316) drive the provider round-trip directly on
top of korvid's own `ConversationState`, `RequestGateway` and `ToolHarness`.
Pydantic AI was the candidate evaluated — it is typed, streaming-first and
would have been the least-bad fit — and it was rejected. **No agent-framework
dependency was added.**

**What was evaluated.** Pydantic AI's only supported interception seams for a
host that wants to keep its own loop are the model layer:

- [`pydantic_ai.models.Model`](https://ai.pydantic.dev/api/models/base/#pydantic_ai.models.Model)
- [`Model.request_stream`](https://ai.pydantic.dev/api/models/base/#pydantic_ai.models.Model.request_stream)
- [`pydantic_ai.models.wrapper.WrapperModel`](https://ai.pydantic.dev/api/models/wrapper/#pydantic_ai.models.wrapper.WrapperModel)

**Why they do not fit.** Three blockers, each one load-bearing for a security
or correctness invariant korvid already ships:

1. **No hook at the byte korvid must police.** `request_stream` is handed
   `messages: list[ModelMessage]` — framework objects, *before* the concrete
   model serializes them into the provider's wire shape. korvid's
   `OutboundPolicy` runs on the serialized request: it counts the exact
   characters that will leave the process, enforces the redaction and
   tool-schema rules on that payload, and raises `OutboundRequestTooLarge` /
   `OutboundPolicyError` so the engine can drop the oldest completed turn and
   retry. A `WrapperModel` can only inspect the pre-serialization objects; the
   provider-specific mapping happens inside each concrete `Model` and is not
   public API. Policing a copy that is not the thing transmitted is exactly
   the failure mode the gateway exists to prevent.
2. **External interruption cannot be repaired in the framework's history.**
   korvid's hard interrupt is task cancellation from the UI, which can land
   mid-stream with a tool call already announced and no result. `ConversationState`
   then repairs the durable history — drop the unmatched call, append the
   interrupt marker, keep the usage estimate — and the *same* conversation
   continues. Pydantic AI owns message history inside its run graph and
   rebuilds it from completed `ModelResponse` parts; a cancelled
   `request_stream` leaves nothing the framework will accept as a repaired
   turn, so korvid would keep the authoritative history anyway and hand the
   framework a translation of it. Two histories, one of them the one actually
   audited, is a worse position than none.
3. **No per-iteration interception of excess tool calls.** korvid's policy is
   one tool call at a time: keep the allowed prefix, emit
   `ToolCallStarted`/`ToolCallFinished(ok=False)` for every discarded call so
   the transcript stays honest, and append a fixed notice to the last kept
   result. Pydantic AI executes the tool calls of a response inside its graph
   node, concurrently by default, below the point a `WrapperModel` can see;
   there is no supported seam to truncate the call list, report the discards
   as UI events and edit the last kept tool result before the next request.
   Working around it means re-implementing the tool node — which is the loop
   the framework was supposed to provide.

**Cost accepted.** korvid maintains its own loop: streaming parse, tool
dispatch, budget retry, cancellation. That is roughly 500 lines
(`native_engine.py`) over components korvid needs regardless, and it is fully
covered by contract tests written against the ABC rather than the
implementation.

**What would change this.** A framework that exposes (a) a hook on the
serialized provider request, (b) an injectable tool-dispatch step per
iteration, and (c) a documented way to hand back an externally repaired
history. If Pydantic AI grows all three, re-run this evaluation — the
`AgentEngine` ABC exists so a second engine can be written without touching
the session, the UI or the tools.
