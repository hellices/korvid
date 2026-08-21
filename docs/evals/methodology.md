# Agent Evaluation Methodology

## Purpose

Korvid evaluates whether a locally installable model can use Kubernetes tools
to explore evidence, maintain conversational context, and reach a safe,
grounded conclusion. Model quality and serving hardware are separate:
publishable model comparisons use the same AKS serving protocol.

## Four Evaluation Tiers

### 1. Task diagnostics

The 23 YAML scenarios under `src/korvid/evals/scenarios/` test one user question
against a deterministic fake cluster. The model and Korvid runtime are live;
only Kubernetes responses are fixtures.

Each run records:

- required and forbidden claims in the final answer;
- whether the model fetched the declared ground-truth evidence;
- resolvable and on-target calls;
- malformed calls, write attempts, and safety violations;
- iterations, tokens, and wall time.

### 2. Offline conversational journeys

The YAML files under `src/korvid/evals/journeys/` share one fake cluster across
multiple scripted user turns. One `AgentRuntime` persists for the complete
conversation, so history, corrections, stale targets, and stopping behavior are
real.

Each turn grades:

- answer claims and evidence fetched during that turn (prior-turn evidence
  cannot satisfy a later checkpoint);
- calls made during that turn;
- forbidden stale targets after a user correction;
- maximum useful-call budget;
- UI intent such as `open_describe`;
- malformed calls and runtime errors.

The pack now covers the eight behaviors #176 asked for:

| journey | what a failure looks like |
|---|---|
| `namespace-triage` | names both broken workloads without ordering them |
| `rollout-owner-chain` | stops at the Deployment instead of following the owner chain |
| `triage-and-correct` | keeps investigating the resource the user just ruled out |
| `compare-namespaces` | ranks by warning count, so the noisier namespace wins |
| `healthy-stop` | invents a fault in a namespace that has none |
| `logs-to-events` | re-reads a normal log instead of pivoting to events |
| `rbac-evidence-gap` | hides the withheld read, or fills the gap with a guess |
| `tui-follow` | describes a pane it never opened |

A journey can withhold reads through `cluster.forbidden`, which fails the
matching call with 403 the way a missing RBAC rule does. Rules name a
plural resource (`pods`, `secrets`, `events`) plus optional `namespace`,
`name` and `subresource: log`; omitted keys are wildcards, and a selector
the matcher cannot honour is rejected at load rather than loading as a
denial that quietly allows everything. This is what makes
"evidence is unavailable" distinguishable from "evidence says nothing is
wrong" — the two are identical to a model that never sees a denial, and
answering anyway is the behavior worth catching.

#### Every turn is pinned in both directions

A grading rule nobody has run is a guess. `_JOURNEY_CASES` in
`tests/evals/test_journey.py` holds, for every turn of every bundled
journey, at least one answer that must be **accepted** and one that must be
**rejected**, and a test refuses to let a turn ship without both.

This exists because the rules failed in a consistent way. A required group
that lists a *topic* rather than a *claim* is satisfied by ruling that topic
out: "the liveness probe is fine" satisfied a group requiring the model to
report a liveness probe failure, in four of the eight journeys. Related
shapes: a group of bare nouns (`configuration`, `endpoints`) is satisfied by
naming the subject rather than saying what is wrong with it; a positive
ordering requirement with no mirror prohibition lets an answer concede the
right order and then rank the other way; and keyword groups cannot bind a
claim to a subject, so "api-7b9d-x1 is still running, while api-5c2f has
been scaled away" satisfied a turn asking whether the *old* ReplicaSet still
serves.

The grader now treats an all-clear predicate (`is fine`, `looks normal`,
`seems correct`) as the positive-grammar spelling of a negation, so it no
longer credits a required claim to an answer that rules it out. The
remaining shapes cannot be fixed centrally and are the reason each turn must
carry a rejecting phrasing.

### 3. Stateful operation journeys

The YAML files under `src/korvid/evals/operations/` grade a write
lifecycle rather than an answer. Each journey runs the real `KorvidApp`,
the real approval dialog, the real unmodified fail-closed audit log, and
an injected `WriteOps` over mutable fake state, with a Textual pilot
pressing the same keys a user would. All twelve development templates run
deterministically in CI; seven of them are the required core gate.

Each run grades:

- required lifecycle checkpoints, attributed to the actor that produced
  them (model tool, app internal, approval driver, fixture actor, audit,
  write ops, grader);
- twelve hard-failure rules — unapproved, unaudited, wrong-target,
  uid-less, unrequested, unrelated, retried-after-terminal, and
  boundary-escaping writes among them;
- the truthfulness of the terminal report class;
- efficiency against a per-fixture tool-call budget.

Safety is a pass/fail gate: a journey with a hard safety failure scores
zero quality. State-value assertions are provisional until the Slice B
AKS calibration promotes them, so they never contribute to a model score.

See [operations.md](operations.md) for the pack, the safety boundary, and
how to run it.

### 4. Live AKS journeys

The live journey targets actual Kubernetes resources in the dedicated
`aks-korvid-contract-test` cluster. Model serving remains isolated in
`aks-shared-runners/ollama`.

Safety controls:

- context must be exactly `aks-korvid-contract-test`;
- namespace must start with `korvid-agent-eval-`;
- fixtures carry `app.kubernetes.io/managed-by=korvid-agent-eval`;
- the model receives the read-only profile; fixture writes happen before the
  model starts;
- cleanup deletes only the run namespace;
- the contract cluster returns to its stopped-at-rest state.

The first live fixture created three real Pods:

| Pod | Real state | Cause |
|---|---|---|
| `search-1` | Running, Ready | valid `pause:3.10` image |
| `checkout-1` | CreateContainerConfigError | missing ConfigMap |
| `payments-1` | ImagePullBackOff | nonexistent image tag |

The model was not given these names in advance. The conversation asked it to
explore broadly, then corrected the focus to payments, then requested the
evidence on screen.

## Reproduction

Task pack:

```sh
export KORVID_EVAL_BASE_URL=http://127.0.0.1:11435/v1
export KORVID_EVAL_MODEL=qwen3:8b
export KORVID_EVAL_TIMEOUT_SECONDS=300
uv run python -m korvid.evals --profile small --reps 3 \
  --out report.md --json report.json
```

Offline journeys:

```sh
uv run python -m korvid.evals.journeys_cli --profile small --reps 3 \
  --out journeys.md --json journeys.json
```

Live journey after provisioning an owned namespace:

```sh
export KUBECONFIG=/tmp/korvid-live-contract-kubeconfig
uv run python -m korvid.evals.journeys_cli \
  --live \
  --context aks-korvid-contract-test \
  --namespace korvid-agent-eval-<run-id> \
  --journeys src/korvid/evals/live_journeys \
  --profile small --reps 3 \
  --out live.md --json live.json
```

## 2026-08-04 Protocol Metadata

- serving engine: Ollama 0.32.5, OpenAI-compatible endpoint;
- model node: zone-2 `Standard_D32s_v5` Spot, 30 CPU / 112Gi limit;
- profile: `small`, six iterations, one tool call per iteration;
- result timeout: 300 seconds;
- model quantization: Q4_K_M;
- task source revision: `25649a3`;
- corrected offline journey source revision: `5d37d96`;
- corrected live journey source revision: `8e15c52`;
- live target: `aks-korvid-contract-test`;
- live namespace: `korvid-agent-eval-20260804200747` (deleted after run).

## 2026-08-05 Post-Merge Protocol Metadata

- source revision: `124b1aa` (squash merge of PR #185);
- models: Qwen3 1.7B, Qwen3 8B, Qwen3-Coder 30B-A3B;
- serving engine: Ollama 0.32.5, OpenAI-compatible endpoint;
- model node: zone-2 `Standard_D32s_v5` Spot, 30 CPU / 112Gi limit;
- profile: `small`, six iterations, one tool call per iteration;
- task pack: 23 scenarios ×3 repetitions;
- offline pack: 3 journeys ×3 repetitions;
- live pack: 1 guarded real-cluster journey ×3 repetitions;
- result timeout: 300 seconds for 1.7B/8B, 600 seconds for Coder 30B;
- quantization: Q4_K_M for all three model artifacts;
- runtime context allocation: 4,096 tokens for all runs (Ollama's CPU-only
  default; `OLLAMA_CONTEXT_LENGTH` and request `num_ctx` were unset);
- model artifacts:
  - Qwen3 1.7B:
    `8f68893c685c3ddff2aa3fffce2aa60a30bb2da65ca488b61fff134a4d1730e7`
    (native maximum context 40,960);
  - Qwen3 8B:
    `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`
    (native maximum context 40,960);
  - Qwen3-Coder 30B-A3B:
    `06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca`
    (native maximum context 262,144);
- warm-up before each task/offline/live batch: one non-streaming `/api/chat`
  request to the selected model with the user message
  `Reply with exactly OK`; the response was discarded before timed runs;
- live namespace: `korvid-agent-eval-124b1aa` (deleted after run);
- final state: contract cluster Stopped, `modeleval` zero nodes, Ollama Ready
  on its default pool.

## Prompt Provenance

A score is only comparable when the prompt that produced it is known.
`--json` therefore writes a metadata envelope alongside the per-scenario
results:

```json
{
  "meta": {
    "profile": "small",
    "prompts": {"source": "default", "sha256": "9f2c…"}
  },
  "scenarios": [ … ]
}
```

Journey runs (`korvid.evals.journeys_cli --json`) carry the same envelope
under a `journeys` key. They cannot yet be swept from the CLI, so their
`source` is always `default`; the digest still makes a profile or
tool-schema change visible.

`source` is `default` when the run used the prompts korvid ships, and
`override` when `--system-prompt-file` or `--prompt-append-file` changed
them. The eval CLI does **not** read `~/.config/korvid/config.yaml`: a
configured `agent.prompts` block affects the running TUI, never this JSON,
so a sweep is reproducible from its command line alone.

The digest covers what the model actually receives — the composed system
prompt for the eval surface, including the write/no-write clause, plus the
complete tool schemas that are retransmitted on every request. Rewording a
tool or editing a parameter description therefore changes the digest,
because both change the model's input.

Rules that follow from this:

- a **publishable scoreboard row must carry `"source": "default"`**; a run
  measured under an override is a tuning artifact, not a comparable score;
- a prompt experiment reports the baseline and the variant together, from
  two runs that differ *only* in the prompt file;
- a changed digest with an unchanged prompt file means something else moved
  — a profile change or a tool-schema edit — and the comparison is void.

## Serving Provenance

The prompt fingerprint pins korvid's half of the run. The other half — what
answered — is pinned by the `meta.serving` block the eval CLI writes:

```json
"serving": {
  "model": "qwen3:8b",
  "engine": {"name": "ollama", "version": "0.5.1"},
  "digest": "bbb…",
  "quantization": "Q4_K_M",
  "context_length": 4096,
  "max_context_length": 40960,
  "parameter_size": "8.0B",
  "warmup": true,
  "unavailable": []
}
```

This exists because the 2026-08-10 matrix did not have it: the deployment
served `ollama/ollama:latest`, nothing recorded which version answered, and by
the time the gap was noticed the node was gone (#235). A future campaign that
scores differently could not have told a korvid regression from an engine
upgrade.

Rules that follow:

- a **publishable row requires an empty `unavailable` list**. Anything listed
  there — `engine`, `digest`, `quantization`, `context_length` — is a field the
  run could not pin, and the CLI warns on stderr when the list is non-empty;
- **publishable rows are measured with `--warmup`**, so the first scenario is
  not charged for paging the weights in. `warmup: false` in an artifact means
  no warm-up happened, including the case where the request failed;
- **`context_length` is the runtime allocation, not the model's maximum.** The
  two differ by an order of magnitude — Qwen3 advertises 40,960 while ollama
  may serve 4,096 — and only the allocation was in effect. It comes from
  `/api/ps`, which lists loaded models only, so a run without `--warmup`
  leaves it unpinned. `max_context_length` carries the native maximum for
  context;
- **pin the serving deployment to a released tag.**
  [`deploy/eval/ollama.yaml`](../../deploy/eval/ollama.yaml) is the checked-in
  manifest and a test rejects `:latest`. A floating tag makes the recorded
  version a coincidence rather than a decision;
- the block is **omitted entirely** in artifacts written before this capture
  existed, which is deliberate: absence means "never captured", not "captured
  and empty".

## Measuring the tool surface itself

`--without-tool NAME` (repeatable) drops a tool from the measured surface.
It exists for the controlled arms of #221: `diagnose_service` and
`diagnose_pvc` were added to the `small` profile, whose whole premise is a
small selection space for 3B-14B models, and that cost was never measured.
Deciding it needs the same models, scenarios and prompts with the surface as
the only variable.

```bash
# arm 2 - the shipped small surface
python -m korvid.evals --profile small --json arm2.json

# arm 3 - the same run without the two diagnostics
python -m korvid.evals --profile small   --without-tool diagnose_service --without-tool diagnose_pvc --json arm3.json
```

Rules:

- an unknown name is **refused**, because a typo would silently measure the
  full surface and publish it as the reduced arm;
- the omission reaches the prompt fingerprint, so two arms can never claim
  the same digest;
- `meta.tools` records `omitted` and `count` by name. Recovering the arm from
  the digest alone would mean keeping a lookup table outside the artifact,
  which is the bookkeeping that made the 2026-08-05 rows unusable;
- dropping a tool is **not** a prompt override: `prompts.source` stays
  `default`, because the shipped prompts are still the ones in effect;
- the tool is **removed, not hidden**. Withholding only the schema would
  leave the arm open to contamination: the runtime dispatches whatever name
  the provider returns and the executor resolves every registry read, so a
  model that remembered the tool would still get its answer and the run
  would credit the call. An omitted call is refused at execution, and it
  counts as a **malformed** call — which is itself the measurement, since
  the question is whether a small model reaches for a tool it was not
  given.

## Interpretation Limits

- Task success does not prove conversational usability.
- Offline journeys do not prove Kubernetes fixture realism.
- One live journey does not cover every interactive workflow.
- UI calls are recorded headlessly; they prove correct Korvid UI intent, not
  visual Textual rendering.
- A model is recommended only when task, offline conversation, and live results
  are shown separately.

## Artifact Retention

Human-readable methodology, scenario descriptions, and scores are versioned
with the source code. Generated raw outputs are not:

- the append-only [`eval-results`](https://github.com/hellices/korvid/tree/eval-results)
  branch stores dated compressed artifacts;
- each run directory includes metadata and SHA-256 checksums;
- issue #176 links the same artifact directory and contains only summaries;
- application commits do not carry model transcripts or pull logs.
