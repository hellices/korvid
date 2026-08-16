# Workload scale-down impact preview (issue #295)

## Problem

Korvid's delete and rollout-restart confirmations show a bounded,
graph-derived advisory. Scale confirmations do not. A scale-down can remove
controller-managed Pods, change Service and EndpointSlice routing, and affect
Ingress or Gateway paths, but the existing dialog shows only the replica
change.

Scale is not one uniform operation:

- scale-up does not remove an existing Pod;
- scale-down deletes surplus Pods through the workload controller;
- the current replica count can be absent from a summary;
- a HorizontalPodAutoscaler can later overwrite a manual replica count;
- StatefulSet PVC retention can turn a scale-down into a data-deletion event.

A generic `scale` relation set would therefore overstate what the snapshot
knows. The preview must activate only for the semantic case it can defend.

## Upstream facts

The design relies on these Kubernetes guarantees:

- Scaling a Deployment does not itself trigger a rollout; only a Pod template
  change does.
  <https://kubernetes.io/docs/concepts/workloads/controllers/deployment/>
- Workload controllers delete surplus Pods directly. PodDisruptionBudgets
  constrain API-initiated Eviction requests, not controller scale-down.
  <https://kubernetes.io/docs/concepts/workloads/pods/disruptions/>
  <https://kubernetes.io/docs/concepts/scheduling-eviction/api-eviction/>
- EndpointSlice readiness and termination state describe which observed Pod
  endpoints remain eligible for traffic.
  <https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/>
- StatefulSet scale-down follows ordinal guarantees, while PVC deletion
  depends on `persistentVolumeClaimRetentionPolicy`.
  <https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/>
- HorizontalPodAutoscaler reconciles the target replica count independently
  of a manual scale request.
  <https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/>

The preview does not attempt to reproduce controller choice, endpoint
readiness, autoscaler reconciliation, or PVC retention logic.

## Alternatives considered

### Full scale analyzer

Fetch the target manifest and relevant live status, calculate the exact Pods
that a controller is likely to remove, count ready endpoints per Service,
detect HPA targets, and evaluate StatefulSet PVC retention.

This would produce the richest warning, but it needs new graph edges, status
data, kind-specific controller logic, and additional privileged reads. It is
too large to review as the first extension of the write-preview boundary.
Rejected for this slice.

### Drain before scale

Drain has high incident value and already computes a PDB-aware plan. Adding a
graph advisory now would create two competing impact surfaces and require a
new composition contract between them. Cordon also belongs with that design
because it changes scheduling without evicting existing Pods. Deferred to a
separate slice.

### Bounded scale-down advisory

Reuse the immutable relationship snapshot and conservative renderer only when
the current replica count is known and the requested count is lower. List
controller and routing resources that may be affected, without predicting
which Pod is deleted or whether traffic fails. This is the selected approach.

## Activation boundary

Add `ImpactAction.SCALE_DOWN`, not a generic `SCALE`.

The scale flow requests an impact snapshot only when:

1. the selected resource is already accepted by the existing scalable-kind
   gate (Deployment, ReplicaSet, or StatefulSet);
2. the selected summary carries a current desired replica count; and
3. the requested count is lower than the captured current count.

Scale-up, no-op, and unknown-current-count confirmations preserve the existing
behavior and perform no relationship snapshot LISTs. This is an intentional
semantic omission, not an unavailable preview.

The base confirmation continues to show the captured `old -> new` replica
change. The graph advisory uses the action label `scale down`.

## Closed relation set

`ImpactAction.SCALE_DOWN` traverses exactly:

- `owned_by`: controller ownership chains, including Deployment to ReplicaSet
  to Pod;
- `managed_by`: workload selectors to matching Pods;
- `selects`: Services whose selectors match candidate Pods;
- `routes_to`: observed EndpointSlices targeting candidate Pods and declared
  Ingress or Gateway routes targeting reached Services.

The graph direction remains dependent to dependency, and the impact walk
continues to traverse it in reverse from the scale target.

This relation set deliberately excludes:

- `protected_by`: a PDB does not gate controller scale-down;
- `uses_volume`, `bound_to`: the current reverse-dependent walk cannot decide
  StatefulSet PVC retention;
- `uses_config`: scaling does not alter referenced ConfigMaps or Secrets;
- `scheduled_on`: scale-down is not a Node operation.

Shared selectors are expected. A Service or EndpointSlice is rendered only as
a known dependent that **may be affected**. The preview never states that a
Service will have zero endpoints, that every selected Pod belongs exclusively
to this workload, or that availability will fail.

`ImpactLimits.max_depth` (3, shared with delete and rollout restart) bounds
this walk the same way it bounds theirs, and the routing chain a scale-down
follows fits inside it. A workload declares the selector that binds its own
Pods, so `spec.selector` yields a `managed_by` edge from every matched Pod
to the workload: the reverse walk reaches those Pods in one hop, beside (not
through) the ReplicaSet the ownerReferences chain gives it. A Deployment's
routing chain to its Ingress is therefore `Deployment -> Pod (managed_by) ->
Service (selects) -> Ingress (routes_to)` — three hops, inside the cap, and
the Ingress is named. The ReplicaSet is a direct dependent in its own right,
and the further routes to that same Pod — the Pod's owner reference and the
ReplicaSet's own selector — are folded into `additional known paths` rather
than expanded a second time. Scaling that ReplicaSet reaches the same chain
through the selector it declares itself (`ReplicaSet -> Pod (managed_by) ->
Service (selects) -> Ingress (routes_to)`), also three hops, with its Pods'
owner references folded into `additional known paths` the same way.

A scale-down composes `managed_by`/`selects`/`routes_to` into one chain, so
where the target sits along that chain decides whether the chain still fits
inside `ImpactLimits.max_depth`: a workload reaches a route through its own
Pods and the Service in front of them, and a longer chain (an extra owner
level, a route reached through a further hop) is reported by `traversal
capped`, never silently dropped. The cap itself (`ImpactLimits.max_depth =
3`) is not changed by this slice.

## Action-specific limitations

The scale-down advisory includes machine-defined text, before the dependent
sections:

- controller scale-down is not an Eviction API request, so
  PodDisruptionBudgets do not gate it;
- HorizontalPodAutoscaler targeting and reconciliation are not evaluated;
- for an `apps/StatefulSet` target only, PVC retention policy is not
  evaluated. The pair selects the line, not the kind alone:
  `persistentVolumeClaimRetentionPolicy` is an `apps` API field, so a CRD
  that names its own kind `StatefulSet` has no such policy to leave
  unchecked and must not be told it has one.

These are static boundaries, not cluster-derived claims. They are shown only
for `ImpactAction.SCALE_DOWN`. They remain bounded and literal like every other
impact line.

## UI and data flow

The action captures `_WriteOrigin`, target metadata, namespace, name, UID,
context epoch, kind alias, and current replica count before opening
`ReplicasPrompt`.

The flow performs **five** scale-context gates. Every gate re-checks the
context epoch, focused pane identity and scope, selected resource identity,
exact UID, and the desired replica count captured with the target:

1. after the permission check and **before** `ReplicasPrompt` opens, so a
   prompt is never raised for a row the user has already left;
2. after `ReplicasPrompt` and **before** the server-side dry-run, so a count
   change or focus move while the prompt was open costs no API round trip;
3. after the server-side dry-run preview and the managed-resource note, and
   **before** any impact LIST, so a doomed scale never spends the snapshot
   fan-out nor scopes it to a pane the user has left;
4. after the impact summary and **before** `ConfirmScreen` is mounted;
5. after the user approves at `ConfirmScreen` and **before** the write worker
   exists — so before the write reservation, the intent audit record, and the
   operation factory. `ConfirmScreen` is the flow's longest awaited gap: it
   stays open until the user answers, and everything gate 4 compared can move
   while it does. The gate runs only on a fresh keystroke approval, so it can
   only refuse an approval the user gave and never becomes a second path to
   the write, and it is deferred by one event-loop iteration because Textual
   invokes a screen's result callback before it pops the dismissed screen.

A same-UID change to `spec.replicas` is drift too: it can turn the requested
count from a decrease into an increase or make the displayed `old -> new`
statement stale. The flow cancels with a phase-specific message instead of
silently reclassifying the action.

So the worker, after the user chooses a replica count:

1. runs gate 2;
2. obtains the existing server-side dry-run preview;
3. obtains the existing managed-resource note;
4. runs gate 3;
5. when the activation boundary is met, loads the relationship snapshot
   using the captured origin scope and summarizes `SCALE_DOWN`;
6. runs gate 4, which is performed only for a known decrease — the one
   awaited gap it guards is the snapshot load;
7. opens `ConfirmScreen` with the dry-run, managed note, and optional impact
   lines;
8. on approval, runs gate 5 and only then launches the write.

A scale that is not a known decrease therefore has gates 1, 2, 3, and 5 and
no impact LIST at all: it keeps the pre-#295 absence of a snapshot fan-out
while gaining the stronger gates, which previously re-checked only
kind, namespace, name and the context epoch. Only gate 4 is conditional on
the decrease classification, because the snapshot load is the only awaited
gap it guards.

No await occurs between the final identity/origin gate and mounting the
confirmation screen, nor between gate 5 and the write. The write still runs
only through the existing approval, reservation, UID-precondition, audit, and
operation path.

## Failure and cancellation behavior

The impact section remains advisory:

- snapshot timeout, loader failure, or render failure produces the existing
  static unavailable advisory and does not bypass approval;
- `asyncio.CancelledError` propagates rather than becoming an unavailable
  preview;
- origin, context, selection, or UID drift cancels before confirmation, and
  drift that only lands while the confirmation is open cancels at gate 5,
  after the dialog is dismissed and before anything is reserved or written;
- cancellation or drift creates no confirmation, write reservation, write
  operation, or audit entry — gate 5's refusal comes after a confirmation
  the user already saw and answered, so it leaves the same nothing behind
  except that dialog;
- RBAC denial and dry-run failure retain their existing behavior;
- audit failure remains fail-closed.

No new per-node GET fan-out is added. Snapshot and render caps remain those
shipped by issue #283.

## Testing

Core tests pin the exact action mapping:

- the four included relations are traversed for `SCALE_DOWN`;
- excluded PDB, config, volume, binding, and node edges produce no claim,
  resolved or dangling: a scale-down's `unresolved references in the
  affected set` warning is filtered by the same closed set its walk is, so
  an excluded relation cannot re-enter the advisory as a warning;
- a dangling reference of an *included* relation (a stale EndpointSlice
  `targetRef`, say) is still warned about for a scale-down;
- delete and rollout restart keep the relation-blind warning: the delete
  removes what those references resolved against, and the restart recreates
  the Pod that has to satisfy them again;
- existing delete and restart semantics remain unchanged.

Renderer tests pin:

- the `scale down` action label;
- the PDB non-gating line and HPA/PVC limitation line;
- deterministic order, literal rendering, Unicode flattening, fragment and
  line bounds, evidence resource plus field, and inferred-marker preservation.

Textual flow and security tests cover:

- scale-down shows direct and transitive candidate dependents;
- shared-selector Services remain conservative `may be affected` entries;
- scale-up, no-op, and unknown-current-count paths make no snapshot calls;
- origin-pane focus and scope changes across the prompt and impact load;
- context change, selection change, UID replacement, and UID loss;
- identity, pane, scope, and replica-count drift landing while
  `ConfirmScreen` is open, refused at gate 5 on approval;
- the replica-count gates before the dry run and before the impact LIST
  refusing an increase as well as a decrease, since neither is conditional
  on the decrease classification;
- timeout and loader/render failure;
- external and loader-raised cancellation;
- approval decline, RBAC denial, dry-run failure, and audit failure;
- no confirmation, reservation, operation, or audit entry after cancellation
  or identity drift — and no reservation, operation, or audit entry when the
  drift is only caught at gate 5.

## Documentation

Update the TUI write-preview documentation and the graph-derived impact plan
with the activation boundary, relation set, action-specific limitations, and
unsupported scale cases. Update issue #293's checklist when issue #295 ships.

## Out of scope

- Exact endpoint deltas or zero-backend claims.
- Predicting which ReplicaSet or StatefulSet Pod is deleted.
- HPA `scaleTargetRef` graph edges or reconciliation timing.
- StatefulSet PVC deletion or retention prediction.
- Scale-up quota, capacity, or scheduling prediction.
- Edit, Pod resize, cordon, drain, Helm, or OLM impact semantics.
