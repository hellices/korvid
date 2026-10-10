# Deployment Operation Outcome Tracker Design

**Issue:** [#405](https://github.com/hellices/korvid/issues/405)  
**Release:** v0.6.0  
**Status:** Approved for implementation

## Purpose

Korvid currently reports whether an approved Deployment scale or rollout
restart request reached the Kubernetes API. That fact does not establish that
the Deployment reached the requested replica count or completed the requested
rollout.

This feature starts a bounded, read-only tracker after an approved direct-TUI
Deployment scale or rollout restart succeeds. The tracker keeps the accepted
write and later observation separate, pins the exact cluster and Deployment
incarnation, and reports only conclusions supported by observed controller
state.

The first version covers direct TUI operations on `apps/Deployment` only. It
does not cover agent or MCP writes, StatefulSets, DaemonSets, ReplicaSets,
Helm operations, automatic remediation, rollback, or general application
health.

## Existing Foundations

- `ResourceWriteController` captures the context epoch, namespace, name, UID,
  and operation-specific scale or restart intent before approval.
- `WriteCoordinator` is the only approval, audit, and mutation perimeter. Its
  write result already distinguishes API acceptance from a blocked or failed
  mutation.
- `KubeClient.get_object` provides an authoritative GET with read telemetry.
- Relationship and Pulse code already contains defensive Kubernetes identity,
  condition, and evidence parsing patterns.
- `korvid.evals` includes offline operation scenarios and grading models. The
  TUI must not import that package; production behavior gets a smaller,
  independently tested model in `core/`.

## Considered Approaches

### 1. Core state model + UI controller + bounded Kubernetes reader

Add a pure state machine under `core/`, a read-only Kubernetes boundary under
`k8s/`, and a Textual lifecycle controller under `ui/`. The write gate emits a
typed accepted-operation receipt only after the success audit has persisted.

**Advantages**

- Preserves the current security perimeter.
- Keeps completion predicates independently testable.
- Pins context and object identity without relying on current table selection.
- Makes read budgets, cancellation, and shutdown explicit.
- Follows the repository's existing controller and constructor-injection
  architecture.

**Cost**

- Requires a new typed handoff through the write gate.
- Adds a dedicated screen and controller lifecycle.

### 2. Extend the session timeline into an outcome tracker

Append observation states to `SessionTimeline` and infer current progress from
timeline entries.

**Rejected:** the timeline is bounded, non-authoritative display state. It can
evict entries and intentionally cannot affect operation correctness. Making it
the tracker would conflate audit history with live authoritative observation.

### 3. Reuse `korvid.evals.operation_state` in production

Use the existing evaluation journal and outcome classifier directly.

**Rejected:** `evals/` is an offline source-checkout harness and is excluded
from the shipped TUI dependency graph. Its report-classification concerns are
larger than the production tracker and importing it would violate the layer
contract.

**Decision:** approach 1.

## Architecture

### Pure outcome model

Add `core/deployment_outcome.py`.

The module defines immutable, Textual-free types:

- `DeploymentOperationTarget`
  - context epoch and stable cluster identity
  - namespace, name, and Deployment UID
- `DeploymentScaleIntent`
  - requested replica count
  - accepted resource version and generation when available
- `DeploymentRestartIntent`
  - exact `restartedAt` value sent by the accepted patch
  - accepted generation and template hash/revision evidence when available
- `DeploymentObservation`
  - current Deployment UID, generation, observed generation
  - desired, current, updated, ready, and available replica counts
  - unavailable replica count and normalized relevant conditions
  - bounded affected-Pod evidence
- `DeploymentOutcome`
  - phase, summary, evidence lines, optional blocker, and navigation targets
- `DeploymentOutcomePhase`
  - `accepted`, `observing`, `completed`, `stalled`, `superseded`,
    `replaced`, `incomplete`, and `stopped`

Pure evaluator functions consume an intent and one observation. They never
perform I/O or infer success from elapsed time alone.

### Kubernetes read boundary

Add `k8s/deployment_outcomes.py` with an `abc.ABC` reader interface and a
concrete reader composed over `KubeClient`.

One snapshot performs:

1. an exact GET of the named Deployment;
2. UID comparison before any success decision;
3. extraction of Deployment generation, conditions, and replica status;
4. bounded collection of Pods selected by the Deployment;
5. filtering by the Deployment's current ReplicaSet ownership chain where
   identity evidence is available;
6. normalization into the core observation type.

Reads are namespace-scoped and capped. Pod evidence is limited to the small
set needed to explain non-ready progress. No Secret data, logs, or unbounded
cross-namespace scan is performed.

Reader errors remain typed:

- 404 after a previously accepted operation becomes `replaced` or
  `incomplete`, depending on available UID evidence;
- 403, timeout, disconnect, and unsupported evidence become `incomplete`;
- cancellation becomes `stopped`;
- malformed fields are evidence gaps, not synthetic zeroes.

### Accepted-write handoff

Extend `WriteGate.confirm` and `WriteCoordinator.confirm` with an optional
post-success callback factory. The callback receives a typed
`AcceptedWriteReceipt` after:

1. approval;
2. final revalidation;
3. intent audit persistence;
4. successful mutation;
5. success audit persistence.

The callback is observational only. It cannot affect the mutation result,
rewrite audit history, or run when the operation was cancelled, blocked, or
failed. Failure to start observation is reported separately as an incomplete
observation.

The receipt contains the action, target identity, context epoch, and accepted
completion timestamp. Operation-specific intent remains captured by
`ResourceWriteController`:

- scale supplies the requested replica count;
- restart supplies the exact annotation stamp used for preview and execution.

Only direct TUI `apps/Deployment` scale and restart flows install this callback
in the first version. Existing operations and all other kinds retain exactly
their current behavior.

### UI lifecycle controller

Add `ui/deployment_outcome_controller.py`.

The controller owns:

- a bounded registry of at most three trackers;
- one supervised Textual worker per active tracker;
- a fixed observation deadline of five minutes;
- polling with bounded backoff and an immediate first read;
- explicit stop, context-switch, unmount, and shutdown behavior;
- immutable snapshots consumed by the screen;
- notifications only on meaningful phase transitions.

The controller never owns a write handle and cannot pass the approval gate.
It receives only accepted receipts and a read-only Deployment outcome reader.

On a context switch, active trackers become `stopped` before the old client is
closed. They do not resume against the new cluster. A tracker also refuses any
observation whose captured epoch no longer matches.

Starting a fourth tracker stops and evicts the oldest active tracker with a
visible bounded-cap reason. Terminal trackers remain available until displaced
by the same cap.

### Outcome screen and navigation

Add `ui/widgets/deployment_outcome_screen.py`.

After a tracked write is accepted, Korvid opens the tracker screen. The user
may close the screen without stopping observation. The screen shows:

- accepted operation and exact target;
- requested scale count or restart marker;
- API acceptance separately from observation status;
- elapsed observation time;
- observed generation/revision and replica progress;
- controller conditions and a bounded blocker summary;
- whether evidence is partial or unavailable.

Actions:

- `Esc`: close the screen but keep observation running;
- `x`: stop observation explicitly;
- `r`: reopen/refresh the current snapshot without creating a new tracker;
- `enter`: navigate to the selected affected Pod;
- `e`: open that Pod's events;
- `d`: open its describe view;
- `l`: open logs.

Navigation carries namespace, name, and UID. The existing navigation routes
must revalidate the object identity before opening evidence. If no exact Pod
identity is available, the action is disabled instead of navigating by name
alone.

The latest tracker is also exposed through the Action Palette so a screen
closed with `Esc` can be reopened. No new globally reserved key is added in
v0.6.0.

## Completion Predicates

### Scale

Scale completes only when all available evidence agrees:

- live UID equals the accepted Deployment UID;
- `spec.replicas` equals the requested count;
- `status.observedGeneration >= metadata.generation`;
- `status.replicas`, `updatedReplicas`, `readyReplicas`, and
  `availableReplicas` equal the requested count;
- `unavailableReplicas` is absent or zero;
- no explicit `ReplicaFailure=True` or failed `Progressing` condition exists.

Scale-to-zero uses the same predicate with zero counts. Once the controller
has observed the current generation, omitted replica counters are normalized
to zero because Kubernetes commonly omits zero-valued JSON fields; present but
malformed counters remain unknown.

A different later `spec.replicas` becomes `superseded`, not failure or
success.

### Rollout restart

Restart completes only when:

- live UID equals the accepted Deployment UID;
- the pod template still contains the exact accepted `restartedAt` stamp;
- the controller has observed the resulting Deployment generation;
- updated, ready, and available replicas reach the desired count;
- unavailable replicas are absent or zero;
- no explicit rollout failure condition exists.

A different restart stamp or later generation that no longer contains the
accepted marker becomes `superseded`. A same-name Deployment with another UID
becomes `replaced`.

### Stalled and incomplete

`ProgressDeadlineExceeded`, `ReplicaFailure=True`, or concrete non-ready Pod
evidence can produce `stalled`. A deadline without positive failure evidence
produces `incomplete`, not `failed`.

RBAC denial, read timeout, lost connection, context change, missing fields, or
read-budget exhaustion can never produce `completed`.

Deployment convergence is labelled exactly as Deployment convergence. It does
not claim Service reachability, application health, or SLO success.

## Concurrency and persistence

Trackers are session-only and are not restored after restart. The accepted
write remains in the durable audit log; observation state is non-authoritative
and intentionally ephemeral.

Each tracker has an immutable ID. Late reads update a tracker only if its ID,
captured epoch, and target UID still match. Screen refreshes read immutable
snapshots, so a closed or replaced screen cannot receive stale mutation.

Observation workers are supervised by Textual and grouped separately from
writes. Shutdown cancels them without delaying or rewriting accepted writes.

## Error reporting

- Mutation errors retain existing notifications and audits.
- Observer startup errors say the write was accepted but verification could
  not start.
- Read errors say verification is incomplete and preserve the last confirmed
  evidence.
- UI rendering or navigation errors do not stop the tracker; they are reported
  as UI errors.
- No broad exception handler converts an unknown error into success.

## Testing

Implementation follows TDD.

### Core model

- scale up, scale down, no-op, and scale-to-zero completion;
- partial progress and missing status fields;
- exact restart marker and generation completion;
- explicit failure conditions and Pod blockers;
- replacement UID and superseding scale/restart;
- no false success from stale observed generation or partial readiness.

### Kubernetes reader

- exact Deployment GET and namespace-bounded related reads;
- owner-chain and UID filtering;
- caps and partial coverage;
- 403, 404, timeout, malformed payload, and cancellation behavior;
- no Secret or cross-namespace reads.

### Write handoff

- callback only after mutation and success audit;
- no callback on decline, approval expiry, precondition refusal, audit
  failure, API failure, or cancellation;
- observer failure cannot change the accepted write result;
- direct Deployment scale/restart only;
- exact scale target and restart stamp are carried.

### Controller and UI

- immediate read, bounded polling, deadline, stop, cap, and shutdown;
- context switch cancels old-cluster reads and late results are ignored;
- closing the screen keeps tracking; explicit stop does not;
- accepted and convergence states remain visually distinct;
- Pod navigation is disabled without exact identity and revalidated with UID;
- Action Palette reopening and help text;
- Textual pilot tests use `tests/ui/waits.py::until()`, never wall-clock
  assertions.

### Gates

- targeted pytest and ruff while iterating;
- strict mypy for changed boundaries;
- `tach check` for the new core/k8s/ui imports;
- full `make check` and `pre-commit run --all-files` before handoff.

## Documentation

Update:

- `docs/tui.md` with the tracker workflow and limitations;
- `docs/keybindings.md` only for modal-local keys;
- `docs/dev/capabilities.md` with implemented scope;
- `docs/release-notes/unreleased.md` with user-visible behavior;
- the v0.6.0 release tracker after implementation evidence is available.

## Non-goals

- automatic retry, rollback, scale correction, or remediation;
- replacing the Kubernetes Deployment controller's own status semantics;
- persistent incident history;
- simultaneous multi-cluster tracking;
- generic tracking for all workload kinds;
- agent-generated interpretation or mandatory LLM dependencies;
- changing approval, audit, protected-context, dry-run, or write permissions.
