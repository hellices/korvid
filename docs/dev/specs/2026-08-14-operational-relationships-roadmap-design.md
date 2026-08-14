# Operational relationships, timeline, and blast radius (issue #194)

## Problem

korvid exposes several narrow relationships today: owner chains, Helm and OLM
hierarchies, Service endpoint diagnostics, PVC binding checks, and drain impact.
They are computed by separate features and cannot answer a general operational
question such as "what depends on this resource, what changed around it, and
what could this write affect?"

Issue #194 combines three user-visible capabilities:

1. a navigable resource relationship graph;
2. a bounded session timeline;
3. deterministic blast-radius text in write previews.

Implementing all three as one change would couple a new data model, broad
Kubernetes extraction, two Textual views, and the write approval path. The
result would be difficult to review and would repeat the long-running issue
pattern this backlog cleanup is intended to avoid.

## Alternatives considered

### One pull request for the complete issue

This keeps one acceptance checklist, but it has the largest review surface and
puts write-path safety behind unrelated UI work. A defect in any subsystem
blocks all user value. Rejected.

### UI-first, on-demand relationship lookups

This could render a graph screen quickly by adding per-selection API reads. It
would duplicate selector and reference logic, create per-node GET fan-out, and
make missing RBAC indistinguishable from "no relationship." It would also leave
no trustworthy model for the later blast-radius preview. Rejected.

### Data-first user-value slices

Build a safe relationship snapshot and its screen first, then a timeline and
its screen, then consume the graph in write previews. Each slice is usable on
its own and has a narrow security boundary. This is the selected approach.

## Delivery structure

Issue #194 remains the parent checklist. Three child issues are completed in
order:

1. **Operational relationship snapshot and graph view**
2. **Bounded session timeline and timeline view**
3. **Graph-derived blast-radius summaries in destructive write previews**

The parent closes only when all three child issues are merged. The first two
are read-only. The third changes preview text but does not add a new write path,
approval bypass, or automatic block.

## Slice 1: relationship snapshot and graph view

### Safe relationship facts

The Kubernetes adapter extracts a deliberately narrow, immutable
`RelationshipFacts` value while it already holds each list/watch manifest.
Facts contain metadata and references only:

- owner references, including owner UID, kind, API group, and name;
- resource labels and label selectors;
- Pod and pod-template references to PVCs, ConfigMaps, and Secrets by name;
- Service selectors;
- EndpointSlice Service ownership and endpoint target references;
- Ingress service/resource backends and TLS Secret references, plus Gateway
  forwarding/request-mirror backends;
- PDB selectors;
- Pod node placement (live Pods only; a workload's template `nodeName` is
  configuration, not an observed placement).

Secret `data` and `stringData` are never read into the facts model, and no
Secret value is ever retained in a summary, the graph, or rendered output.
Relationship snapshot Secret LISTs request Kubernetes
`PartialObjectMetadataList`; if the API server cannot provide that
representation, coverage reports the failure rather than retrying with a
full-object LIST. Environment literal values, command arguments, annotations,
and unrelated manifest fields are also excluded.

`GenericSummary`, `PodSummary`, and any specialized summary that flows through
the resource store carry `RelationshipFacts`. The extraction belongs in
`korvid.k8s`; graph construction belongs in `korvid.core`.

### Immutable snapshot, not an incremental mutable graph

`RelationshipGraphBuilder` builds an immutable `RelationshipGraph` from a
bounded collection of current summaries. Rebuilding from a store/list snapshot
is preferred to incrementally editing edges:

- a replaced object automatically receives its new UID;
- deleted objects cannot leave stale edges;
- cycles and duplicates are handled in one deterministic pass;
- tests can compare complete values;
- the builder needs no async lifecycle or subscriber failure policy.

The graph screen performs bounded LISTs for the supported kinds, in parallel
with a small concurrency cap, then follows referenced discovered routing kinds
that the first phase did not already cover. Cross-namespace follow-up and
`ReferenceGrant` authorization are restricted to Gateway API Routes; unrelated
cross-namespace observations cannot consume that cap. It never performs a GET
per node. Refresh repeats the snapshot operation.

### Identity and edge model

A `GraphResource` is identified by API group, kind, namespace, name, and
optional UID. An unresolved named reference is retained with `uid=None`; an
owner reference retains its historical UID even when no current object has that
UID. This makes replaced-UID and missing-target states visible instead of
silently reconnecting an old edge to a new object with the same name.

Each `RelationshipEdge` contains:

- `subject`: the resource that declares or exhibits the relationship;
- `target`: the referenced or matched resource;
- `relation`: a closed enum such as `owned_by`, `selects`, `routes_to`,
  `uses_volume`, `uses_config`, `protected_by`, or `scheduled_on`;
- `confidence`: `declared`, `observed`, or `inferred`;
- `evidence`: the subject resource and a JSON field path;
- `resolution`: `resolved`, `missing`, or `invalid`;
- a short machine-defined explanation.

Direction is always **dependent to dependency**. Reverse queries therefore
return known dependents, which is the direction the later blast-radius
calculation needs.

`declared` means a manifest field directly names or selects the target.
`observed` means runtime state identifies it, for example an EndpointSlice
endpoint `targetRef` or Pod `nodeName`. `inferred` is reserved for joins that
Kubernetes does not itself declare. Inferred edges are never promoted to
declared merely because the target currently exists.

### Completeness

Every attempted resource LIST produces a `CoverageRecord` with one of:

- `complete`;
- `forbidden`;
- `unavailable` (the API or CRD is not installed);
- `failed`;
- `capped`.

The graph is incomplete when any required source is not complete or when an
input cap is reached. A missing target and an invalid cross-namespace
reference are edge resolution states, not proof that no dependency exists.

Gateway API sources are optional: absence is `unavailable`, not an error, but
the view still reports that Gateway relationships were not observed. RBAC
denials name only group/resource/scope and do not expose credentials or Secret
content.

### Graph view

The first UI is an adjacency table rather than a force-directed canvas. It is
deterministic, keyboard accessible, dependency-free, and fits Textual's
existing modal/navigation patterns.

For the selected resource it shows:

- direct dependencies and direct dependents in separate groups;
- relation type, confidence, resolution, and evidence field;
- a visible incomplete-graph banner and coverage details;
- bounded transitive expansion with cycle markers and a node cap;
- navigation to a resolved resource through existing resource views.

No LLM is required. The same pure graph queries feed tests and the later
blast-radius slice.

## Slice 2: bounded session timeline and view

### Timeline model

`SessionTimeline` is an in-memory, append-only bounded deque in `korvid.core`.
Entries use one envelope with monotonic sequence, UTC timestamp, context epoch,
source, resource identity when known, and a typed payload.

The first payload kinds are:

- watch `ADDED`, `MODIFIED`, and `DELETED` deltas;
- Kubernetes Warning events;
- context switch started/completed/failed;
- korvid write intent and outcome.

The timeline stores metadata and bounded summaries, not full manifests, Secret
values, arbitrary audit payloads, or full Event messages. Event messages pass
the existing projection/masking boundary before storage.

### Bounds and context isolation

Configuration sets maximum entries and maximum encoded bytes. Both are enforced
on append; oldest entries are evicted until both limits hold. Oversized
individual entries are refused with a visible diagnostic rather than silently
truncated into a misleading event.

A context switch increments the existing epoch and partitions the timeline.
The default view shows only the current epoch. Previous epochs may remain in
the same bounded buffer for session review, but are clearly labelled and never
mixed into current-context blast-radius calculations.

### Producers and view

`WatchManager` emits deltas after the store accepts them. The Event watch emits
only Warning events. The UI composition root records context switches. The
existing write gate records intent/outcome timeline entries after the
fail-closed audit append succeeds; the timeline is never a substitute for the
durable audit log.

The timeline screen provides bounded filtering by resource, source, and current
epoch, and navigates resource-bearing entries back to existing views.

## Slice 3: graph-derived blast radius

`ImpactSummary` is a pure calculation over a relationship snapshot and one
proposed destructive action. It reports:

- the direct target;
- direct known dependents;
- transitive known dependents, separately;
- edge paths supporting every item;
- graph completeness and unresolved-reference warnings;
- whether the result was capped.

Only relationship types with explicit, tested action semantics participate.
For example, deleting an owner may affect owned dependents; deleting a
ConfigMap may affect workloads that declare it; deleting a Pod does not claim
that every Service selecting it will fail. Inferred edges are labelled and
never cause a write to be blocked.

The summary is added to the existing server dry-run preview and confirmation
dialog. It does not replace dry-run, UID preconditions, RBAC checks, typed-name
confirmation, or fail-closed audit logging. Approval remains possible when the
graph is incomplete, but the preview must say so.

## Error handling and safety

- No per-node API GET fan-out.
- No Secret value enters summaries, graph nodes, edges, timeline entries, or
  preview text.
- Missing RBAC and missing APIs remain explicit coverage states.
- Subscriber or rendering failures cannot kill a watch.
- Graph and timeline caps are reported, never silent.
- Context switches cannot reuse a graph or timeline partition from the old
  cluster.
- The feature remains deterministic and fully usable without an LLM.
- Blast-radius output is advisory; it never weakens the approval gate or audit
  fail-closed invariant.

## Testing

### Relationship graph

- owner cycles and duplicate edges;
- duplicate selectors and empty selectors using the API-version-specific
  Kubernetes semantics;
- EndpointSlice Service ownership and endpoint target references;
- Ingress service/resource backend and TLS Secret resolution, plus Gateway
  forwarding/request-mirror backend resolution;
- PVC, ConfigMap, and Secret references without Secret values;
- PDB selectors and Pod node placement;
- invalid cross-namespace references;
- missing RBAC, unavailable APIs, capped inputs, and mixed coverage;
- object replacement with the same name and a new UID;
- deterministic ordering and bounded transitive traversal;
- Textual navigation and incomplete-graph rendering.

### Timeline

- entry count and encoded-size eviction;
- oversized-entry refusal;
- watch, Warning Event, context switch, and write outcome producers;
- current-epoch partitioning and old-context isolation;
- projection of bounded Event text;
- deterministic filters and resource navigation.

### Blast radius

- direct and transitive dependents are distinct;
- cycles, unresolved targets, inferred edges, and caps;
- action-specific relationship semantics;
- incomplete-graph warning in every destructive preview;
- no impact summary can approve, execute, or bypass a write;
- audit failure still prevents execution.

## Out of scope

- A persistent or multi-user historical database.
- A complete causal dependency graph.
- Force-directed graph layout or a new visualization dependency.
- Retaining or rendering Secret values anywhere in this feature.
- Blocking a write solely because of an inferred edge.
- Cluster-write plugins or third-party graph extractors.
