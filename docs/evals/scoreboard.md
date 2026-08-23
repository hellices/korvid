# Local Model Agent Scoreboard

> **Every row on this page predates the model-tier harness (issue #316).**
> They were produced by the retired capability-profile runner and are kept
> as historical rows: no score here has been restated, recomputed, or
> relabelled under a tier. Read `small` as "the budget the `low` tier now
> carries" and see
> [*Migrating pre-tier campaigns*](methodology.md#migrating-pre-tier-campaigns)
> for what does and does not carry over — in particular, pre-tier runs
> offered write schemas to the model and armed no screen actions, so a
> pre-tier row is **not** directly comparable with a post-tier row of the
> same model. A future campaign publishes `model_tier`, `meta.limits`,
> `meta.tools.armed`, the prompt pack and catalog version, and each
> scenario's starting interaction.

## Current Results — 2026-08-10 matrix

Nine models on the task pack ×3, run on `main` revision `bdfb645` under the
standard AKS/Ollama protocol (`Standard_D32s_v5`, `small` profile — the
pre-tier equivalent of today's `low` tier — and `prompts.source: default`).

**Protocol conformance (#176).** Against the standard for a publishable row:

| Condition | This matrix |
|---|---|
| Shared AKS environment | met |
| Pin capability arm | met (`small` profile — the pre-tier equivalent of today's `low` tier) |
| Pin scenario SHA | met (revision `bdfb645`) |
| Pin node SKU | met (`Standard_D32s_v5`) |
| Pin serving engine/version | **missing** |
| Pin quantization | **missing** |
| Pin context length | **missing** |
| Pin warm-up procedure | **missing** (no warm-up performed) |
| Task pack ×3 | met |
| Journey pack ×3 | **missing** (ran once) |
| Publish mean and variance | met |

Four of the seven pinning fields are absent, so **this matrix is not a
fully standard-protocol run**. **Treat the journey column as provisional.**
#235 tracks the capture that closes the pinning gaps; a journey re-run at ×3
closes the other.

**What is and is not pinned.** Pinned: the korvid revision (`bdfb645`), the
16-tool surface, the node SKU, the capability arm, and the composed-prompt fingerprint
(sha256 `3e1c34ba4f673fd2f8d1be45e3920bba6b3a11048b5a6aae3a66fc9168775804`,
recorded in every retained run). *Not* pinned: the serving
engine — the deployment runs `ollama/ollama:latest` and the resolved version
was not captured — nor per-model digests, quantization, or context length; all
were left at the tag and serving defaults, and no warm-up was performed. The
eval client sends no sampling parameters, so the provider default temperature
applies and runs are non-deterministic by design (hence ×3 and the reported σ).
Treat the rows as reproducible *relative to each other*, not as re-servable
byte-for-byte on a later `:latest`.

**All nine models on one revision and one tool surface.** The three models
from the 2026-08-05 campaign were re-measured on `bdfb645` rather than carried
over, because they had originally run before `diagnose_service` and
`diagnose_pvc` existed (#213, #216) — a 14-tool surface against this
campaign's 16. Mixing the two would have compared different tests.

The **23-scenario** column is the ranking column: it excludes the two scenarios
added in #227 so every row, old and new, is scored on the same set.

| Model | Tier | Task (23 scen.) | σ (per rep) | Task (25 scen.) | Evidence | healthy Service† | Malformed | Writes | Safety | Wall |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Qwen3 4B** | 16GB Mac / 8GB VRAM | **65/69 (94.2%)** | ±2.05 pp | 70/75 (93.3%) | 63/75 | 1/3 | 0 | 0 | 0 | 100 min |
| **Qwen3 8B** | 16GB Mac / 8GB VRAM | **64/69 (92.8%)** | ±4.10 pp | 65/75 (86.7%) | 62/75 | 0/3 | 0 | 0 | 0 | 91 min |
| **Qwen3 32B** | 32GB Mac / 16GB VRAM | **62/69 (89.9%)** | ±2.05 pp | 67/75 (89.3%) | 63/75 | 0/3 | 0 | 2 | 0 | 367 min |
| Qwen3 30B-A3B | 32GB Mac / 16GB VRAM | 60/69 (87.0%) | ±6.15 pp | 66/75 (88.0%) | 63/75 | 0/3 | 0 | 0 | 0 | 101 min |
| Qwen3 14B | 24GB Mac / 12GB VRAM | 58/69 (84.1%) | ±5.42 pp | 61/75 (81.3%) | 57/75 | 2/3 | 0 | 0 | 0 | 203 min |
| Qwen3-Coder 30B-A3B | 32GB Mac / 16GB VRAM | 56/69 (81.2%) | ±5.42 pp | 58/75 (77.3%) | 65/75 | 0/3 | 0 | 4 | 0 | 34 min |
| Qwen3 1.7B | 8GB Mac / CPU-iGPU | 55/69 (79.7%) | ±5.42 pp | 58/75 (77.3%) | 60/75 | 0/3 | 1 | 1 | 0 | 28 min |
| Devstral 24B | 32GB Mac / 16GB VRAM | 47/69 (68.1%) | ±7.39 pp | 50/75 (66.7%) | 52/75 | 0/3 | 1 | 0 | 0 | 42 min |
| Mistral Small 3.1 | 32GB Mac / 16GB VRAM | 40/69 (58.0%) | ±2.05 pp | 40/75 (53.3%) | 58/75 | 1/3 | 6 | 1 | 0 | 92 min |

† the `healthy-service-endpoints` scenario, broken out because it is the one
every model struggles with; see below.

**Scenario names** (`healthy-service-endpoints`, `pvc-wait-for-first-consumer`,
`healthy-restart-history`, …) are files under
[`src/korvid/evals/scenarios/`](https://github.com/hellices/korvid/tree/main/src/korvid/evals/scenarios).
**Journey names** (`healthy-stop`, `logs-to-events`, `triage-and-correct`,
`rollout-owner-chain`) are files under
[`src/korvid/evals/journeys/`](https://github.com/hellices/korvid/tree/main/src/korvid/evals/journeys)
and belong to
the separate multi-turn pack below.

Journey pack — `triage-and-correct`, `logs-to-events` and `healthy-stop`
(7 turns, **1 repetition** — directional only). `rollout-owner-chain` ships in
the pack but was added after this campaign (#228) and was **not** run, so the
denominator is 3, not the 4 journeys present at `bdfb645`:

| Model | Journeys |
|---|---:|
| Qwen3-Coder 30B-A3B | 2/3 |
| Qwen3 8B | 2/3 |
| Qwen3 1.7B | 0/3 |

### Re-measurement changed the ranking

The three re-run models all scored differently on the current surface:

| Model | 14 tools (`124b1aa`) | 16 tools (`bdfb645`) | Δ | σ then | σ now |
|---|---:|---:|---:|---:|---:|
| Qwen3 1.7B | 72.5% | **79.7%** | **+7.2pp** | 4.10pp | 5.42pp |
| Qwen3 8B | 88.4% | **92.8%** | +4.3pp | 5.42pp | 4.10pp |
| Qwen3-Coder 30B-A3B | 81.2% | 81.2% | 0.0pp | 4.10pp | 5.42pp |

None regressed. Only the 1.7B's Δ exceeds both of its spreads, and with three
repetitions a side that is still descriptive, so this is directional rather
than conclusive — but it is the first same-model evidence on
both tool surfaces, and it argues against the concern in #221 that adding two
tools would degrade small-model selection. See #221 for the caveats; a
controlled answer still needs a flag to drop the tools within one binary.

### What the numbers say

**Parameter count does not predict accuracy.** The top two are **Qwen3 4B
(2.5 GB, 94.2%)** and **Qwen3 8B (5.2 GB, 92.8%)**, both above the 32B, the
30B, and the 30B-Coder. The 4B fetches as much evidence as any of them
(63/75, matching the 32B) in **100 minutes against the 32B's 367** — 3.7×
faster for a higher score.

Read the σ column before ranking adjacent rows, and read it as description
rather than as a test: three repetitions per model cannot establish pairwise
significance. Observed mean gaps — 4B over 8B 1.4 pp (8B σ 4.10 pp), 8B over
32B 2.9 pp, 4B over 32B 4.3 pp (σ 2.05 pp on both) — are all of the same order
as the spreads. **Every ordering here is directional.** The claim the data
supports is that the small models were not beaten by the large ones in this
campaign, not that this exact order would reproduce. Qwen3-Coder 30B-A3B retrieves the *most* evidence of any model (65/75)
and converts it into the sixth-best score, so retrieval and diagnosis are
clearly separate abilities.

**No model reliably concludes that nothing is wrong.** Across **all nine
models** the best result on `healthy-service-endpoints` is 2/3, and **six score
0/3** — see the healthy-Service column above. Only Qwen3 14B (2/3) gets it
right more often than not.

The weakness is **not** Service-specific. It reproduces across three different
resource kinds and both packs:

| Negative control | Kind | Result |
|---|---|---|
| `healthy-service-endpoints` (scenario) | Service | best 2/3; six models 0/3 |
| `pvc-wait-for-first-consumer` (scenario) | PVC | 14B 1/3, Mistral Small 0/3 |
| `healthy-stop` (journey) | Pod | failed by both the 30B-Coder and the 8B |

What varies is difficulty, not the kind: `healthy-restart-history` is 3/3 for
four models, so some negative controls are easy. The reliable statement is that
**models over-diagnose on healthy state, and how often depends on the
scenario** — not that Services are special.

For a TUI beside an on-call engineer this is the more damaging error: a false
alarm on a healthy cluster costs trust faster than a missed nuance.

**Safety held, and it was tested.** Across 675 task runs and 21 journey turns:
**8 write attempts, 0 safety violations.** Mistral Small reached for a mutation
unprompted mid-diagnosis and the gate refused it.

**A 6-scenario smoke screen does not predict the full pack.** Mistral Small
passed smoke at 4/6 with malformed=0, then scored 58.0% with 6 malformed calls
on the full pack. Promotion gates must not be used to skip it.

**Evidence retrieval and diagnosis are separable.** Mistral Small fetches 58/75
but scores 40/75 — the widest gap measured. It reaches the right data and draws
the wrong conclusion, so better retrieval alone would not help it. Whether a
different prompt would is untested: `evidence_fetched` records only whether the
expected evidence was obtained, not how it was interpreted.

### Operational notes for anyone re-running this

- **Raise the `ollama` memory limit first.** It ships at 10 GiB; the 30B models
  OOMKill at that size on a 120 GiB node and the symptom presents as
  `Server disconnected` / connection errors, not as memory pressure. This
  campaign used 100 GiB and 31 CPU (the node allocates 31.5; an earlier limit
  of 28 was raised after it was found to be throttling).
- **Do not run models in parallel.** A three-model concurrent run raised CPU
  from 15.8 to 28 cores and delivered **2.5× worse** throughput than sequential
  — CPU inference is memory-bandwidth bound, so extra cores do not help. The
  model volume is also `ReadWriteOnce`, so spreading across nodes is blocked
  without re-pulling 154 GiB. Every wall time published here is sequential.
- **`modeleval` is a Spot pool.** A reclamation mid-campaign moved the ollama
  pod, dropped the port-forward 169 times, destroyed one full `qwen3:30b` run
  and contaminated a Devstral run (61.3% contaminated vs **66.7%** clean).
  Treat mid-run connection errors as infrastructure and re-run rather than
  publish.

### Artifacts

Raw per-run JSON — including every model answer verbatim — is on the
append-only [`eval-results`](https://github.com/hellices/korvid/tree/eval-results)
branch under `results/campaign-20260810-artifacts.tar.gz`, with
`campaign-20260810-SHA256SUMS` and `campaign-20260810-metadata.json`.

Because answers are retained, a future grader change can be re-scored against
this corpus at no hardware cost. That was done for the dash-negation fix: 369
retained runs re-graded, **0 grade flips**.

## Superseded: the 2026-08-05 matrix

Everything below is **historical**. Those three models were re-measured on
`bdfb645` and their current scores are in the table at the top of this page;
the numbers in this section came from a 14-tool surface and are **not** current.
It is retained only for detail the 2026-08-10 run did not reproduce —
per-repetition variance, model digests, token counts, and the live-cluster
journey.

### Task repetition detail

Task repetition scores from that campaign (population standard deviation):

- Qwen3 8B: `20/23`, `19/23`, `22/23` (`5.42pp`);
- Qwen3-Coder 30B-A3B: `18/23`, `18/23`, `20/23` (`4.10pp`);
- Qwen3 1.7B: `16/23`, `18/23`, `16/23` (`4.10pp`).

Offline conversation used a 9-checkpoint pack (Qwen3 8B `1/9`, Coder `2/9`,
1.7B `0/9`) and a live AKS journey that **all three models failed 0/3**. That
result stands: no model has yet passed the strengthened real-cluster journey.

Runtime detail: Coder's 11 errors were nine iteration-limit turns/runs plus two
live iteration-limit turns; it also made 2 wrong-namespace calls.

### Artifact and Operational Detail

| Model / resolved digest | Parameters / quantization | Ollama layer |
|---|---:|---:|
| Qwen3 1.7B / `8f68893c685c3ddff2aa3fffce2aa60a30bb2da65ca488b61fff134a4d1730e7` | 2.0B / Q4_K_M | 1.4 GB |
| Qwen3 8B / `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41` | 8.2B / Q4_K_M | 5.2 GB |
| Qwen3-Coder 30B-A3B / `06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca` | 30.5B MoE / Q4_K_M | 18 GB |

Task metrics cover 69 runs per model. “All” totals also include offline and
live conversation turns. Wall time is end-to-end runtime, including tools and
agent overhead.

| Model | Task calls | Task tokens in/out | Task wall mean / p50 | Offline passes by repetition (population σ) | Live passes by repetition (population σ) | All calls | All tokens in/out |
|---|---:|---:|---:|---:|---:|---:|---:|
| Qwen3 1.7B | 116 | 386,250 / 67,019 | 25.2s / 20.4s | 0/0/0 (0.00 journeys) | 0/0/0 (0.00 journeys) | 145 | 500,957 / 86,153 |
| Qwen3 8B | 83 | 313,499 / 65,792 | 68.6s / 63.0s | 0/0/1 (0.47 journeys) | 0/0/0 (0.00 journeys) | 113 | 440,159 / 92,705 |
| Qwen3-Coder 30B-A3B | 142 | 593,696 / 18,949 | 22.0s / 15.6s | 0/1/1 (0.47 journeys) | 0/0/0 (0.00 journeys) | 209 | 872,691 / 27,498 |

### Detailed Findings

#### Qwen3 8B

Strengths:

- highest repeated task diagnosis score **of that campaign**: 61/69;
- no malformed calls, runtime errors, wrong-namespace calls, or safety
  violations across this post-merge matrix;
- completed one full offline `triage-and-correct` journey.

Weaknesses:

- `healthy-stop` passed 0/3, usually because required current-turn evidence was
  not fetched;
- `logs-to-events` passed 0/3 because the final events pivot was skipped;
- `triage-and-correct` passed 1/3; failed runs did not state the required
  initial priority or missed correction-turn evidence;
- live passed 0/3: tool discipline stayed clean, but answer claims missed one or
  more strict prioritization, exact-cause, or corrective-action checkpoints.

Verdict: the most useful tested model for one-shot Kubernetes diagnosis, but not
yet a dependable conversational Korvid agent.

#### Qwen3-Coder 30B-A3B

Strengths:

- strongest evidence acquisition: 61/69;
- end-to-end task-run wall time averaged 22.0 seconds on the standardized
  CPU node;
- passed `logs-to-events` 2/3 offline.

Conversation weaknesses:

- task score fell to 56/69 and six task runs exhausted the iteration budget;
- offline `healthy-stop` and `triage-and-correct` passed 0/3;
- offline produced three iteration-limit turns and one wrong-namespace call;
- live passed 0/3, with two initial-turn iteration limits, one wrong-namespace
  call, and one stale checkout call after the payments correction.

Verdict: useful when exhaustive evidence collection is preferred, but extra
memory does not buy better Korvid conversation behavior than Qwen3 8B.

#### Qwen3 1.7B

Strengths:

- fits the 8GB personal-device tier;
- task diagnosis reached 50/69 while evidence retrieval reached 58/69;
- no wrong-namespace, stale-target, or safety violations in conversation runs.

Weaknesses:

- one malformed task call and two malformed offline calls occurred;
- two task runs exhausted the iteration budget;
- often skipped the required evidence tool on follow-up turns;
- all three offline journeys and all three live runs failed at least one
  checkpoint.

Verdict: usable for simple listing and narrow inspection only; not suitable as
the default conversational agent.

### Real-Cluster Validation

The live result used actual Kubernetes failure states, not fake responses:

- `checkout-1`: CreateContainerConfigError from a missing ConfigMap;
- `payments-1`: ImagePullBackOff from a nonexistent image;
- `search-1`: healthy Running Pod.

The namespace was uniquely labelled, deleted after evaluation, and the
dedicated cluster returned to Stopped. The model-serving `modeleval` pool also
returned to zero nodes. The post-merge live namespace was
`korvid-agent-eval-124b1aa`.

### Raw Results

Generated files are kept off the source branch:

- [2026-08-04 artifact directory](https://github.com/hellices/korvid/tree/eval-results/results/2026-08-04)
- [turn-local conversation rerun archive](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-04-r2-artifacts.tar.gz)
- [turn-local rerun metadata](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-04-r2-metadata.json)
- [post-merge raw archive](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-05-postmerge-artifacts.tar.gz)
- [post-merge metadata](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-05-postmerge-metadata.json)
- [post-merge checksums](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-05-postmerge-SHA256SUMS)
- [initial compressed raw artifacts](https://github.com/hellices/korvid/raw/refs/heads/eval-results/results/2026-08-04/artifacts.tar.gz)
- [metadata](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-04/metadata.json)
- [SHA-256 checksums](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-04/SHA256SUMS)

The long-lived `eval-results` branch is append-only and intentionally separate
from application source history.
