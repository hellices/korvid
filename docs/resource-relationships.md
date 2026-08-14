# Operational resource relationships

Select a resource and press `g` to see what it depends on and what depends on
it — owner references, label selectors, config/volume mounts, routing
backends, node scheduling, and storage bindings, joined from the resources
korvid has already listed. It answers "what breaks if I delete this?" and
"why is this Pod using that ConfigMap?" without a second tool.

This is a **read-only, metadata-only** view: it never issues a write, and it
never reads a Secret's `data`/`stringData`, a container's `command`/`args`, or
a literal environment variable value. See [Secret safety](#secret-safety)
below.

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
| `uses_config` | Pod / workload template | A ConfigMap or Secret referenced by name only (`envFrom`, `env[].valueFrom`, a volume, a projected volume source, or `imagePullSecrets`) |
| `scheduled_on` | Pod | The Node named in its (live) `spec.nodeName` |
| `bound_to` | PersistentVolumeClaim / PersistentVolume | The bound PersistentVolume / PersistentVolumeClaim on the other side |

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
direct rows, expanded rows, and cycle rows by one counter, so no single
category can silently grow the table past the cap on its own). Deeper rows
are labelled with their hop count (`Dependents (depth 2)`, etc.).

An edge that loops back to an already-visited resource is recorded as a
**cycle** row (`Dependents (cycle)`) instead of being traversed again, so a
Deployment → ReplicaSet → Pod chain — or any other cyclical ownership
pattern — terminates safely rather than looping forever. When any row
category is capped, a trailing `(capped)` row explains how many rows were
omitted and why.

## Coverage

Before it can join any facts, korvid LISTs a fixed catalog of resource kinds
(Pods, Services, ConfigMaps, Secrets, PVCs, PVs, Nodes, Deployments,
ReplicaSets, StatefulSets, DaemonSets, Jobs, CronJobs, EndpointSlices,
Ingresses, PodDisruptionBudgets) plus any Gateway API resources
(`gateway.networking.k8s.io` Gateways, `*Route` kinds, ReferenceGrants) the
cluster's own API discovery reports. Every discovered `*Route` kind in that
group is read through the one `spec.rules[].backendRefs[]` shape the Gateway
API defines for all of them, so HTTPRoute, GRPCRoute, and the stream routes
(TLSRoute/TCPRoute/UDPRoute) all contribute `routes_to` edges rather than
being listed and silently ignored. A CRD outside that group whose kind merely
ends in `Route` (for example an OpenShift `Route`) is neither listed nor
interpreted. Route `parentRefs` (the Gateway a Route attaches to) are not
modelled. Each LIST records one of five **coverage states**:

- **`complete`** — the LIST succeeded.
- **`forbidden`** — the LIST returned 403; RBAC denies this account that
  resource kind.
- **`unavailable`** — the resource kind (or the whole Gateway API group) was
  never discovered on this cluster, or a Gateway API LIST returned 404/405 —
  an absent optional CRD, not a permission problem.
- **`failed`** — the LIST raised any other API error or a network/transport
  error.
- **`capped`** — the number of input resources (across all sources) or the
  number of joined edges exceeded a hard limit and the excess was dropped
  deterministically (see [Limits](#limits)); this can also appear once per
  cap even when every individual source's own LIST was `complete`.

Press `c` to toggle coverage detail: the underlying error/skip text for each
non-`complete` record (sanitized: control characters flattened, capped at
512 characters, so a pathological RBAC/API message cannot break the display
or grow the snapshot unboundedly).

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
`complete`: the object was deleted, or recreated with a new UID after the
table's row was read, or dropped when a source hit the resource cap. Its
Dependencies and Dependents sections are then empty because the snapshot has
nothing about it — not because the cluster has nothing. The banner says so
literally ("This resource is not present in this snapshot …") instead of
letting two empty sections read as "no relationships."

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
just because the name matches. A resource is only resolved by name when its
reference carries no UID at all (some reference shapes, like a ConfigMap
`envFrom`, never carry one).

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

## Limits

| Limit | Default | Applies to |
|---|---|---|
| `max_resources` | 10,000 | Total resources fed into the graph across every listed source |
| `max_edges` | 50,000 | Total joined edges kept in the graph |
| `max_depth` | 5 | Hops the `d` expansion walks outward from the root |
| `max_nodes` | 500 | Resources visited by the `d` expansion, and the total render-row budget the screen shares across every category |
| `max_concurrency` | 4 | Concurrent LISTs the snapshot loader runs at once |

All caps are deterministic, never API-response-order dependent. The resource
cap is enforced twice, each in its own fixed order: the snapshot loader caps
first, keeping resources in `(group, plural)` source order (each source's own
resources ordered by `(namespace, name, uid)`); the graph builder then
applies its own resource cap — a safety net that is effectively a no-op once
the loader has already capped, but independently enforced regardless — sorted
by `(group, kind, namespace, name, uid)`, and its edge cap sorted by
`(subject, relation, target, evidence field)`. Exceeding any cap adds a
`capped` coverage record rather than silently dropping data with no trace.

## Secret safety

Korvid's relationship extraction is metadata-only by construction: it reads
only reference *names* (`envFrom`, `env[].valueFrom`, a volume's `secret`/
`configMap`, a projected volume source, `imagePullSecrets`) — never a
Secret's `data`/`stringData`, never a literal `env[].value`, and never a
container's `command`/`args` or object annotations. This holds even though
Secrets are one of the fixed sources the loader LISTs: the LIST result is
reduced to name/namespace/UID/labels/relationship-facts before it is ever
joined into the graph, so no Secret value reaches the graph, the screen, or
any evidence field. A row's Evidence column is always a manifest *field
path* (e.g. `spec.volumes[0].secret`), never a field *value*.

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

- It does not read Secret values, container commands/args, env literal
  values, or annotations (see [Secret safety](#secret-safety)).
- It does not perform any write — it is read-only, like every other korvid
  view that is not behind the write-approval gate.
- It does not attempt to enumerate every possible Kubernetes relationship —
  only the relation kinds in the table above. A relationship korvid does not
  model (for example a custom controller's own reconciliation logic) never
  appears here.
- It does not prove the *absence* of a dependency once coverage is
  incomplete (see [above](#why-an-incomplete-graph-cannot-prove-no-dependency)).
- It scopes its namespaced-resource LISTs to the currently selected
  namespace, the same way every other korvid table does (cluster-scoped
  kinds such as Node and PersistentVolume are always listed cluster-wide) —
  press `0` to switch to the all-namespaces view before opening the graph if
  you need dependencies/dependents outside the current namespace
  considered.
