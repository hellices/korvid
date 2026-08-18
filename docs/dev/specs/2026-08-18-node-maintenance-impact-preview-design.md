# Node maintenance impact previews (issue #293)

## Problem

Korvid supports three approval-gated Node operations:

- cordon marks a Node unschedulable;
- uncordon makes it schedulable again;
- drain cordons the Node, then evicts a precomputed set of Pods.

The confirmations already include server dry-run output for cordon/uncordon and
a PDB-aware `DrainPlan` for drain. They do not use the relationship graph
because these actions have no closed, tested per-relation semantics.

The three operations must not share one plausible-looking impact policy:

- cordon and uncordon do not move or evict current Pods;
- drain directly affects eligible Pods currently scheduled on the Node;
- a PodDisruptionBudget can govern an eviction, but the existing drain plan is
  more authoritative than a graph edge because it contains current
  `disruptionsAllowed`, skip reasons, and exact eviction targets.

This slice defines the graph boundary, preserves the drain plan as the execution
contract, and adds identity revalidation around the new awaited work.

## Kubernetes facts and limits

The design relies on these facts:

- cordon patches `Node.spec.unschedulable=true`; existing Pods remain running;
- uncordon clears that flag; it permits future placement but does not move
  existing Pods;
- drain uses the Eviction API after cordoning;
- mirror Pods and DaemonSet-controlled Pods are not eviction targets;
- `emptyDir` data can be lost when a Pod is evicted;
- a PDB with no disruptions available can reject an eviction with HTTP 429;
- workloads normally recreate eligible Pods elsewhere, but successful
  rescheduling, capacity, readiness, and application availability are not
  guaranteed by the drain request.

The preview does not predict scheduler placement, replacement readiness,
eviction completion time, workload health, or whether capacity exists
elsewhere.

## Alternatives considered

### Generic graph summary for all three actions

Give cordon, uncordon, and drain normal graph snapshots.

Rejected. Empty cordon/uncordon relation sets would pay full snapshot latency
to produce a deterministic empty result, and could imply that current
relationships were inspected for effects that these actions do not have.

### Drain plan only

Keep cordon/uncordon unchanged and treat `DrainPlan` as the complete impact
preview.

Rejected. The plan is authoritative for execution, but this would leave the
relationship policy open and would not record which graph facts drain may
conservatively describe.

### Closed graph semantics plus the existing drain plan

Define all three actions in `ImpactAction`. Cordon and uncordon have empty graph
sets and use machine-defined local notes without loading a graph. Drain follows
only Node placement, while `DrainPlan` remains the sole source of eviction
targets and PDB blockers.

Selected. It closes every graph claim, avoids pointless reads for empty
actions, and does not duplicate drain execution logic.

## Closed graph semantics

Add:

- `ImpactAction.CORDON_NODE`;
- `ImpactAction.UNCORDON_NODE`;
- `ImpactAction.DRAIN_NODE`.

Cordon and uncordon share one empty frozenset for both their traversal and
unresolved-reference policies. They follow none of the current relation kinds:

- `scheduled_on`: current placement does not change;
- `protected_by`: neither action sends an eviction;
- `owned_by` and `managed_by`: ownership does not change;
- `selects` and `routes_to`: labels, endpoints, and routing membership are not
  mutated;
- `uses_volume`, `uses_config`, and `bound_to`: references, mounts, and storage
  bindings remain unchanged.

Drain follows exactly:

- `scheduled_on`: a Pod currently placed on the Node is operational context for
  the drain.

The graph includes every observed Pod on the Node, including mirror and
DaemonSet Pods that the plan skips. It does not classify eviction eligibility.
The adjacent plan is definitive about which Pods will be evicted.

Drain excludes:

- `owned_by` and `managed_by`: controllers are not deleted or mutated;
- `selects` and `routes_to`: Services and routes are not changed by the drain
  request, and endpoint availability is not predicted;
- `uses_volume`, `uses_config`, and `bound_to`: references and bindings remain;
  the design does not claim that a replacement Pod can mount successfully.
- `protected_by`: the graph's dependent walk cannot reach a PDB from a Node
  through this edge direction, and the PDB-aware `DrainPlan` already provides
  the authoritative blocker state.

`ACTION_UNRESOLVED_RELATIONS[DRAIN_NODE]` reuses the exact single-relation drain
frozenset. Every included and excluded relation receives a dedicated test.

## Machine-defined local notes

Add a pure node-maintenance renderer with bounded, constant-only lines.

Cordon states:

- current Pods are not evicted or moved;
- the Node is marked unschedulable for ordinary workload placement;
- workload availability and future placement are not predicted.

Uncordon states:

- current Pods are not moved;
- future scheduling to the Node is permitted;
- scheduler choice and capacity are not predicted.

Drain states:

- the drain plan, not the graph, defines exact eviction targets and skip
  reasons;
- after the Node is successfully cordoned, it remains cordoned if drain
  execution later fails or is cancelled;
- replacement placement, readiness, and application availability are not
  predicted.

No Node name, Pod name, label, annotation, exception message, or manifest text
enters these lines.

## DrainPlan remains authoritative

The existing `DrainPlan` continues to determine:

- eligible eviction targets;
- mirror and DaemonSet skips;
- `emptyDir` warnings;
- current PDB blockers;
- the approved target set rechecked after cordoning.

The graph section is advisory context only. It cannot:

- add a Pod to the eviction set;
- remove a plan target;
- override a PDB blocker;
- approve, reserve, or execute a write.

If the graph and plan differ, execution follows the plan. The dialog labels the
sections separately:

- `drain impact plan:` for the existing execution preview;
- `graph-derived impact (advisory):` for bounded relationship evidence;
- `Node maintenance impact (advisory):` for static operational limits.

## Cordon and uncordon data flow

The keypress captures pane origin, context epoch, Node name, and exact UID
before the first await.

The flow:

1. performs the existing permission check;
2. runs the existing server dry-run;
3. revalidates pane, scope, context, selection, and UID;
4. composes the action-specific local note without calling the relationship
   loader;
5. opens the standard confirmation dialog;
6. revalidates the same identity through an approval guard before constructing
   the write coroutine;
7. executes through the existing fail-closed audit path.

No graph loader, namespace listing, or relationship snapshot is needed for an
empty action policy.

## Drain data flow

The drain keypress captures pane origin, context epoch, Node name, and UID.

The flow:

1. performs the existing drain permission precheck;
2. computes the current `DrainPlan`;
3. revalidates pane, scope, context, selection, and UID;
4. loads the relationship snapshot using the captured origin's impact scope;
   because Node is cluster-scoped, the existing scope resolver expands this to
   all namespaces so Pods on the Node are not omitted;
5. summarizes `DRAIN_NODE`;
6. composes graph and Node-maintenance advisory lines;
7. revalidates exact identity after graph loading;
8. opens the typed-name confirmation with the unchanged drain plan preview;
9. revalidates exact identity in the confirmation callback;
10. starts the existing `DrainController` with the approved plan.

The controller's post-cordon plan recheck and audit lifecycle remain unchanged.

## Failure and cancellation behavior

- Permission denial retains the existing actionable refusal.
- Cordon/uncordon dry-run failure continues to omit only the dry-run section.
- Drain-plan failure remains fail-closed: no confirmation, cordon, eviction, or
  audit success record is created.
- A missing relationship loader or UID removes the drain graph section but
  retains the drain plan and local notes.
- Graph timeout or unexpected failure produces the existing machine-defined
  unavailable advisory and retains the drain plan.
- Graph exceptions are logged by type only; response and manifest text never
  reach the dialog.
- Cancellation during plan or graph loading creates no confirmation,
  reservation, write, or audit entry.
- Cancellation after drain execution starts keeps the existing contract: no
  further evictions are issued and the Node remains cordoned.
- Any pane, scope, context, selection, or UID drift during an awaited phase
  aborts before confirmation.
- Drift while confirmation is open prevents execution.
- Audit intent failure remains fail-closed for cordon, uncordon, and drain.

## Rendering and bounds

The drain plan keeps its existing limits. Graph lines keep the existing impact
caps and Unicode-safe truncation. Node-maintenance lines are constant-only and
bounded to the confirmation modal.

The graph can mention only resources produced by the relationship renderer.
The local renderer never interpolates cluster-derived strings.

## Testing

### Core graph contracts

- every `RelationKind` is explicitly included or excluded for each new action;
- cordon and uncordon traversal and unresolved policies reuse the same empty
  set;
- drain follows only `scheduled_on`;
- unrelated and unresolved references cannot leak into the affected set.

### Cordon and uncordon

- graph loader is never called;
- action-specific local wording is rendered;
- current Pods are never described as moved or evicted;
- permission, dry-run, decline, expiry, cancellation, context, pane, scope,
  selection, UID, and audit failures preserve existing behavior;
- approval-time UID drift dispatches no write.

### Drain

- graph and drain-plan sections are both present and ordered;
- `DrainPlan.targets` remains the exact execution set;
- DaemonSet, mirror, `emptyDir`, and PDB cases retain current plan behavior;
- the graph may list DaemonSet and mirror Pods, while the adjacent plan
  explicitly skips them and remains authoritative;
- graph failure or absence retains plan and local notes;
- plan failure never falls back to graph-only approval;
- loader timeout and cancellation create no confirmation or reservation;
- pane, scope, context, selection, and UID drift are tested during both plan
  and graph awaits and while confirmation is open;
- post-cordon plan growth still aborts with the Node cordoned;
- no relationship outside `scheduled_on` is rendered as affected.

### Regression gates

- delete, rollout restart, scale-down, and Pod resize semantics remain
  unchanged;
- no agent write surface is added;
- all fakes retain exact `WriteOps` signatures;
- randomized test order and full repository gates pass.

## Documentation

Update:

- the TUI guide with the three confirmation sections and limitations;
- the relationship guide with the new closed action matrix;
- keybinding/help text only if user-visible wording changes.

## Out of scope

- adding agent cordon, uncordon, or drain tools;
- predicting replacement placement, capacity, readiness, or application health;
- replacing `DrainPlan` with graph traversal;
- changing eviction retries, concurrency, timeout, or cancellation semantics;
- changing DaemonSet, mirror Pod, `emptyDir`, or PDB classification;
- caching or moving graph summarization off the event loop;
- display-cell-width-aware truncation work tracked separately.
