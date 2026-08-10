# Local Model Agent Scoreboard

## Current Results — 2026-08-10 matrix

Six models on the task pack ×3, run on `main` revision `bdfb645` under the
standard AKS/Ollama protocol (`Standard_D32s_v5`, `small` profile,
`prompts.source: default`).

**Six models measured on 2026-08-10.** The three models from the 2026-08-05
campaign are listed separately below and are **not comparable**: they were
measured before `diagnose_service` and `diagnose_pvc` existed (#213, #216), so
they ran on a **14-tool** surface against this campaign's **16**. Tool-surface
size is exactly the variable #221 exists to measure, so the two sets cannot be
ranked against each other.

The 23-scenario column re-scores this campaign on the pre-#227 scenario set; it
controls for the scenario change but not for the tool change.

| Model | Tier | Task (23 scen.) | Evidence | Journeys | Malformed | Writes | Safety | Wall |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **Qwen3 4B** | 16GB Mac / 8GB VRAM | **65/69 (94.2%)** | 63/75 | — | 0 | 0 | 0 | 100 min |
| **Qwen3 32B** | 32GB Mac / 16GB VRAM | **62/69 (89.9%)** | 63/75 | — | 0 | 2 | 0 | 367 min |
| **Qwen3 30B-A3B** | 32GB Mac / 16GB VRAM | **60/69 (87.0%)** | 63/75 | — | 0 | 0 | 0 | 101 min |
| Qwen3 14B | 24GB Mac / 12GB VRAM | 58/69 (84.1%) | 57/75 | — | 0 | 0 | 0 | 203 min |
| Devstral 24B | 32GB Mac / 16GB VRAM | 47/69 (68.1%) | 52/75 | — | 1 | 0 | 0 | 42 min |
| Mistral Small 3.1 | 32GB Mac / 16GB VRAM | 40/69 (58.0%) | 40/75* | — | 6 | 1 | 0 | 92 min |

All rows above: `main` revision `bdfb645`, 16-tool surface, evidence
denominator 75. The journey column is empty because the 2026-08-10 journey pack
was run against the three older models instead; those results are below.

\* Mistral Small fetched 58/75 evidence against 40/75 accuracy — the widest gap
measured; see below.

On the current 25-scenario pack the 08-10 models score: Qwen3 4B 70/75
(93.3%), Qwen3 32B 67/75 (89.3%), Qwen3 30B-A3B 66/75 (88.0%), Qwen3 14B 61/75
(81.3%), Devstral 50/75 (66.7%), Mistral Small 40/75 (53.3%).

### What the numbers say

**Parameter count does not predict accuracy.** Qwen3 4B (2.5 GB) tops the pack,
above the 32B, the 30B and the 30B-Coder. It fetches the *same* evidence as the
top three (63/75) and does it in **100 minutes against the 32B's 367** — 3.7×
faster for a higher score. Size is not the lever here.

**No model reliably reports a healthy Service.** On `healthy-service-endpoints`
the best result across six models is 2/3, and three models score 0/3:

| Model | `healthy-service-endpoints` |
|---|---|
| Qwen3 32B, Qwen3 30B-A3B, Devstral 24B | **0/3** |
| Qwen3 4B, Mistral Small 3.1 | 1/3 |
| Qwen3 14B | 2/3 |

The same shape appears independently in the journeys (`healthy-stop` fails for
both the 30B and the 8B) and in `pvc-wait-for-first-consumer`, where the 14B
scores 1/3 and Mistral Small 0/3 by inventing a provisioning fault. Note that
`healthy-restart-history` is 3/3 for four models, so this is not "healthy
scenarios are hard" — it is specific to concluding a *Service* is fine.

For a TUI beside an on-call engineer this is the more damaging error: a false
alarm on a healthy cluster costs trust faster than a missed nuance.

**Safety held, and it was tested.** Across 450 task runs and 21 journey turns:
**3 write attempts, 0 safety violations.** Mistral Small reached for a mutation
unprompted mid-diagnosis and the gate refused it.

**A 6-scenario smoke screen does not predict the full pack.** Mistral Small
passed smoke at 4/6 with malformed=0, then scored 58.0% with 6 malformed calls
on the full pack. Promotion gates must not be used to skip it.

**Evidence retrieval and diagnosis are separable.** Mistral Small fetches 58/75
but scores 40/75 — the widest gap measured. It reaches the right data and draws
the wrong conclusion, which tool or prompt changes are unlikely to fix.

### Operational notes for anyone re-running this

- **Raise the `ollama` memory limit first.** It ships at 10 GiB; the 30B models
  OOMKill at that size on a 120 GiB node and the symptom presents as
  `Server disconnected` / connection errors, not as memory pressure. This
  campaign used 100 GiB / 28 CPU.
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

## 2026-08-05 matrix — supporting detail

Scores from that campaign are merged into the table above. Detail that the
2026-08-10 run did not reproduce is kept here.

Task repetition scores (population standard deviation):

- Qwen3 8B: `20/23`, `19/23`, `22/23` (`5.42pp`);
- Qwen3-Coder 30B-A3B: `18/23`, `18/23`, `20/23` (`4.10pp`);
- Qwen3 1.7B: `16/23`, `18/23`, `16/23` (`4.10pp`).

Offline conversation used a 9-checkpoint pack (Qwen3 8B `1/9`, Coder `2/9`,
1.7B `0/9`) and a live AKS journey that **all three models failed 0/3**. That
result stands: no model has yet passed the strengthened real-cluster journey.

Runtime detail: Coder's 11 errors were nine iteration-limit turns/runs plus two
live iteration-limit turns; it also made 2 wrong-namespace calls.

## Artifact and Operational Detail

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

## Detailed Findings

### Qwen3 8B

Strengths:

- highest repeated task diagnosis score: 61/69;
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

### Qwen3-Coder 30B-A3B

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

### Qwen3 1.7B

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

## Real-Cluster Validation

The live result used actual Kubernetes failure states, not fake responses:

- `checkout-1`: CreateContainerConfigError from a missing ConfigMap;
- `payments-1`: ImagePullBackOff from a nonexistent image;
- `search-1`: healthy Running Pod.

The namespace was uniquely labelled, deleted after evaluation, and the
dedicated cluster returned to Stopped. The model-serving `modeleval` pool also
returned to zero nodes. The post-merge live namespace was
`korvid-agent-eval-124b1aa`.

## Raw Results

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
