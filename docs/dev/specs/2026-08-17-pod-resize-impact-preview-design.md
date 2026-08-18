# Pod resize impact preview (issue #300)

## Problem

Korvid's Pod resize confirmation shows the requested resource patch and the
API server's dry-run response, but it does not explain what an in-place resize
can do at runtime. It also omits the graph-derived impact section shown for
delete, rollout restart, and known workload scale-down because Pod resize has
no tested per-relation semantics yet.

A Pod resize differs from those actions:

- it mutates CPU and memory requests or limits on the existing Pod;
- it does not replace the Pod object or change its UID, IP, owner, volumes,
  configuration references, node placement, PDB membership, or Service
  membership;
- a container can restart when its `resizePolicy` requires it;
- a memory-limit decrease with `NotRequired` has only best-effort OOM
  avoidance;
- the kubelet can defer or reject a resize that the current node cannot
  satisfy, and the API server dry-run cannot predict that actuation result.

A non-empty graph relation set would therefore overstate the operation's
blast radius. An empty graph result by itself would also omit the Pod-local
runtime facts that matter to an approver. This slice defines both boundaries.

## Upstream facts

The design relies on these Kubernetes guarantees:

- In-place Pod resize changes desired CPU and memory resources without
  recreating the Pod.
  <https://kubernetes.io/docs/tasks/configure-pod-container/resize-container-resources/>
- `status.containerStatuses[*].resources` reports the resources currently
  applied by the runtime; the desired Pod spec can differ while a resize is
  pending or in progress.
- `PodResizePending` reports `Infeasible` or `Deferred`; the kubelet retries a
  deferred resize.
- `resizePolicy` defaults to `NotRequired`. `RestartContainer` requires a
  container restart when that resource changes, and a mixed CPU and memory
  request restarts when either changed resource requires it.
- A memory-limit decrease with `NotRequired` uses best-effort OOM avoidance,
  not a guarantee. The resize can remain in progress when current usage is
  above the requested limit.
- A resize cannot change the Pod's original QoS class, and the API server
  validates the class-specific request/limit constraints.

The preview does not predict kubelet capacity, actuation timing, current
memory usage, or the final resize condition.

## Alternatives considered

### Relationship graph only

Add `ImpactAction.POD_RESIZE` with an empty relation set and render the normal
graph summary.

This correctly refuses every relationship claim, but it does not tell the
approver about a required container restart, memory-limit reduction, or
deferred/infeasible execution. Rejected as incomplete.

### Action variants for every local outcome

Represent CPU-only, restart-required, memory-decrease, and mixed changes as
different `ImpactAction` members.

This keeps rendering simple but turns a graph-semantics enum into a
combinatorial encoding of Pod-local state. New resources or policies would
multiply the variants. Rejected.

### Empty graph policy plus a Pod-local classifier

Use one closed `POD_RESIZE` graph action and a separate deterministic
classifier for the already-captured Pod manifest and requested resources.
The graph states which relationships the action may claim about; the local
classifier states what the resource patch can do inside the Pod.

This is the selected approach. It keeps the graph boundary closed, produces
specific rather than unconditional warnings, and reuses data the write flow
already holds.

## Closed graph semantics

Add `ImpactAction.POD_RESIZE` with one shared empty frozenset used by both:

- `ACTION_RELATIONS[ImpactAction.POD_RESIZE]`;
- `ACTION_UNRESOLVED_RELATIONS[ImpactAction.POD_RESIZE]`.

The action follows none of the current `RelationKind` values:

- `owned_by` and `managed_by`: the Pod is not replaced and remains under the
  same controller;
- `selects` and `routes_to`: Pod identity, labels, IP, and membership are not
  changed by the resize request;
- `uses_volume`, `uses_config`, and `bound_to`: mounts, references, and
  bindings are unchanged;
- `protected_by`: a resize is not an Eviction API request;
- `scheduled_on`: the Pod remains on its current node; an infeasible resize is
  deferred or rejected rather than rescheduled by this operation.

Tests must pin every exclusion individually. The unresolved-reference policy
reuses the exact same empty frozenset object so the walk and warning policies
cannot drift.

The normal graph renderer remains conservative and bounded. Its zero direct
and transitive counts mean that this action deliberately walks no supported
relation, not that the cluster contains no related resources. The adjacent
Pod-local section states that boundary explicitly.

## Pod-local classification

Add a pure classifier in `korvid.core` that accepts:

- the already-fetched Pod manifest;
- the changed-resource mapping that will be sent to `pods/resize`.

It returns an immutable `ResizeImpactContext` containing only machine-defined
facts:

- whether CPU changed;
- whether memory requests changed;
- whether memory limits changed;
- whether any memory limit decreased;
- whether any changed resource has `RestartContainer`;
- whether a changed resource relies on the default or explicit
  `NotRequired` policy.

The classifier matches containers by exact name and considers only CPU and
memory requests and limits. It uses the existing
`korvid.k8s.models.parse_quantity` helper for numeric memory-limit comparison;
quantity strings are never compared lexically.

Malformed or missing manifest fragments do not become optimistic claims. The
classifier records that policy or direction could not be determined, and the
renderer emits the generic bounded limitation instead of guessing. It never
copies a container name, quantity, annotation, exception message, or other
cluster-derived string into the advisory.

The Pod-local renderer emits a bounded section with:

- the invariant that no graph relationship is traversed because the resize
  keeps the Pod object and its relationship membership;
- a restart-required line only when a changed resource has
  `RestartContainer`;
- a no-required-restart line only when every changed resource is known to use
  `NotRequired`;
- a memory-limit-decrease line only when numeric comparison proves a
  decrease;
- an indeterminate-policy or indeterminate-direction line when malformed or
  incomplete input prevents the corresponding classification;
- an unconditional limitation that node feasibility, deferred/infeasible
  status, actuation, and completion are not predicted.

These lines are advisory. They neither approve nor block the write and never
replace server dry-run validation.

## TUI data flow

The `R` keybinding flow captures the pane origin, context epoch, target
metadata, namespace, name, and exact UID before its first await.

The flow then:

1. performs the existing `patch pods/resize` permission pre-check;
2. fetches the Pod manifest and pre-fills `ResizePrompt`;
3. receives a non-empty changed-resource mapping;
4. runs the existing server dry-run with the exact UID and request body;
5. revalidates pane identity, pane scope, context epoch, selected resource,
   and UID before starting relationship work;
6. classifies the captured manifest and requested resources;
7. loads and renders `ImpactAction.POD_RESIZE` using the captured pane scope;
8. revalidates the same identity after the snapshot;
9. opens `ConfirmScreen` with the Pod-local section, graph section, and
   server dry-run preview;
10. revalidates exact identity through a synchronous approval guard before
    constructing the mutation coroutine.

The graph and Pod-local sections are composed deterministically. Pod-local
lines remain visible when no relationship loader or UID is available.

## Agent data flow

Agent-requested resize uses the same impact semantics and approval screen.
The existing target-manifest lookup supplies both the UID precondition and the
Pod-local classifier input.

An agent request names its target explicitly and is not raised from a resource
row. Its relationship snapshot therefore uses:

- the explicit target namespace for a namespaced Pod;
- all namespaces only for a future cluster-scoped action.

It never borrows the currently focused pane's scope. Add a scope-based impact
loader helper underneath the existing pane-origin wrapper so TUI writes retain
their race checks while agent resize can pass its explicit namespace.

Only agent resize gains impact lines in this slice. Delete, rollout restart,
and scale agent requests retain their current behavior.

## Failure and cancellation behavior

- A missing relationship loader or target UID omits the graph-derived
  section but retains the Pod-local section.
- A relationship timeout or unexpected loader, summarizer, or renderer error
  produces only sanitized machine-defined unavailable text plus the Pod-local
  section. Exception messages and response bodies never reach the dialog.
- `asyncio.CancelledError` propagates. No confirmation, reservation, write,
  or audit entry is created.
- A dry-run timeout or failure retains the existing fail-open approval
  behavior. It does not suppress safe impact notes.
- Pane, scope, context, selection, or UID drift before confirmation aborts the
  TUI flow with the existing notification pattern.
- TUI identity drift while the dialog is open fails the approval guard before
  the mutation coroutine is created.
- Agent decline, expiry, or cancellation creates no mutation or audit entry.
- Approval, UID preconditions, RBAC checks, write reservation, and fail-closed
  intent audit behavior remain unchanged.

## Testing

Implementation follows TDD.

### Core semantics

- Assert `POD_RESIZE` is present in both exhaustive action mappings.
- Parameterize all nine `RelationKind` values and prove none is traversed or
  reported unresolved.
- Assert the unresolved policy is the same empty frozenset object as the
  action relation policy.

### Pod-local classifier and renderer

- CPU-only and memory-only changes.
- Request increase/decrease and limit increase/decrease.
- Default, explicit `NotRequired`, and `RestartContainer` policies.
- Mixed resources and mixed containers.
- Equivalent Kubernetes quantities with different spellings.
- Missing containers, malformed resources, malformed policies, and invalid
  captured quantities.
- Stable machine-defined wording and per-line/section bounds.

### TUI flow and safety

- Real `R` key flow with graph plus Pod-local lines.
- Empty relation policy never renders a Service, controller, PDB, volume,
  ConfigMap, node, or binding as affected.
- Loader absent, UID absent, timeout, failure, and cancellation.
- Split-pane focus/scope drift, context switch, selection move, UID
  replacement/loss, and drift while the dialog is open.
- Permission denial, prompt cancel, dry-run failure, approval decline, and
  fail-closed audit failure.
- No reservation, write, or audit before the user's approval keystroke.

### Agent flow and safety

- Explicit namespace drives snapshot scope regardless of the active pane.
- The same Pod-local classification and empty graph semantics render.
- Decline, expiry, cancellation, graph failure, and audit failure.
- No mutation or audit before a real user keystroke.

### Documentation

Update `docs/tui.md` and `docs/resource-relationships.md` with the production
wording, action boundary, failure behavior, and scope rules. Update #293 when
#300 is complete.

## Out of scope

- Editing `resizePolicy`.
- VerticalPodAutoscaler integration.
- Predicting node capacity, current memory usage, or final kubelet outcome.
- Polling resize completion.
- Changing the dry-run patch format or write/audit contracts.
- Adding agent impact previews to delete, rollout restart, or scale.
