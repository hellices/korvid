# Local Model AKS Evaluation Design

## Goal

Evaluate models that individuals can install and use routinely on
Apple-silicon MacBooks or consumer Windows PCs under one standardized AKS
serving protocol. Publish task-diagnostic
and conversational-exploration scores separately, assign practical grades, and
recommend models by MacBook unified-memory tier.

## Scope

The model is the comparison unit. Developer laptop versus AKS is not a
leaderboard dimension. Laptop runs may guide iteration, but a model receives a
published score only after it runs on the shared AKS evaluation environment.

The primary matrix covers practical personal-device tiers:

| Mac unified memory | Windows baseline | Candidate class |
|---:|---|---|
| 8GB | CPU/iGPU, 8GB system RAM | 0.6B–3B; 4B only with short context |
| 16GB | 8GB VRAM and 16GB+ system RAM | 4B–8B |
| 24GB | 12GB VRAM and 24GB+ system RAM | 8B–14B |
| 32GB | 16GB VRAM and 32GB+ system RAM | 14B–24B |
| 64GB | 24GB VRAM and 48GB+ system RAM | 30B MoE or 32B dense |

The tiers are practical minimum recommendations, not claims that the model
weights are the only memory consumer. Ollama, KV cache, context length, and the
operating system need headroom.

Primary candidates are Qwen3 0.6B/1.7B/4B/8B/14B/30B-A3B, Llama 3.2
1B/3B, Granite 3.2 2B, Phi-4 Mini, Llama 3.1 8B, Mistral Small 3.1,
Devstral, and Qwen3-Coder 30B-A3B. The 8GB smoke result promotes Qwen3
1.7B (4/6 with all evidence fetched) and conditionally Qwen3 4B when context
is kept short. Qwen3 0.6B, Llama 3.2 1B/3B, Granite 3.2 2B, and Phi-4 Mini
do not reliably complete evidence-driven tool tasks in the current profile.
Qwen3 32B is an enthusiast reference. Llama 3.3 70B is excluded from
personal-device recommendations and retained only as a server-class comparison.

## Evaluation Strategy

Use a two-stage promotion gate.

### Stage 1: smoke matrix

Every candidate runs:

- six task scenarios covering pod failure, scheduling, storage, rollout,
  healthy negative control, and safety-sensitive behavior;
- three conversational journeys covering broad discovery, user correction,
  and UI-follow/evidence narrowing;
- one repetition after an explicit warm-up request.

A candidate advances when it achieves all of:

- at least 4/6 task scenarios;
- at least 2/3 conversational journeys;
- zero malformed calls;
- zero safety violations;
- native structured tool calls that the runtime can parse.

### Stage 2: publishable matrix

Promoted candidates run:

- all 23 task scenarios, three repetitions;
- the initial three conversational journeys, three repetitions;
- the same profile, prompt SHA, scenario SHA, serving engine, quantization,
  context length, and warm-up procedure.

Raw JSON records every run. Generated documentation contains means and
variance; it does not copy ad hoc numbers from issue comments.

The initial three-journey pack validates broad triage/correction, evidence
pivoting, and healthy stopping. Expanding it to eight journeys (including
ownership traversal, cross-namespace comparison, and RBAC-limited exploration)
remains follow-up work tracked by #176; scores in this PR are an initial,
explicitly partial conversation benchmark.

## Conversational Journey Evaluation

Task scenarios remain one-turn diagnostic checks. A separate journey format
models the interactive product:

1. broad namespace triage and candidate discovery;
2. compare abnormalities and prioritize one;
3. correct the target mid-investigation;
4. preserve name/namespace identity across turns;
5. pivot from logs to events or manifests when logs are insufficient;
6. traverse workload ownership;
7. mirror evidence in the TUI;
8. verify a healthy environment and stop.

Each journey uses deterministic fake-cluster state and scripted user turns.
Checkpoints grade evidence, target identity, conversational pivots, redundant
calls, clarification quality, TUI-follow behavior, and the terminal answer.
Safety and malformed-call invariants remain identical to task evaluation.

## Grades

| Grade | Task success | Conversation success | Required invariants |
|---|---:|---:|---|
| S | at least 90% | at least 80% | malformed below 1%; safety violations 0 |
| A | at least 80% | at least 70% | malformed below 1%; safety violations 0 |
| B | at least 65% | at least 55% | malformed below 1%; safety violations 0 |
| C | below B | or below B | no safety exception |

A model with a safety violation is ungraded and not recommended regardless of
answer quality.

## AKS Serving Environment

Use the existing `aks-shared-runners` cluster and `ollama/ollama` deployment,
**pinned to a released tag rather than `:latest`** — a floating tag makes the
version that produced a published row a coincidence (#235). The manifest is
checked in at `deploy/eval/ollama.yaml`.

- Expand `ollama-models` from 30GiB to 200GiB. The `default` StorageClass uses
  Azure Disk CSI and supports online expansion.
- Add a retained zone-2 `Standard_D32s_v5` Spot user node pool named
  `modeleval`. Zone 2 is required by the existing model PVC.
- Keep the node-pool resource after the evaluation track. Scale it to one node
  during runs and zero while idle; do not delete it after each run.
- Bind Ollama to the current `modeleval` node explicitly while evaluating.
  The cluster uses Node Auto Provisioning, whose scheduler rejected values
  from a static node-pool selector during the initial experiment.
- Restore Ollama to its original scheduling and CPU/memory settings before
  scaling the evaluation pool to zero.
- Use `kubectl port-forward`; the existing public LoadBalancer is not reachable
  from the development network.

GPU T4 SKUs are present in Korea Central, but the subscription's
`Standard NCASv3_T4 Family` quota is zero and an automatic quota request
returned `QuotaNotAvailableForResource`. A future approved GPU quota replaces
the CPU pool without changing the scoring protocol.

## Model Storage

The 200GiB PVC can retain the initial matrix. Pull candidates serially and
record the resolved Ollama digest and model-layer size. If total storage
approaches 85%, remove only candidates that failed the smoke gate; retain
published and pending candidates.

## Scoreboard

The canonical table contains:

- model and resolved digest;
- parameter class and quantization;
- Ollama layer size;
- practical MacBook unified-memory tier;
- task score and variance;
- conversation score and variance;
- evidence, malformed, safety, calls, tokens, and latency;
- grade and recommendation.

The environment appears once in the evaluation-protocol header, not as a row
dimension.

## Error Handling

- A cold-load timeout is not a model-quality failure. Warm-up is mandatory and
  timed separately.
- A model that cannot emit parseable native tool calls fails the smoke gate
  with the reason recorded.
- Spot eviction marks the run invalid and resumable; it does not count as a
  model failure.
- Endpoint, port-forward, or node failures produce nonzero runner exit status
  and never create a publishable row.

## Acceptance Criteria

- All listed candidates receive a smoke result on AKS.
- Every published task grade uses three task-pack runs.
- Initial conversation results use three journeys × three repetitions and stay
  labelled partial until #176 expands the pack to eight journeys.
- Results include practical 8GB, 16GB, 24GB, 32GB, and 64GB MacBook tiers and
  corresponding consumer Windows VRAM/system-memory guidance.
- The node pool remains available and can scale to zero after evaluation.
- The scoreboard is generated from raw results and issue #176 is its single
  tracking location.
