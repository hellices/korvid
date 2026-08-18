# Operational resource relationships

Select a resource and press `g` to see what it depends on and what depends on
it — owner references, label selectors, config/volume mounts, routing
backends, node scheduling, and storage bindings, joined from the resources
korvid has already listed. It answers "what breaks if I delete this?" and
"why is this Pod using that ConfigMap?" without a second tool.

This is a **read-only, metadata-only** view: it never issues a write, and no
Secret `data`/`stringData`, container `command`/`args`, or literal
environment variable value is ever retained in a relationship fact, the
graph, or anything the screen renders. See [Secret safety](#secret-safety)
below for what that guarantee does and does not cover.

## Opening the view

With a resource selected in any discovered (non-synthetic) table, press `g`.
Korvid resolves the exact root identity from the current view's discovered
API group/kind plus the selected row's namespace, name, and UID, then runs a
bounded set of LISTs (see [Coverage](#coverage)) and opens the relationship
screen over the result.

`g` is unavailable for korvid-invented views that have no backing API
resource (for example the Helm release browser) — there is nothing a graph
LIST could ever fetch for them, and korvid says so rather than opening an
empty or misleading screen.

## Reading the two panes

The screen splits the root resource's edges into two sections:

- **Dependencies** — what the root resource declares or exhibits a
  relationship *to*. Deleting one of these can break the root.
- **Dependents** — what declares or exhibits a relationship *to* the root.
  Deleting the root can break one of these.

Every edge is **directed dependent → dependency**: the *subject* is the
resource that declares or exhibits the relationship (the dependent) and the
*target* is the resource it references or matches (the dependency). A
Dependencies row's target is the root's dependency; a Dependents row's
subject is the thing depending on the root. This direction is fixed
regardless of which pane a given relation kind tends to show up in — for
example a ReplicaSet is *owned by* (dependent on) its Deployment, so
`owned_by` edges appear as a Dependents row on the Deployment.

Each row shows six columns:

| Column | Meaning |
|---|---|
| Direction | Which pane/expansion depth the row belongs to |
| Relation | The relationship kind (below) |
| Resource | The other end of the edge (target for Dependencies, subject for Dependents), as `group/kind/namespace/name` |
| Confidence | How the fact was derived (below) |
| State | Resolution state (Dependencies) or navigability (Dependents) |
| Evidence | The exact manifest field path the fact came from, e.g. `spec.template.spec.volumes[0].configMap` |

### Relation kinds

| Relation | Subject (dependent) | Target (dependency) |
|---|---|---|
| `owned_by` | Any owned object | The owning object (`metadata.ownerReferences`) |
| `selects` | Service | The Pods its `spec.selector` matches |
| `managed_by` | Pod | The Deployment/ReplicaSet/StatefulSet/DaemonSet/Job whose `spec.selector` matches it |
| `protected_by` | Pod | The PodDisruptionBudget whose `spec.selector` matches it |
| `routes_to` | Ingress / Gateway API Route (HTTPRoute, GRPCRoute, TLSRoute, TCPRoute, UDPRoute) | The backend Service it routes to |
| `routes_to` | EndpointSlice | The Pod named by one `endpoints[].targetRef` (live routing target, not authored config) |
| `uses_volume` | Pod / workload template | A PersistentVolumeClaim mounted as a volume |
| `uses_config` | Pod / workload template / Ingress | A ConfigMap or Secret referenced by name only (`envFrom`, `env[].valueFrom` — across regular, init, and ephemeral containers — a volume, a projected volume source, `imagePullSecrets`, or an Ingress TLS `secretName`) |
| `scheduled_on` | Pod | The Node named in its (live) `spec.nodeName`. Only a live Pod is ever scheduled: a workload's `spec.template.spec.nodeName` is template configuration, not an observed placement of the Deployment/Job itself, so it produces no edge |
| `bound_to` | PersistentVolumeClaim / PersistentVolume | The bound PersistentVolume / PersistentVolumeClaim on the other side (a PV's `spec.claimRef` UID is kept, so a stale binding is not reattached to a recreated claim of the same name) |

Direction reflects **operational** dependency, not which object's manifest
holds the selector: a Service's `spec.selector` names Pods, and the Service
is the dependent (`selects`, Service → Pod), because the Service's routing
depends on those Pods existing. A Deployment's `spec.selector` also names
Pods, but there the *Pod* is the dependent (`managed_by`, Pod → Deployment),
because a Pod created by that Deployment cannot outlive it operationally —
the same is true for a PodDisruptionBudget's selector (`protected_by`,
Pod → PodDisruptionBudget). The Evidence field always points at whichever
manifest actually declared the selector, regardless of which side is the
edge's subject.

### Confidence

- **`declared`** — read directly from a manifest's `spec` (owner references,
  selectors, volumes, config references, routing backends).
- **`observed`** — read from live cluster state rather than authored config:
  an EndpointSlice's `targetRef` (current routing target) and a Pod's
  `nodeName` (current scheduling decision).
- **`inferred`** — reserved for facts derived by graph-building heuristics
  rather than read directly; the current relation catalog above does not use
  it, but the model exists so a future addition (e.g. a heuristic join) does
  not need a new confidence level.

### Resolution (Dependencies rows)

A Dependencies row's **State** column is the edge's resolution against the
resources korvid actually listed:

- **`resolved`** — the target was found among the listed resources; the row
  navigates there on Enter.
- **`missing`** — no listed resource matches the target's identity (by UID
  when the reference carried one, otherwise by name). The row is shown, not
  hidden, and cannot be navigated.
- **`invalid`** — the reference names a different namespace than the subject
  and is not authorized by an exact Gateway `ReferenceGrant` match (see
  [Cross-namespace routing](#cross-namespace-routing-referencegrant)). Not
  navigable.

Dependents rows do not carry `resolution` (it only ever describes an edge's
*target*, and a Dependents row navigates the *subject*, which is always a
resource korvid already listed). Their State column instead reads `resolved`
or `not indexed` depending on whether that subject is still present in the
graph's own node set — it is always present for a freshly built snapshot,
but stays correct if a screen is ever reused across snapshots.

### Direct vs. bounded expansion

By default the screen shows only the root's **direct** dependencies and
dependents — one hop. Press `d` to toggle a **bounded transitive expansion**
of dependents: a breadth-first walk outward from the root, capped at
`max_depth = 5` hops and `max_nodes = 500` visited resources (shared across
direct rows, expanded rows, cycle rows, and repeat rows by one counter, so
no single category can silently grow the table past the cap on its own).
Deeper rows are labelled with their hop count (`Dependents (depth 2)`, etc.).

No resource is ever walked twice. An edge that reaches a resource the walk
already reached is classified rather than traversed again, and the two
outcomes mean very different things:

- **`Dependents (cycle)`** — a genuine loop: the dependent is an *ancestor*
  of the resource it depends on, along the very path the walk took to get
  there (including the root itself, or a resource that references itself).
  A Deployment → ReplicaSet → Pod chain that loops back to the Deployment
  terminates safely here rather than looping forever.
- **`Dependents (repeat)`** — the same resource reached again by a
  different edge that is *not* a loop: two independent paths converging on
  one dependent (a diamond — say a Pod owned by one ReplicaSet and selected
  by another object that also depends on the root), or two distinct
  relationships between the same pair of resources (for example a Pod both
  owned by and mounting config from the same object). The relationship is
  real and is shown, but nothing about it forms a cycle, and the resource
  is not expanded a second time.

Both row kinds are informational: they name a resource that already appears
elsewhere in the table, so they are not navigation targets. When any row
category is capped, a trailing `(capped)` row explains how many rows were
omitted — counted separately for direct, deeper, cycle, and repeat rows —
and why.

## Coverage

Before it can join any facts, korvid LISTs a fixed catalog of resource kinds
(Pods, Services, ConfigMaps, Secrets, PVCs, PVs, Nodes, Deployments,
ReplicaSets, StatefulSets, DaemonSets, Jobs, CronJobs, EndpointSlices,
Ingresses, PodDisruptionBudgets) plus every Gateway API `*Route` and
`ReferenceGrant` the cluster's own API discovery reports. Bare Gateway objects
are not automatic sources because listener relationships are outside this
slice; a selected Gateway root is still listed for its universal owner facts.
Every discovered `*Route` kind is read through the one
`spec.rules[].backendRefs[]` shape the Gateway API defines for all of them, so
HTTPRoute, GRPCRoute, and the stream routes (TLSRoute/TCPRoute/UDPRoute) all
contribute `routes_to` edges rather than being listed and silently ignored. A
CRD outside that group whose kind merely
ends in `Route` (for example an OpenShift `Route`) is neither listed nor
interpreted. Route `parentRefs` (the Gateway a Route attaches to) are not
modelled. A second, bounded phase then LISTs the namespaces those results'
`routes_to` references explicitly named (see [Cross-namespace
routing](#cross-namespace-routing-referencegrant)); each of those LISTs
records its own coverage, scoped to the namespace it ran in. Coverage uses
six states:

- **`complete`** — the LIST succeeded.
- **`partial`** — a target namespace was intentionally sampled only for the
  referenced kind(s) and `ReferenceGrant`, not for the full catalog.
- **`forbidden`** — the LIST returned 403; RBAC denies this account that
  resource kind.
- **`unavailable`** — the resource kind (or the whole Gateway API group) was
  never discovered on this cluster, or a Gateway API LIST returned 404/405 —
  an absent optional CRD, not a permission problem.
- **`failed`** — the LIST raised any other API error or a network/transport
  error.
- **`capped`** — the number of input resources in a named source, the
  number of joined edges, or the number of cross-namespace follow-up LISTs
  exceeded a hard limit and the excess was dropped deterministically (see
  [Limits](#limits)).

Press `c` to toggle coverage detail: the namespace each non-`complete`
record was scoped to (shown as `core/services @prod: forbidden`, so the
same resource denied in two namespaces is tellable apart — a scope-less
record such as a graph-wide cap keeps its concise form) plus the
underlying error/skip text (sanitized: control characters flattened,
capped at 512 characters, so a pathological RBAC/API message cannot break
the display or grow the snapshot unboundedly).

### Why an incomplete graph cannot prove no dependency

**A graph built from incomplete coverage can only prove a dependency
*exists*, never that one does not.** If the Secret LIST is `forbidden`, a
Pod's `uses_config` edge to a Secret that was never listed will render as
`missing` — but that means "not found among what korvid could see," not "does
not exist." Absence of an edge, or a `missing`/`invalid` resolution, is never
sufficient grounds to conclude no relationship exists once any coverage
record is not `complete`. The screen's top line always states whether
coverage as a whole is `complete` or `incomplete` for exactly this reason —
treat a `missing` edge under incomplete coverage as "unknown," not "absent."

### When the selected resource is not in the snapshot

The root itself can be absent from a snapshot even when coverage is
`complete`: the object was deleted or recreated with a new UID after the
table's row was read. With a positive resource limit, the loader prioritizes
the root's source and the exact selected object before sharing the remaining
budget across sources. Dependencies and Dependents are empty when the root is
still absent because the snapshot has nothing about it — not because the
cluster has nothing. The banner says so literally ("This resource is not
present in this snapshot …") instead of letting two empty sections read as
"no relationships."

## Stale owner references

Kubernetes owner references (and the EndpointSlice `targetRef`, and any other
reference that carries a `uid`) are resolved by **UID first**: if a target
reference names a UID, korvid only accepts a match against a currently
listed resource with that exact UID — it never falls back to matching by
name when a UID was declared and does not match anything currently listed.

This means **recreating a Deployment with the same name produces a new
UID**, and any ReplicaSet still carrying the *old* Deployment's UID in its
`ownerReferences` resolves as `missing`, explained as "no observed resource
has uid `<old-uid>`" — it is never silently reattached to the new Deployment
just because the name matches. The same protection covers a
PersistentVolume's `spec.claimRef`, which carries the bound claim's UID: a
PV still pointing at a deleted PVC stays `missing` instead of appearing
bound to a replacement claim that merely reuses the name. A resource is only
resolved by name when its reference carries no UID at all (some reference
shapes, like a ConfigMap `envFrom`, never carry one).

## Cross-namespace routing (`ReferenceGrant`)

A `routes_to` reference (an Ingress or Gateway API Route backend) that names a
different namespace than its subject is **invalid by default** —
cross-namespace routing is denied unless an exact Gateway API `ReferenceGrant`
in the *target* namespace authorizes it. "Exact" means every one of these must
match, with no wildcard or partial match honored:

- the `ReferenceGrant`'s `from.group`/`from.kind`/`from.namespace` match the
  subject's group, kind, and namespace, **and**
- the `ReferenceGrant`'s `to.group`/`to.kind` match the target's group and
  kind, **and**
- the `ReferenceGrant`'s `to.name`, *when it names one*, matches the target's
  name exactly. An omitted `to.name` means "every object of that group/kind in
  this namespace", which is what the Gateway API defines it to mean — but a
  grant for Service `payments` never authorizes a route to Service `admin`.

The mere presence of a `ReferenceGrant` object in the target namespace is
never treated as authorization on its own — every field above must match.
Every other namespaced-to-namespaced reference that crosses namespaces
(owner references, selectors, volume/config references) is invalid
unconditionally; `ReferenceGrant` authorization applies only to `routes_to`.
A cluster-scoped subject (for example a PersistentVolume referencing a
namespaced PersistentVolumeClaim) is exempt from this check, since it has no
namespace of its own to disagree with.

Both halves of that decision — the backend object and the grant — live in
the *target* namespace, so the snapshot loads it. After the current
namespace's LISTs return, korvid reads the `routes_to` facts they produced
and, for each namespace one of them explicitly named, LISTs only the
referenced kind(s) plus `ReferenceGrant` there. Nothing else widens the
fan-out: no per-object GET, no cluster-wide sweep, and no namespace that no
route named. Those follow-up LISTs are deduplicated (a thousand routes into
one namespace still cost one LIST per resource), ordered by `(namespace,
group, plural)`, capped at `max_target_lists`, and each records its own
coverage with `scope` set to the namespace it ran in — so an RBAC denial in
the target namespace shows up as `forbidden` coverage rather than a silent
default-deny. The target namespace also gets one `partial` record because
other catalog kinds in that namespace were intentionally not read; a resolved
backend therefore does not make that namespace look fully covered. An
all-namespaces snapshot (`0`) does not repeat follow-ups for kinds already
listed in the first-phase catalog. A referenced discovered kind outside that
bounded catalog, such as a custom backend kind, still gets a namespace-scoped
follow-up and therefore a `partial` marker for that target namespace.

## Limits

| Limit | Default | Applies to |
|---|---|---|
| `max_resources` | 10,000 | Total resources fed into the graph across every listed source |
| `max_edges` | 50,000 | Total joined edges kept in the graph |
| `max_depth` | 5 | Hops the `d` expansion walks outward from the root |
| `max_nodes` | 500 | Resources visited by the `d` expansion, and the total render-row budget the screen shares across every category (direct, deeper, cycle, and repeat rows) |
| `max_concurrency` | 4 | Concurrent LISTs the snapshot loader runs at once |
| `max_target_lists` | 32 | Follow-up LISTs into the namespaces `routes_to` references name |

All caps are deterministic, never API-response-order dependent. The snapshot
loader applies the resource cap first: it moves the selected root's source
first, moves the exact selected object first within that source, then allocates
the remaining budget round-robin across sources. Each source is internally
ordered by `(namespace, name, uid)`, and each source that loses objects gets
its own `capped` record and dropped count. This prevents an early, large source
from starving every later kind. The graph builder then applies its own
resource cap — a safety net that is effectively a no-op once the loader has
already capped, but independently enforced regardless — sorted by `(group,
kind, namespace, name, uid)`.

The edge cap is applied *while* edges are generated, not after: a selector
join can describe far more candidate edges than the cap allows (an empty
`policy/v1` PDB selector matches every Pod in its namespace), so korvid
retains at most `max_edges` of them at a time, always the lowest-ordered
ones by `(subject, relation, target, evidence field)`. The result is exactly
what sorting every candidate and truncating would have produced, without
ever allocating every candidate. Its `capped` coverage record therefore
names the cap rather than a dropped count: the dropped candidates are never
retained, and counting the distinct edges among them would need the
unbounded memory the cap exists to avoid.

Exceeding any cap adds a `capped` coverage record rather than silently
dropping data with no trace.

## Secret safety

Korvid's relationship extraction is metadata-only by construction: it reads
only reference *names* (`envFrom`, `env[].valueFrom`, a volume's `secret`/
`configMap`, a projected volume source, `imagePullSecrets`) — never a
Secret's `data`/`stringData`, never a literal `env[].value`, and never a
container's `command`/`args` or object annotations.

Secrets are one of the fixed sources this view LISTs, but this path requests
Kubernetes' `PartialObjectMetadataList` representation. The API response
therefore contains only object metadata; korvid does not request or receive
Secret `data`/`stringData` for a relationship snapshot. If an API server
cannot serve that representation, the Secret source fails visibly in
coverage instead of falling back to a full-payload LIST. The resulting
summary and graph retain only name, namespace, UID, labels, and metadata-only
relationship facts. A row's Evidence column is always a manifest *field
path* (e.g. `spec.volumes[0].secret`), never a field *value*, and the same
holds for ephemeral containers, whose `envFrom`/`env[].valueFrom` references
are read through that same name-only extractor.

To actually see a Secret's contents you use the Secrets view, which routes
them through korvid's masking pipeline — this view has no path to them at
all.

## Navigating from the view

- **Enter** on a resolved row navigates to that resource's normal view — the
  same jump used elsewhere in korvid (for example the hierarchy tree) — with
  the cursor placed on the object. Pressing Enter on a Pod row opens the
  ordinary Pod table view, not a special graph-only view. Enter on an
  unresolved (`missing`/`invalid`) Dependencies row, or a header/capped row,
  shows why it cannot be navigated instead.
- **d** toggles the bounded dependent expansion described above.
- **c** toggles coverage detail text.
- **Escape** (or **q**) closes the view and returns to where you were.

## What this view does not do

- It does not retain Secret values, container commands/args, env literal
  values, or annotations anywhere in the facts it extracts, the graph, or
  what it renders (see [Secret safety](#secret-safety)).
- It does not perform any write — it is read-only, like every other korvid
  view that is not behind the write-approval gate.
- It does not attempt to enumerate every possible Kubernetes relationship —
  only the relation kinds in the table above. A relationship korvid does not
  model (for example a custom controller's own reconciliation logic) never
  appears here.
- It does not prove the *absence* of a dependency once coverage is
  incomplete (see [above](#why-an-incomplete-graph-cannot-prove-no-dependency)).
- It scopes its namespaced-resource LISTs to the currently selected
  namespace, plus the namespaces a `routes_to` reference in those results
  explicitly names (see [Cross-namespace
  routing](#cross-namespace-routing-referencegrant)); cluster-scoped kinds
  such as Node and PersistentVolume are always listed cluster-wide. It does
  not sweep every namespace looking for other relationships — press `0` to
  switch to the all-namespaces view before opening the graph if you need
  dependencies/dependents outside the current namespace considered.

## Blast radius in write previews

The same snapshot feeds the approval dialogs for `Ctrl-D` (delete), `r`
(rollout restart), and `S` (scale) when the scale is a *known decrease* —
the row's current desired replica count was readable and the requested count
is lower than it; a scale-up, a no-op, or a row with no readable desired
count gets the ordinary confirmation with no graph section and no snapshot
LIST at all. Only relationships with explicitly tested action semantics
participate, and the set differs by action:

| Action | Relations followed (target → its dependents) |
|---|---|
| delete | `owned_by`, `managed_by`, `routes_to`, `uses_volume`, `uses_config`, `protected_by`, `scheduled_on`, `bound_to` |
| rollout restart | `owned_by`, `managed_by` |
| scale down (known decrease only) | `owned_by`, `managed_by`, `selects`, `routes_to` |
| cordon Node | none |
| uncordon Node | none |
| drain Node | `scheduled_on` |

For cordon and uncordon, no snapshot is loaded and no graph walk is
performed — the advisory section is derived locally. For drain, the
graph walk follows `scheduled_on` (Pods currently placed on the Node).
`protected_by` remains excluded from the drain walk because PDB blocker
state comes from `DrainPlan`, not the graph dependent walk; the plan is
the authoritative source for which Pods are PDB-blocked.

`selects` is deliberately excluded from delete and rollout restart. A Service
selecting many Pods does not fail because one selected Pod is deleted, so
korvid never claims it does — the same reasoning that keeps `missing` from
meaning "absent". A known scale-down is the one action where `selects` is
followed: a Service (or, through `routes_to`, an EndpointSlice or an
Ingress/Gateway route reaching it) whose selector matches the shrinking
workload's Pods is listed as a known dependent that **may be affected** —
never as one that loses an endpoint or stops routing. Nothing about *which*
Pod a controller removes, whether a Service still has ready endpoints
afterward, or whether traffic actually fails is evaluated or claimed.

A scale-down advisory additionally states, unconditionally, that
PodDisruptionBudgets do not gate it — a controller deletes surplus Pods
directly rather than through the Eviction API that PDBs constrain — and that
HorizontalPodAutoscaler targeting and reconciliation are not evaluated: an
HPA can independently overwrite the very replica count you just set. When
the target itself is an `apps/StatefulSet`, it also states that PVC retention
policy is not evaluated, since `persistentVolumeClaimRetentionPolicy` — not
this walk — decides whether the removed replicas' claims are kept or deleted.
Group and kind together select that line: the field belongs to the `apps`
API, so a custom resource whose kind is spelled the same way in another group
does not get it.
`protected_by` (PDB), `uses_volume`, `uses_config`, `scheduled_on`, and
`bound_to` are excluded from the scale-down relation set for the same
reason: none of them is something a scale-down itself changes for a Pod that
remains.

Only **resolved** edges are traversed; an unresolved reference is reported
as a warning instead. That warning is always bounded by *the affected set* —
an unrelated dangling reference elsewhere in the cluster never lands in your
approval dialog — and each action additionally states which relations it may
warn about. Delete and rollout restart warn about **any** relation: they
remove or recreate the object those references were resolved against, so a
restarted workload whose Pod mounts a deleted ConfigMap is exactly the case
worth seeing. A scale-down warns only inside the relation set it follows,
because a dangling `protected_by`, `uses_volume`, `uses_config`,
`scheduled_on`, or `bound_to` reference describes what a *remaining* Pod
still holds, not something the scale-down changes. The walk is breadth-first
and deterministic (each dependent is
listed once, with the first path that reached it; further paths to the same
dependent are counted as `additional known paths`), bounded to 3 hops and 50
resources, and classifies a genuine loop as a cycle rather than expanding it
twice.

This shortest-path rule also applies to delete and rollout restart. Because
both actions include `managed_by`, a production-shaped Deployment reaches a
matching Pod directly through its own `spec.selector`; its ReplicaSet is a
second direct dependent, while the ReplicaSet selector and the Pod's
ownerReference are two additional known paths to the already-listed Pod.
Selector and owner evidence are kept distinct: a selector identifies a Pod a
controller can manage or acquire, while an ownerReference records current
ownership.

A workload reaches its own Pods in a single hop, because it declares the
selector that binds them: a Deployment's, StatefulSet's or ReplicaSet's
`spec.selector` is a `managed_by` relationship to every Pod it matches,
alongside the `owned_by` chain those Pods' `metadata.ownerReferences` give.
So a Deployment's routing chain to its Ingress is `Deployment -> Pod
(managed_by) -> Service (selects) -> Ingress (routes_to)` — three hops,
inside the bound — and scaling it down names the Ingress. The ReplicaSet in
between is a direct dependent of the Deployment in its own right, and the
further routes to the same Pod — that Pod's owner reference up to the
ReplicaSet, and the ReplicaSet's own selector back down to it — are folded
into `additional known paths` rather than listed twice. Scaling that
ReplicaSet down instead reaches the same chain through the selector it
declares itself (`ReplicaSet -> Pod (managed_by) -> Service (selects) ->
Ingress (routes_to)`), also three hops, with its Pods' owner references
folded into `additional known paths` the same way. The bound is still a
bound: anything past
3 hops or 50 resources is disclosed by `traversal capped` on the dialog and
never silently dropped (see [Limits](#limits) for the graph view's own, much
larger caps, which this bound is independent of).

Each rendered hop names both halves of its evidence — the resource an edge's
evidence came from and the field path on it — and each is individually
length-bounded before the line is composed. The composed line is then capped
again at 240 characters, because a path line concatenates up to three
rendered hops onto it: once that cap is reached, the remaining tail is
replaced by a visible `...`, which can fall within the first hop's own
field — even though neither of its fragments approached its own bound — and
can omit later hops entirely. This is an accepted trade-off, not a defect —
an approval dialog is a 70-column modal, so a line that stays reviewable at
a glance matters more than showing every hop of a deep path in full — and
the `[inferred]` marker's width is reserved ahead of that cap, so it
survives regardless of where the cut falls.

The snapshot's own scope is the pane's namespace for a namespaced target, and
every namespace for a cluster-scoped one such as a Node or PersistentVolume
(or when the pane is already showing all namespaces) — so a dependent in
another namespace is never silently omitted from the preview. This is
*not* simply "the same scope the graph view uses": the graph view (`g`)
always LISTs namespaced sources in the pane's current namespace, regardless
of whether the selected row itself is namespaced or cluster-scoped (see
[What this view does not do](#what-this-view-does-not-do)) — so inspecting
a cluster-scoped row from a namespaced pane with `g` only sees dependents in
that one namespace unless you press `0` first. The write preview computes
its scope from the *target's* own namespaced-ness instead, so a cluster-scoped
delete or rollout restart is never under-scoped by the pane you happen to be
in. The preview always states which scope it used.

Everything the answer does not know is stated: a target that was not in the
snapshot at all (an object recreated under the same name has a new UID),
coverage that is not `complete`, a truncated snapshot, and either traversal
cap. Any of those also turns every count into a lower bound (`N or more`)
rather than an exact total. The target is matched by exact identity
including its UID, and never by name: a row whose summary carries no UID
gets no impact section at all — the preview is omitted and no snapshot is
loaded — rather than a summary silently attached to whichever object holds
that name now. The summary is advisory — see [Write impact
preview](tui.md#write-impact-preview) for how it appears and what it never
does.

Delete, rollout restart, a known scale-down decrease, and Pod resize show
this section today. `POD_RESIZE` (Pod resize) intentionally traverses no
relation. The existing Pod object keeps its UID/IP, owner, node placement,
mounts/config references, PDB membership, and routing membership. Runtime
resize considerations are rendered from the captured Pod manifest and
requested resources, not inferred from graph edges.

The remaining write types (scale-up, a scale with no readable current count,
edit, Helm, operator) have no tested per-relation semantics yet and
deliberately show nothing rather than a plausible guess. Cordon, uncordon,
and drain use the node maintenance advisory path described above.
