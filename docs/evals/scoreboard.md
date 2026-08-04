# Local Model Agent Scoreboard

## Current Results

| Model | Personal-device tier | Task | Offline conversation | Live AKS journey | Malformed / stale | Usability verdict |
|---|---|---:|---:|---:|---:|---|
| Qwen3 8B | 16GB Mac / 8GB VRAM Windows | 20/23 (87%, one run) | rerun required | rerun required | — | candidate |
| Qwen3-Coder 30B-A3B | 32GB Mac / 16GB VRAM Windows | **59/69 (85.5%)** | rerun required | rerun required | — | Task A; conversation pending |
| Qwen3 1.7B | 8GB Mac / CPU/iGPU Windows | 4/6 smoke | rerun required | rerun required | — | limited candidate |

Task and conversation denominators differ intentionally. Task scores measure
fault diagnosis; conversation scores measure complete multi-turn journeys.
Offline v2 grades each checkpoint from calls made in that turn only; earlier
v1 results incorrectly credited prior-turn evidence and are superseded. Review
then exposed that the triage checkpoint mentioned both candidates without
requiring an explicit priority. The fixture now requires `checkout` first, so
both offline and live conversation rates must be regenerated after merge before
any conversational recommendation is published.
Qwen3 8B's task row is a single run and the conversation pack currently has
three journeys, so the recommendation remains provisional until #176 completes
three task repetitions and expands the pack to eight journeys.

Qwen3-Coder 30B-A3B task repetitions were `20/23`, `19/23`, and `20/23`
(`87.0%`, `82.6%`, `87.0%`). Mean success was `85.5%`; population variance
was `4.20 percentage-points²` (standard deviation `2.05pp`). Its Task A grade
therefore has the required three-run dispersion evidence.

## Detailed Findings

### Qwen3 8B (superseded exploratory conversation runs)

Strengths:

- preserved namespace and target identity across all corrected live runs;
- broad listing found both real broken Pods and the healthy control;
- obeyed “payments, not checkout” without stale calls;
- used events to identify the nonexistent image;
- emitted `open_describe` on every final live turn;
- malformed calls and safety violations remained zero.

Weaknesses:

- offline `healthy-stop` passed 0/3 because the final checkpoint did not perform
  its declared verification read (one run also hedged the initial health claim);
- offline `logs-to-events` passed 0/3: it reused prior evidence or fetched a
  manifest instead of making the requested events pivot;
- offline `triage-and-correct` passed 3/3 with turn-local evidence.

Live representative sequence:

```text
turn 1: list_resources(pods, namespace) -> finds checkout, payments, search
turn 2: get_events(payments-1) -> image tag not found / ImagePullBackOff
turn 3: open_describe(payments-1) -> visible evidence and corrective action
```

Verdict: candidate for post-merge rerun; no current conversational grade.

### Qwen3-Coder 30B-A3B (superseded exploratory conversation runs)

Strengths:

- highest task depth: 59/69 across 23 scenarios ×3;
- no malformed calls across 124 task calls;
- fast MoE CPU inference relative to dense 30B models.

Conversation weaknesses:

- `healthy-stop` passed 0/3 due unnecessary follow-up diagnosis or no final
  verification read;
- `logs-to-events` passed 2/3;
- `triage-and-correct` passed 0/3 due over-exploration and missed checkpoint
  evidence;
- after “payments, not checkout,” one live run called checkout again;
- one live run described both faults but failed to state the requested payments
  ImagePullBackOff cause;
- another exhausted the six-iteration budget during broad exploration.

Verdict: useful as a task-oriented diagnostic model, but currently less usable
than Qwen3 8B for interactive correction and concise exploration.

### Qwen3 1.7B (superseded exploratory conversation runs)

Strengths:

- fits the 8GB personal-device tier;
- task smoke fetched all declared evidence with no malformed calls;
- one live run completed all three turns correctly.

Weaknesses:

- one malformed call appeared in the nine offline journey runs;
- often skipped the required evidence tool on follow-up turns;
- sometimes lost the requested payments focus even without making a stale tool
  call;
- only 1/3 offline triage journeys and 1/3 real-cluster journeys completed.

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
- [turn-local conversation rerun archive](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-04-r2-artifacts.tar.gz)
- [turn-local rerun metadata](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-04-r2-metadata.json)
- [initial compressed raw artifacts](https://github.com/hellices/korvid/raw/refs/heads/eval-results/results/2026-08-04/artifacts.tar.gz)
- [metadata](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-04/metadata.json)
- [SHA-256 checksums](https://github.com/hellices/korvid/blob/eval-results/results/2026-08-04/SHA256SUMS)

The long-lived `eval-results` branch is append-only and intentionally separate
from application source history.
