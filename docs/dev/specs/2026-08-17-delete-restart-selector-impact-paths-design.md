# Delete/Restart Selector Impact Paths

**Issue:** [#297](https://github.com/hellices/korvid/issues/297)
**Status:** Approved for implementation
**Date:** 2026-08-17

## Problem

The delete and rollout-restart impact-flow fixtures omit the
`spec.selector` facts that production summaries emit for Deployments and
ReplicaSets. They therefore document this topology:

```text
Deployment <-owned_by- ReplicaSet <-owned_by- Pod
```

Production builds a richer graph:

```text
Deployment <-managed_by- Pod
Deployment <-owned_by- ReplicaSet
ReplicaSet <-managed_by- Pod
ReplicaSet <-owned_by- Pod
```

For either `DELETE` or `ROLLOUT_RESTART`, the bounded breadth-first walk
reaches the Pod and ReplicaSet directly from the Deployment. The two later
ReplicaSet-to-Pod edges are revisits because the Pod is already listed.
Current fixtures instead show the Pod as a transitive dependent and report
only one additional path.

## Evidence and Root Cause

`korvid.k8s.relationship_facts` emits a `MANAGED_BY` selector fact for every
supported workload with a non-empty `spec.selector`. The graph reverses that
declaration into a `Pod -> workload` edge while retaining the workload as the
evidence resource.

The UI fixture helpers make those selector facts opt-in solely to preserve the
pre-scale-down delete/restart assertions. Scale-down tests opt in; the default
delete/restart environment does not. The discrepancy is therefore in the
fixture and documentation, not the production graph builder or impact walk.

The Kubernetes documentation treats the two facts as related but distinct:

- a ReplicaSet selector identifies Pods it can acquire;
- a Pod `metadata.ownerReferences` records its current owning ReplicaSet;
- a Deployment provides declarative updates for both ReplicaSets and Pods.

Selector and owner evidence must not be collapsed merely because both paths
reach the same Pod.

## Chosen Semantics

Keep the production graph and impact traversal unchanged.

The deterministic breadth-first policy remains:

1. each resource is listed once using the first shortest path that reaches it;
2. one-hop dependents are direct;
3. later edges into an already reached resource are counted as additional
   known paths;
4. distinct selector and owner-reference evidence remains distinct.

For a realistic Deployment, ReplicaSet, and Pod snapshot, both delete and
rollout restart therefore report:

```text
known direct dependents: 2
  Pod via Deployment spec.selector (managed_by)
  ReplicaSet via ReplicaSet ownerReference (owned_by)
known transitive dependents: none in this snapshot
additional known paths: 2
```

Incomplete snapshot coverage continues to render counts as lower bounds
(`2 or more`) rather than exact values.

## Alternatives Rejected

### Prefer owner-reference paths

Making the Pod transitive through the ReplicaSet would hide the shorter,
declared `Deployment -> Pod` selector path and contradict the documented
definition of direct as one hop. It would also require action-specific
traversal ordering or filtering that diverges from the relationship graph's
cycle/revisit classification.

### Deduplicate selector and owner paths

Selectors can match orphaned or adoptable Pods and can overlap with other
controllers. An owner reference describes current ownership. Treating these
facts as semantically interchangeable would discard real evidence and make
the advisory less conservative.

## Implementation

### Fixtures

Make `_deployment` and `_replicaset` in
`tests/ui/test_impact_flow.py` always carry their production
`spec.selector` facts. Remove the scale-down-only opt-in switch and update
call sites.

### Core regression

Add a realistic Deployment/ReplicaSet/Pod topology test in
`tests/core/test_impact.py`, parameterized for delete and rollout restart. It
must pin:

- direct ordering: Pod, then ReplicaSet;
- no transitive duplicate;
- two deterministic revisits, preserving the ReplicaSet selector and Pod
  owner-reference evidence separately.

### Flow regression

Update the delete and rollout-restart end-to-end assertions to pin the same
production-shaped result from snapshot loading through rendered confirmation
text. Keep the Pod-delete assertion that `SELECTS` is excluded.

### Documentation

Synchronize:

- `docs/tui.md`;
- `docs/resource-relationships.md`;
- the authoritative #283 implementation plan.

The documentation must explain why the shortest selector path is displayed
and why the owner/ReplicaSet alternatives remain counted rather than rendered
as duplicate dependents.

## Safety and Error Handling

No application source, graph relation set, write flow, approval gate, RBAC
check, UID precondition, dry-run behavior, audit behavior, timeout handling,
or cancellation behavior changes.

The change only corrects tests and documentation to exercise the graph that
production already builds. If the realistic regression produces a different
shape, implementation stops and the root-cause analysis is revisited rather
than changing traversal semantics to satisfy the expected text.

## Verification

Implementation uses a RED/GREEN cycle:

1. add the production-shaped core and flow expectations;
2. verify they fail while the fixtures still omit selectors;
3. make selectors unconditional in the fixtures;
4. verify the targeted core and UI suites pass;
5. run Ruff, formatting, mypy, tach, and the full test suite before the PR.
