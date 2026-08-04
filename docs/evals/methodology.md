# Agent Evaluation Methodology

## Purpose

Korvid evaluates whether a locally installable model can use Kubernetes tools
to explore evidence, maintain conversational context, and reach a safe,
grounded conclusion. Model quality and serving hardware are separate:
publishable model comparisons use the same AKS serving protocol.

## Three Evaluation Tiers

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

The current pack has three journeys. It is an initial benchmark, not the final
eight-journey coverage target tracked in #176.

### 3. Live AKS journeys

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
