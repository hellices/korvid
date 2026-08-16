# Browsing the cluster

Everything about reading the cluster: tables, filters, custom columns, live
metrics, ops hints, the split workspace, the log viewer, namespace scope,
and context switching. Keys referenced here are listed in
[keybindings.md](keybindings.md).

## Custom columns

Teams encode operational facts in labels, annotations, and spec fields —
owning team, release version, container image. The `views:` section of
`config.yaml` adds them to any resource table (keyed by the **plural** kind
name used in `:` navigation):

```yaml
views:
  pods:
    columns:
      - name: TEAM
        label: team                          # metadata.labels['team']
      - name: OWNER
        annotation: owner                    # metadata.annotations['owner']
      - name: IMAGE
        jsonpath: .spec.containers[0].image  # dotted path + [index] subset
  deployments:
    replace: true          # replace the defaults (NAME/NAMESPACE always stay)
    columns:
      - name: VERSION
        label: app.kubernetes.io/version
```

Each column declares exactly one source: `label:`, `annotation:`, or
`jsonpath:` (a read-only built-in subset — dotted keys and `[n]` indexes;
no filters or wildcards). By default custom columns are appended after the
kind's built-in columns; `replace: true` keeps only NAME (and NAMESPACE in
all-namespaces mode) plus your columns.

Missing values render `<none>`; an expression that fails at runtime renders
`<err>` — the render loop never crashes. Invalid column definitions
(including duplicates, names shadowing built-in columns, the synthetic
helm views, and `secrets` — Secret values only render through the masking
pipeline) are dropped with a startup warning. `:sort <COLUMN>` sorts by
any custom column or by the built-in sort keys `name`, `age`, `cpu`, `mem`
(only while their columns are visible; custom values compare as
case-insensitive strings). Repeating flips direction, bare `:sort` clears.

## Live metrics

The pods table shows live `CPU` / `MEM` usage and `%CPU/R` / `%MEM/R`
(usage as a percentage of the declared request) from the `metrics.k8s.io`
API, polled every 15 seconds while the pods view is on screen.  The number
is always relative to the request, but the colour keys off enforced
**limits**: running above the request is normal bursting, while approaching
an enforced limit means OOMKill (memory) or throttling (CPU) territory.
Every applicable ceiling is checked — each container against its own limit
(the kubelet enforces them independently) and, on K8s 1.34+, the pod
aggregate against the pod-level limit — and the most severe colour wins
(green &lt; 70 % &le; yellow &lt; 90 % &le; red).  Only when no limit bounds
the usage does the colour fall back to the request ratio, capped at yellow
(bursting without a ceiling is expected, never critical).
On clusters without metrics-server the columns show `-` and korvid keeps
polling, so a later install is picked up without a restart.

## Ops hints

When the cursor lands on a troubled pod row (CrashLoopBackOff,
ImagePullBackOff, failing readiness, …) a hint strip appears under the
table with up to two concise lines built from the pod's container statuses
and its freshest Warning event — verbatim API data, no synthesized
diagnoses.  Anything that does not fit folds behind `+N more (i: details)`;
press `i` to open a read-only overlay with every troubled container (full
message, exit code, restart count, last-seen age) and the pod's recent
Warning events.

## Split workspace

`Ctrl-W` `v` splits the workspace into two side-by-side panes, each an
independent resource view with its own kind, namespace and filter — e.g.
deployments on the left, their pods on the right.  The focused pane (accent
border) receives all `:` commands, filters and keybindings; `Ctrl-W` `w`
moves focus to the other pane and `Ctrl-W` `q` closes the focused one.
Drill-down, describe and the log pane all act on the focused pane, and the
AI agent's screen context reports the focused view plus a one-line summary
of the other.  The layout is session-only — korvid always starts single-pane.

## Log viewer

The log pane supports multi-container merge: press `L` to stream logs from every
visible pod simultaneously with `[pod/container]` prefixes.  Lines that look like
JSON are auto-detected and rendered with colour highlighting; press `f` to toggle
between the formatted and raw views.  Press `p` to reload the pane with logs from
the previous (terminated) container instance.

Live streams reconnect automatically on transient errors or unexpected EOF.  After
five consecutive reconnect attempts without a successful line the header shows an
error state and a notification is raised.  The in-memory ring buffer retains the
last 5000 lines; when it overflows a one-time banner is written to the pane so you
know older lines were dropped.

## Namespace scope

korvid always watches exactly one explicit scope — a single namespace or
all namespaces — and never expands it on your behalf.

The startup namespace resolves in precedence order: the `-n`/`--namespace`
CLI flag, then `namespace:` in `~/.config/korvid/config.yaml`, then your
kubeconfig context's namespace, then `default`:

```bash
korvid -n team-a
```

Switch scope inside the app with `:ns <name>` (free-text always works, no
list permission needed), the `:ns` picker, or `0` to toggle the
all-namespaces view. Keys `1`-`9` jump straight to your favorite
namespaces — a pure UI shortcut over the same `:ns` path:

```yaml
namespace: team-a        # startup namespace (optional)
favorite_namespaces:     # 1-9 jump keys, in order (max 9)
  - team-a
  - team-b
```

On RBAC-limited clusters korvid reports denials instead of guessing around
them:

- A watch that answers 403 stops with one concise notice — no retry loop,
  no fan-out into other namespaces. Switch to a namespace your role grants
  with `:ns <name>` or a favorite key.
- `0` re-checks cluster-wide access each press, so it starts working the
  moment your role is granted the permission.
- When listing namespaces is forbidden, the `:ns` picker explains the
  denial and points at `:ns <name>` free-text entry.
- API discovery is unaffected: individual API groups that fail discovery
  are hidden; they never fail startup.

The legacy `namespaces:` fallback list (and the per-namespace watch
fan-out it drove) is gone; a leftover key produces a startup warning
pointing at `favorite_namespaces`.

## Context switching

`:ctx` opens a picker of kubeconfig contexts (current one marked);
`:ctx <name>` switches directly, with tab completion. Before anything is
torn down, korvid probes the target context — loads its credentials in
isolation and issues an authenticated self-access review — so a context
with expired credentials or an unreachable API server fails with an error
toast while you stay connected to the current cluster. Only after the
probe succeeds
does korvid stop watches, port-forwards, log streams and metrics polling,
clear cached state, and retarget everything (resource discovery, capability
probes like pod-resize support, cloud-provider hints, audit log context)
at the new cluster. An open AI conversation survives the switch: the agent
is told the context changed, and its tools operate on the new cluster from
the next turn.

## Session timeline

`T` opens a bounded, read-only log of what has happened in this session:
watch deltas (ADDED/MODIFIED/DELETED), Warning events, context switches, and
audit-logged writes, newest first. It performs no cluster I/O of its own —
it only renders what the running session already recorded — so it opens
instantly, with or without a row selected, and works the same with the AI
agent disabled. The table does not refresh itself while open; reopening it,
or changing a filter, renders a fresh snapshot of the entries currently stored.

By default the timeline shows only the current kube context's epoch, every
source, every resource. Inside the modal:

- `e` toggles between the current epoch and every epoch the session has
  seen (a stale entry from before a `:ctx` switch is otherwise never shown
  by default).
- `s` cycles the source filter: all → watch → event → context → write →
  all.
- `r` toggles between every resource and the one selected in the table
  behind the modal at the moment `T` was pressed (captured once; it is
  never re-read while the modal stays open).
- `Enter` on a row that carries a resource navigates there, reusing the
  same jump path as every other in-app navigation; rows with no resource
  (a context-switch entry, say) are inert.
- `Esc` / `q` closes.

Every cluster-controlled field the timeline renders — timestamps, Warning
`reason`/`note`, context names, resource identifiers — is shown as literal
text, never interpreted as Rich markup, even if the cluster or a workload
supplies text that looks like a markup sequence.

The timeline is bounded in memory and never grows without limit: it holds
at most `max_entries` entries and `max_bytes` of encoded content, evicting
the oldest entries first once either cap is hit; the banner above the table
reports how many entries are currently stored, their encoded size, and how
many were evicted or refused. Configure the caps in
`~/.config/korvid/config.yaml`:

```yaml
timeline:
  max_entries: 500      # default
  max_bytes: 262144     # default (256 KiB of encoded content)
```

## Write impact preview

Destructive writes that have tested relationship semantics — delete
(`Ctrl-D`) and rollout restart (`r`) — show a graph-derived impact section
above the server dry-run preview in the approval dialog. It answers one
bounded question: which resources korvid has already observed depend on this
one?

    graph-derived impact (advisory):
      delete apps/Deployment/prod/web
      advisory only: known relationships from one bounded snapshot - not a prediction of failure, no replacement for the server dry-run, and never a block on approval.
      known direct dependents (may be affected): 1 or more
        - apps/ReplicaSet/prod/web-abc via owned_by (declared) at apps/ReplicaSet/prod/web-abc: metadata.ownerReferences[0]
      known transitive dependents (may be affected): 1 or more
        - Pod/prod/web-abc-1 via owned_by (declared) at apps/ReplicaSet/prod/web-abc: metadata.ownerReferences[0] -> owned_by (declared) at Pod/prod/web-abc-1: metadata.ownerReferences[0]
      additional known paths: 1 or more (already-listed dependents reached again)
      scope: prod
      graph coverage: incomplete - a missing dependent here does not prove none exists
        - gateway.networking.k8s.io/*: unavailable

Every count reads `1 or more` above because the Gateway API group could not
be listed: that one incomplete coverage record is enough to make the whole
answer a floor rather than a total (see the `N or more` bullet below). With
every source `complete` and neither bound hit, the same summary renders
exact counts.

The section is **advisory**, and says so on its second body line — directly
under the action, before the first count, because that is where the hedge is
still on screen with the target rather than below a body that can run to the
preview's caps. It never predicts failure, never replaces the
server dry-run, and never blocks approval: the y/typed-name gate, the UID
precondition, the RBAC pre-check, and the fail-closed audit log are exactly
what they were. Scale, edit, resize, cordon/uncordon, drain, Helm, and
operator flows do not show it — they have no tested per-relation semantics
yet, and korvid would rather show nothing than a plausible guess.

Reading it:

- **direct** dependents are one hop from the target, **transitive** are two
  or more; each line names the relation, how the fact was derived, and the
  resource and manifest field the evidence came from — for a
  selector-derived `managed_by`/`protected_by` hop that resource is the
  Deployment/PDB that declared `spec.selector`, not the Pod it matched.
- `additional known paths` counts relationships that reach a dependent
  already listed above (a second route, a second mount). They are counted
  rather than repeated, so a count of dependents is never inflated.
- `relationship cycles` and `additional known paths` count edges the walk
  folded away rather than expanding them.
- Every cluster-derived count — both dependent sections, `relationship
  cycles`, `additional known paths`, and `unresolved references in the
  affected set` — renders as `N or more` instead of an exact `N` whenever
  the answer as a whole could not be exhaustive: `traversal capped`,
  `snapshot truncated`, `graph coverage: incomplete`, or `target not found
  in this snapshot`. A capped walk stops before it reaches every dependent,
  a truncated snapshot was already missing resources or relationships
  before the walk began, and a source that could not be listed was never
  joined at all — so in each case `N` is a floor and an exact number would
  read as exhaustive (and would contradict the coverage line right below
  it). `none in this snapshot` is left as-is: it is already a statement
  about the snapshot, not a count — which is also why a missing target,
  whose sections are all empty, hedges nothing. The `... N more dependents
  not shown (preview capped)`, `... N more unresolved references not shown
  (preview capped)` and `... N more coverage records not shown (preview
  capped)` lines also stay exact — they count what the preview cut from
  rows it holds, not what was never found — and each names the section it
  cut, since all three overflow at the same indent.
- `[inferred]` marks a hop derived by a heuristic rather than read from a
  manifest. It is labelled, never a blocker.
- `unresolved references in the affected set` lists dangling references
  held by the target or by something it takes down — a mounted ConfigMap
  that no longer exists, say — whatever relation they use. Each line names
  its own confidence (`declared`, `observed`, or `inferred`) next to the
  relation, the same way a dependent path does, so a heuristically-derived
  dangling reference is identifiable on its own line, not only through the
  `[inferred]`/aggregate note above.
- `scope` is the namespace the snapshot covered. `all namespaces` appears
  for a cluster-scoped target (a Node, a PersistentVolume) or an
  all-namespaces view; otherwise the coverage below it is only ever
  complete *within that namespace*.
- `target not found in this snapshot - dependents unknown` means the exact
  object (UID included) was not in the snapshot — usually deleted and
  recreated under the same name. The counts below it then describe the
  snapshot, not your object.
- `graph coverage: incomplete` means some source could not be listed
  (RBAC, an absent API, a cap): a missing dependent is then *unknown*, not
  *absent*.
- `traversal capped` and `snapshot truncated` are two different bounds, not
  one:
  - `traversal capped` means the *impact* walk itself — the dependent search
    for this one action — hit its own limit: 3 hops, 50 dependents.
  - `snapshot truncated` means the underlying relationship snapshot (the
    same one the graph view `g` builds) hit one of its own, much larger
    input caps while gathering raw objects and candidate edges before the
    impact walk ever started: either the resource cap (input objects were
    dropped, so some resources were never joined) or the edge cap
    (candidate relationships were dropped, so some edges between resources
    that *are* present were never kept). Both are coarser, earlier limits
    than the 50-dependent traversal cap above (see
    [Limits](resource-relationships.md#limits) for the exact numbers).

The snapshot is the same bounded, read-only LIST fan-out the relationship
view (`g`) performs — scoped to the namespace of the pane the write was
raised from for a namespaced target, and cluster-wide for a cluster-scoped
one so a dependent in another namespace cannot be quietly missed — with a
5-second deadline. If it times out or fails, the dialog says `impact
unavailable; approval remains available` and the approval proceeds normally.
If the context switches, the selection moves, focus lands in the other pane
of a split workspace, or that pane changes its namespace while the snapshot
loads, the write is cancelled before any dialog opens — even when the newly
focused pane happens to sit on the same object.

The summary is matched to the target by **exact identity, UID included**.
When the selected row carries no UID (a summary type that does not expose
one), the section is omitted entirely: the dialog opens with the dry-run
preview only, and no snapshot is loaded at all. korvid does not fall back to
matching by name — that would silently reconnect the preview to whatever
object currently holds the name — and it does not show `target not found in
this snapshot` either, which would read as "the object is gone" when the
truth is only that korvid has no UID to match on. Approval, the typed-name
gate, the write, and the audit record are unaffected.
