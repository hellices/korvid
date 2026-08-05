# Local Model Agent Scoreboard

## Current Results

These are post-merge results from `main` revision `124b1aa` (PR #185), using
the same AKS/Ollama serving protocol for every model.

| Model | Personal-device tier | Task diagnosis | Evidence fetched | Offline conversation | Live AKS journey | Runtime / safety | Usability verdict |
|---|---|---:|---:|---:|---:|---|---|
| Qwen3 8B | 16GB Mac / 8GB VRAM Windows | **61/69 (88.4%)** | 58/69 (84.1%) | **1/9** | 0/3 | errors 0, malformed 0, safety 0 | best task model tested; conversation not reliable |
| Qwen3-Coder 30B-A3B | 32GB Mac / 16GB VRAM Windows | 56/69 (81.2%) | **61/69 (88.4%)** | **2/9** | 0/3 | errors 11, malformed 1, wrong namespace 2, safety 0 | strong evidence retrieval; over-explores |
| Qwen3 1.7B | 8GB Mac / CPU/iGPU Windows | 50/69 (72.5%) | 58/69 (84.1%) | 0/9 | 0/3 | errors 2, malformed 3, safety 0 | narrow/simple use only |

Task and conversation denominators differ intentionally. Task diagnosis scores
count runs whose answer contains every required claim and no forbidden claim
across 23 scenarios ×3; evidence retrieval is the independent adjacent column.
Conversation scores require every checkpoint in a complete multi-turn journey
to pass. Runtime/safety totals combine task, offline, and live runs; Coder's 11
errors are nine iteration-limit turns/runs plus two live iteration-limit turns.
All models maintained zero successful write/safety violations.

Task repetition scores:

- Qwen3 8B: `20/23`, `19/23`, `22/23` (population standard
  deviation `5.42pp`);
- Qwen3-Coder 30B-A3B: `18/23`, `18/23`, `20/23` (population standard
  deviation `4.10pp`);
- Qwen3 1.7B: `16/23`, `18/23`, `16/23` (population standard
  deviation `4.10pp`).

The conversation pack currently has three offline journeys and one live journey,
so #176 still tracks expansion to eight journeys. The current result is already
enough to reject a general conversational recommendation: none of the tested
models passed the strengthened real-cluster journey.

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
