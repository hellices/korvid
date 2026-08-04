# Evaluation Scenario Catalog

## Task Diagnostics

| Scenario | Ground truth | Main behavior checked |
|---|---|---|
| `bad-command-crash` | invalid executable/command | inspect crash state and avoid OOM/image guesses |
| `crashloop-app-panic` | application panic | retrieve previous logs and identify code failure |
| `crashloop-dependency-unreachable` | dependency connection refusal | correlate logs with dependency failure |
| `crashloop-missing-env` | missing required configuration | distinguish configuration from application crash |
| `healthy-deployment` | no fault | verify availability and stop |
| `healthy-restart-history` | recovered Pod | distinguish historical restarts from a current failure |
| `healthy-service-endpoints` | no fault | verify Service endpoints without inventing latency faults |
| `image-pull-auth` | registry authentication | distinguish credentials from image typo |
| `image-pull-typo` | nonexistent image/tag | identify manifest/tag failure |
| `init-container-failing` | failed init container | inspect init state rather than main container |
| `job-backoff-limit-exceeded` | Job exhausted retries | cite BackoffLimitExceeded |
| `liveness-probe-failing` | kubelet probe kill | do not infer OOM from exit 137 alone |
| `missing-configmap-mount` | missing ConfigMap | follow Pod reference to missing object |
| `missing-secret-env` | missing Secret | identify exact referenced Secret |
| `node-pressure-eviction` | node memory pressure | distinguish node eviction from container OOM |
| `oom-killed` | memory limit exceeded | cite OOMKilled and exit 137 |
| `pending-insufficient-cpu` | insufficient CPU | interpret FailedScheduling evidence |
| `pending-node-selector` | selector/label mismatch | compare requested and available labels |
| `pvc-pending-no-storageclass` | missing StorageClass | follow Pod to PVC provisioning event |
| `quota-blocked-scheduling` | ResourceQuota exceeded | distinguish quota from capacity shortage |
| `readiness-probe-failing` | readiness failure | distinguish unready from restarting |
| `service-selector-mismatch` | Service selects no Pods | compare selector and labels |
| `stuck-rollout` | bad image blocks rollout | traverse Deployment → ReplicaSet → Pod |

Every task fixture contains Kubernetes-shaped objects, events, log tails,
required answer groups, forbidden misdiagnoses, and expected evidence targets.

## Offline Conversation Journeys

### `triage-and-correct`

1. Broadly list namespace Pods and identify both checkout and payments.
2. User redirects focus: “payments, not checkout.”
3. Diagnose the payments image authentication failure without calling checkout.
4. Open the payments describe view and summarize the next action.

Checks broad discovery, prioritization, user correction, namespace/name memory,
stale-target avoidance, and UI intent.

### `logs-to-events`

1. Diagnose a restarting gateway whose logs look normal.
2. User explicitly asks for non-log evidence.
3. Fetch events and explain the liveness-probe/kubelet behavior without claiming
   OOM.

Checks evidence-channel pivoting and resistance to exit-code anchoring.

### `healthy-stop`

1. Explore a namespace containing two healthy Pods.
2. User asks whether further investigation is needed.
3. Confirm health and stop within the call budget.

Checks negative-control behavior and unnecessary exploration.

## Live AKS Journey

### `live-triage-and-correct`

The journey mirrors `triage-and-correct` but reads actual Pod status and events
from the dedicated contract cluster:

1. discover `checkout-1`, `payments-1`, and `search-1`;
2. accept the user's correction to payments;
3. identify the nonexistent image and ImagePullBackOff;
4. call `open_describe` for payments;
5. avoid stale checkout calls after the correction.

The initial failed Qwen3 8B run exposed a harness defect: live discovery mapped
the `pods` alias to `metrics.k8s.io/PodMetrics`, returning only the healthy Pod.
The regression test now verifies that core/v1 Pods win alias collisions via the
same `build_alias_map()` helper used by the application.
