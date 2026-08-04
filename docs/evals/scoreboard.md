# Local Model Agent Scoreboard

## Current Results

| Model | Personal-device tier | Task | Offline conversation | Live AKS journey | Malformed / stale | Usability verdict |
|---|---|---:|---:|---:|---:|---|
| **Qwen3 8B** | 16GB Mac / 8GB VRAM Windows | 20/23 (87%, one run) | **6/9 (66.7%)** | **3/3 (100%)** | 0 / 0 | **Recommended default** |
| Qwen3-Coder 30B-A3B | 32GB Mac / 16GB VRAM Windows | **59/69 (85.5%)** | 5/9 (55.6%) | **0/3** | 0 / 1 | Task-strong, not recommended for interactive exploration yet |
| Qwen3 1.7B | 8GB Mac / CPU/iGPU Windows | 4/6 smoke | 3/9 (33.3%) | 1/3 (33.3%) | 3 offline / 0 live | Limited fallback for simple reads |

Task and conversation denominators differ intentionally. Task scores measure
fault diagnosis; conversation scores measure complete multi-turn journeys.

## Detailed Findings

### Qwen3 8B

Strengths:

- preserved namespace and target identity across all corrected live runs;
- broad listing found both real broken Pods and the healthy control;
- obeyed “payments, not checkout” without stale calls;
- used events to identify the nonexistent image;
- emitted `open_describe` on every final live turn;
- malformed calls and safety violations remained zero.

Weaknesses:

- offline `logs-to-events` passed only 1/3: it sometimes reused existing
  diagnosis rather than explicitly fetching events, or omitted the kubelet
  explanation;
- offline `triage-and-correct` passed 2/3 because one run fetched a manifest
  instead of the declared diagnostic evidence.

Live representative sequence:

```text
turn 1: list_resources(pods, namespace) -> finds checkout, payments, search
turn 2: get_events(payments-1) -> image tag not found / ImagePullBackOff
turn 3: open_describe(payments-1) -> visible evidence and corrective action
```

Verdict: usable for conversational Kubernetes exploration in the tested scope.
It is the first model in this matrix with both strong task results and a 3/3
real-cluster correction journey.

### Qwen3-Coder 30B-A3B

Strengths:

- highest task depth: 59/69 across 23 scenarios ×3;
- no malformed calls across 124 task calls;
- fast MoE CPU inference relative to dense 30B models.

Conversation weaknesses:

- over-explored simple turns and exceeded call budgets;
- after “payments, not checkout,” one live run called checkout again;
- one live run described both faults but failed to state the requested payments
  ImagePullBackOff cause;
- another exhausted the six-iteration budget during broad exploration.

Verdict: useful as a task-oriented diagnostic model, but currently less usable
than Qwen3 8B for interactive correction and concise exploration.

### Qwen3 1.7B

Strengths:

- fits the 8GB personal-device tier;
- task smoke fetched all declared evidence with no malformed calls;
- one live run completed all three turns correctly.

Weaknesses:

- offline malformed calls appeared in 3 of 9 journeys;
- often skipped the required evidence tool on follow-up turns;
- sometimes lost the requested payments focus even without making a stale tool
  call;
- only 1/3 real-cluster journeys completed.

Verdict: usable for simple listing and narrow inspection, not reliable as the
default conversational agent.

## Real-Cluster Validation

The live result used actual Kubernetes failure states, not fake responses:

- `checkout-1`: CreateContainerConfigError from a missing ConfigMap;
- `payments-1`: ImagePullBackOff from a nonexistent image;
- `search-1`: healthy Running Pod.

The namespace was uniquely labelled, deleted after evaluation, and the
dedicated cluster returned to Stopped. The model-serving `modeleval` pool also
returned to zero nodes.

## Raw Results

Generated files are kept off the source branch:

- [2026-08-04 artifact directory](https://github.com/hellices/korvid/tree/eval-results/results/2026-08-04)
- [compressed raw artifacts](https://github.com/hellices/korvid/raw/refs/heads/eval-results/results/2026-08-04/artifacts.tar.gz)
- [metadata](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-04/metadata.json)
- [SHA-256 checksums](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-04/SHA256SUMS)

The long-lived `eval-results` branch is append-only and intentionally separate
from application source history.
