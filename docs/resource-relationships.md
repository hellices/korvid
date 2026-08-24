# Operational resource relationships

Select a resource and press `g` to see what it depends on and what depends on
it — owner references, label selectors, config/volume mounts, routing
backends, node scheduling, and storage bindings, joined from the resources
korvid has already listed. It answers "what breaks if I delete this?" and
"why is this Pod using that ConfigMap?" without a second tool.

This is a **read-only, metadata-only** view: it never issues a write, and no
Secret `data`/`stringData`, container `command`/`args`, or literal
environment variable value is ever retained in a relationship fact, the
graph, or anything the screen renders (see [Deliberate
limits](#deliberate-limits)).

<figure class="docs-visual">
  <img src="../assets/scenes/relationship-graph.png" width="1280" height="720" loading="lazy" alt="Korvid relationship screen listing a synthetic Pod's declared ConfigMap dependency and the Service that selects it">
  <figcaption>The two sections separate dependencies from dependents; every row preserves relation direction, confidence, state, and source field.</figcaption>
</figure>

## Read the graph

With a resource selected in any discovered (non-synthetic) table, press `g`.
Korvid resolves the exact root identity, runs a bounded set of LISTs (see
[Coverage is evidence, not completeness](#coverage-is-evidence-not-completeness)),
and splits the result into two panes:

- **Dependencies** — what the root declares or exhibits a relationship *to*.
  Deleting one of these can break the root.
- **Dependents** — what declares or exhibits a relationship *to* the root.
  Deleting the root can break one of these.

Every edge is **directed dependent → dependency**, fixed regardless of which
pane a relation kind tends to show up in: a ReplicaSet is *owned by*
(dependent on) its Deployment, so `owned_by` appears as a Dependents row on
the Deployment. Relation kinds are read from a fixed catalog — owner
references, selector matches (`selects`, `managed_by`, `protected_by`),
routing backends (`routes_to`), config/volume references (`uses_config`,
`uses_volume`), node placement (`scheduled_on`), and volume binding
(`bound_to`) — not an open-ended inference engine; a relationship korvid does
not model (a custom controller's own reconciliation logic, say) never
appears here.

A four-column legend covers how to read any row:

| Direction | Source | Resolution | Confidence |
|---|---|---|---|
| Dependencies | a manifest field path, e.g. `spec.volumes[0].configMap` | `resolved` / `missing` / `invalid` against what korvid listed | `declared` — read straight from the manifest |
| Dependents | live cluster state, e.g. an EndpointSlice `targetRef` or a Pod's `nodeName` | `resolved` / `not indexed` against the graph's own node set | `observed` — read from current state, not authored config |

A third confidence level, `inferred`, is reserved for a future
heuristic-derived fact; no relation kind uses it today. `missing` means "not
found among what korvid could see," never "does not exist" — see
[Coverage is evidence, not
completeness](#coverage-is-evidence-not-completeness). `invalid` marks a
cross-namespace `routes_to` reference that no exact Gateway API
`ReferenceGrant` authorizes (see [Cases that need
care](#cases-that-need-care)).

## Direct edges and bounded expansion

By default the screen shows only the root's **direct** dependencies and
dependents — one hop. Press `d` to toggle a **bounded transitive expansion**
of dependents: a breadth-first walk outward from the root, capped at
`max_depth = 5` hops and `max_nodes = 500` visited resources shared across
direct, expanded, cycle, and repeat rows by one counter, so no single
category can silently grow the table past the cap. No resource is walked
twice: a genuine loop back to an ancestor is labelled `Dependents (cycle)`
and stops there, while two independent paths converging on the same resource
are labelled `Dependents (repeat)` and shown once, not expanded again. When
any row category is capped, a trailing `(capped)` row states how many rows
were omitted and why — a bound is disclosed, never silently applied.

## Coverage is evidence, not completeness

Before joining any facts, korvid LISTs a fixed catalog of resource kinds plus
every Gateway API `*Route` and `ReferenceGrant` the cluster's own discovery
reports. Each source records its own coverage state: `complete`, `partial`
(intentionally scoped, for example a cross-namespace follow-up LIST), or one
of the failure states `forbidden`, `unavailable`, `failed`, `capped` (see
[Deliberate limits](#deliberate-limits) for the numeric caps). Press `c` to
see the per-source detail.

**A graph built from incomplete coverage can only prove a dependency
*exists*, never that one does not — an incomplete graph cannot prove
absence.** If a Secret LIST is `forbidden`, a Pod's `uses_config` edge to
that Secret renders as `missing`, but that means "not found among what
korvid could see." The screen's top line always states whether coverage as a
whole is `complete` or `incomplete`, and any `missing`/`invalid` resolution
under incomplete coverage should be read as "unknown," not "absent."

The root itself can also be absent from an otherwise-`complete` snapshot —
deleted or recreated with a new UID after the table row was read. The
banner then says so literally ("This resource is not present in this
snapshot …") rather than letting two empty sections read as "no
relationships."

## Cases that need care

- **Stale owner references.** A UID-carrying reference (an owner reference,
  an EndpointSlice `targetRef`, a PV's `claimRef`) is resolved by **UID
  first**: recreating a Deployment produces a new UID, so a ReplicaSet still
  carrying the old UID resolves `missing` rather than silently reattaching
  to the new Deployment by name. A reference is only matched by name when
  its shape never carries a UID at all (a ConfigMap `envFrom`, for example).
- **Cross-namespace routing (`ReferenceGrant`).** A `routes_to` reference
  naming a different namespace than its subject is `invalid` unless an exact
  Gateway API `ReferenceGrant` in the *target* namespace authorizes it —
  matching `from` group/kind/namespace and `to` group/kind/name with no
  wildcard credit. Its presence alone is never sufficient; every field must
  match. Only `routes_to` honors `ReferenceGrant` — every other
  cross-namespace reference is `invalid` unconditionally.
- **Snapshot misses.** A cluster-scoped kind (Node, PersistentVolume) is
  always listed cluster-wide; a namespaced kind is scoped to the pane's
  current namespace, plus any namespace a `routes_to` reference explicitly
  names. Press `0` for the all-namespaces view before opening the graph if a
  dependency in another namespace matters.

## Navigate or preview impact

- **Enter** on a resolved row jumps to that resource's normal view — the
  same jump used elsewhere in korvid — with the cursor on the object; `Esc`
  there returns to the graph. Enter on an unresolved (`missing`/`invalid`)
  row explains why it cannot be navigated instead.
- **d** toggles the bounded dependent expansion; **c** toggles coverage
  detail; **Escape**/**q** closes the view.

The same snapshot also feeds an advisory, graph-derived **blast-radius**
section in the approval dialog for delete, rollout restart, a known
scale-down, Pod resize, and Node drain — see [Preview impact before a
write](tui.md#preview-impact-before-a-write) for how it appears. Only
**resolved** edges are traversed, and each action follows a fixed,
representative relation set rather than the graph's full catalog:

| Action | Representative relations followed |
|---|---|
| delete / rollout restart | `owned_by`, `managed_by`, `routes_to`, `uses_config` |
| known scale-down | `owned_by`, `managed_by`, `selects`, `routes_to` |
| drain | `scheduled_on` |

An unresolved reference inside that set is reported as a warning rather than
silently skipped. The walk is bounded to 3 hops and 50 resources — much
tighter than the graph view's own caps — and every count becomes a lower
bound (`N or more`) once coverage, a UID mismatch, or a traversal cap makes
the true total unknowable. The section is advisory: it never claims a write
will fail or succeed, only what the snapshot found.

Three caveats apply to the scale-down preview in particular:

- **PDB (PodDisruptionBudget)** rules are *not* evaluated — the graph
  records the `poddisruptionbudget` relation, but whether the budget would
  gate the actual eviction is determined by the API server at runtime, not
  here.
- **HPA (HorizontalPodAutoscaler)** reconciliation is *not* evaluated — a
  scale-down you preview may be overwritten by the HPA's next control loop
  if a conflicting target replica count is in effect.
- **StatefulSet PVC retention policy** (`persistentVolumeClaimRetentionPolicy`)
  is *not* evaluated — PVC fate on scale-down depends on that policy and the
  storage class, neither of which the blast-radius walk examines.

## Deliberate limits

korvid never renders an unrestricted, cluster-wide relationship graph: a
snapshot is always scoped to one selected root, one namespace (or all
namespaces if you pressed `0` first), and the numeric caps below.

| Limit | Default | Applies to |
|---|---|---|
| `max_resources` | 10,000 | Resources fed into the graph across every listed source |
| `max_edges` | 50,000 | Joined edges kept in the graph |
| `max_depth` / `max_nodes` | 5 / 500 | Hops and resources visited by the `d` expansion |
| `max_target_lists` | 32 | Concurrent relation-target LISTs per snapshot |
| `max_concurrency` | 4 | Concurrent LISTs the snapshot loader runs at once |

Every cap is deterministic (ordered, never API-response-order dependent) and
disclosed: exceeding one adds a `capped` coverage record rather than
silently dropping data.

Relationship extraction is also metadata-only by construction: it reads only
reference *names*, never a Secret's `data`/`stringData`, a literal
`env[].value`, or a container's `command`/`args`. Secrets are read through
Kubernetes' `PartialObjectMetadataList` representation, so the API response
itself carries no Secret contents to retain — if a cluster cannot serve that
representation, the Secret source fails visibly in coverage instead of
falling back to a full-payload LIST. Secret **values** stay masked behind
the Secrets view's own pipeline; this view has no path to them at all.
