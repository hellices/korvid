# ConfigMap and Secret Propagation Design

**Issue:** [#410](https://github.com/hellices/korvid/issues/410) (design: [#499](https://github.com/hellices/korvid/issues/499)); ships the shared evidence renderer, [#486](https://github.com/hellices/korvid/issues/486) (S6 of #409)  
**Release:** v0.7.0  
**Builds on:** [capability-aware diagnostics](2026-10-25-capability-aware-diagnostics-design.md) (S1–S5, merged)  
**Status:** Proposed — awaiting maintainer review

## Purpose

A ConfigMap or Secret can change without the running application adopting
it. Whether it does depends on how each container consumes it, and a
reference graph does not say:

- environment variables, from `envFrom` or `env[].valueFrom`, are read once,
  when the container starts;
- a ConfigMap or Secret volume, plain or projected, is updated in place by
  the kubelet, eventually;
- a volume mounted with `subPath` or `subPathExpr` is never updated;
- whether the application rereads an updated file is not visible to
  Kubernetes at all.

This design adds a read-only view, opened on a selected ConfigMap or Secret,
that lists each Pod container consuming it, how, and what that means for a
change. It is the first consumer of the #409 foundation, so the shared
evidence renderer (S6) lands with it.

## Decisions to confirm in review

This design was written without a clarifying conversation, so these choices
are stated rather than assumed agreed:

1. **Pods are the consumers.** The view reads the Pods in the object's
   namespace, because a running container is what does or does not adopt a
   change. A workload template that has no Pod, such as a Deployment scaled
   to zero, is not listed. Its next Pod reads the current object anyway.
2. **Reads at run time, not the graph or the watch store** (approach 1
   below). The relationship graph's extraction is extended, not duplicated,
   and nothing is cached between runs.
3. **One namespace.** ConfigMap and Secret references cannot cross
   namespaces, so the Pods in the object's own namespace are the whole
   search space. Nothing is read cluster-wide.
4. **No change history in the first pull request.** Whether the object
   changed after a container started needs a session-scoped change record,
   which is the second slice. The first slice explains modes only.
5. **No default key.** The view is an action-palette entry, bindable in the
   keybinding editor. Every free single key is a scarce resource, and the
   maintainer can assign one at review.
6. **`immutable` stays not established.** A metadata projection carries no
   top-level fields (#409), so the view never claims an object is or is not
   immutable, and does not read the object for it.

## Approaches considered

1. **Read at run time through the #409 readers (chosen).** The view GETs
   the object's metadata and LISTs the namespace's Pods with `read_object`
   and `read_list`, and projects each Pod to its references at once. Every
   source gets an `Observation`, so a denied, truncated or unsupported read
   is reported in the foundation's vocabulary and rendered by S6. It costs
   one bounded LIST each time the view opens.
2. **Extend the relationship graph.** The graph already finds `USES_CONFIG`
   edges, but its facts do not record which container mounts a volume, or
   with what `subPath`, and its coverage is a `CoverageRecord`, not an
   `Observation`. Using it would mean converging the graph on the new
   vocabulary first, which #409 left to a follow-up.
3. **Use the watch store.** Watched Pod summaries do not keep the Pod spec,
   and adding it would put every Pod's references in memory for the whole
   session to serve one occasional view.

## The view

Opened on a ConfigMap or Secret row, from the action palette entry
**Explain configuration propagation**:

```text
ConfigMap team-a/settings
  configmaps v1 in namespace team-a: available — read · observed 2s ago
  pods v1 in namespace team-a: partial — stopped at the page limit · observed 2s ago

Environment variables — read when the container starts; a change needs a restart
  Pod api-7d9f-x2kq   container app    envFrom[0].configMapRef
  Pod api-7d9f-x2kq   container app    env[3] LOG_LEVEL ← key log-level

Volumes — updated in place by the kubelet, eventually; the application rereading it is not verified
  Pod api-7d9f-x2kq   container app    volume settings → /etc/app

subPath mounts — never updated; a change needs a restart
  Pod worker-0        container main   volume settings → /etc/app/app.yaml (subPath app.yaml)

Not established: whether every consumer is listed (the Pod list stopped at the page limit)
Not established: whether the object is immutable (only its metadata is read)
```

- **One line per source** comes first, rendered by S6.
- **Sections by mode.** Each mode's meaning is said once, in its heading,
  rather than on every row. A section with no row is not shown.
- **A row** names the Pod, the container and its category (container, init
  container, sidecar, ephemeral), and the reference path. An environment
  row names the variable and key; a volume row names the volume and mount
  path. Values are never shown, because they are never read.
- **Enter** on a row opens that Pod in the resource table, through the
  navigation the relationship view already uses.
- **Restart is a suggestion, not an action.** The environment and `subPath`
  headings name the existing approval-gated rollout restart as the way to
  adopt a change. The view never starts it.

## Consumption modes

`core/config_consumers.py` turns one Pod into the references it makes to
the selected object. It is pure and reads only these fields:

| Where | Mode | What a change means |
| --- | --- | --- |
| `containers[].envFrom[].configMapRef`/`secretRef` | `ENV_FROM` | read at start; needs a restart |
| `containers[].env[].valueFrom.configMapKeyRef`/`secretKeyRef` | `ENV_KEY` | read at start; needs a restart |
| a `configMap`/`secret` volume, or a `projected` source, mounted without `subPath` | `VOLUME` | updated eventually; reload not verified |
| the same volume mounted with `subPath` or `subPathExpr` | `SUBPATH` | never updated; needs a restart |
| the same volume, mounted by no container | `UNMOUNTED` | nothing reads it |
| `imagePullSecrets[]` | `IMAGE_PULL` | used only when an image is pulled |

- **Container category** comes from the list a container is in. An init
  container with `restartPolicy: Always` is a sidecar and is treated like a
  container. Any other init container ran once before the Pod started, so
  even a `VOLUME` mount of its own does not affect the running Pod, and its
  row says so.
- **`optional: true`** on a reference is shown, since the Pod runs whether
  or not the object exists.
- **Matching** is by kind, namespace and name. A Secret volume names its
  object with `secretName`, a projected Secret source with `name`; both are
  handled, as the graph's extraction already does.
- **Kept fields:** the Pod's name, namespace and UID, its controlling
  owner's kind and name, and for each reference the container name and
  category, the reference path, the mode, `optional`, the variable name and
  key (environment), or the volume name, mount path and `subPath`
  (volumes). Nothing else of the Pod is kept: not `env[].value`, not
  annotations, not status. A test feeds a Pod with literal values and
  asserts none reaches the result.
- **Unrecognised forms.** Only the forms in the table are recognised. A
  Secret used through a CSI driver or an in-tree volume plugin's
  `secretRef` is not listed, and the view says that only these forms are
  checked, so "no consumer" is never stronger than that.

The graph's existing extraction in `k8s/relationship_facts.py` gains the
mount details through shared helpers rather than a second parser, so the
two cannot disagree about which fields name a reference.

## Evidence

The view declares two sources, both required:

| Key | Read | Role | Why |
| --- | --- | --- | --- |
| `config-object` | `read_object`, metadata projection, the object's name | `CONFIRMS` | the object exists, and which incarnation |
| `pods` | `read_list`, object projection, the namespace | `RULES_OUT` | "no Pod consumes it" needs a complete list |

- **Budgets for `pods`:** the readers' maxima, pages of 100, 10 pages,
  2 MiB a response and 15 seconds, so up to 1,000 Pods. Beyond that, the
  read is `PARTIAL`/`TRUNCATED` and the view says the consumer set is
  incomplete.
- **Items are projected in the worker.** `read_list` returns whole Pods;
  the worker projects each one and drops the list before the result leaves
  it. Only the projection reaches the UI.
- **Incarnation.** The selected row's UID is compared with the
  `observed_uid` the GET returned. A different UID means the object was
  recreated; the view says so and reports the new one's evidence only. A
  404 means the object is gone, and the Pods that still reference it are
  listed under that heading.
- **Conclusions from absence.** "No Pod in team-a consumes this ConfigMap"
  is shown only when `pods` is conclusive. Otherwise the view says the
  consumer set is not established and why, from the blocking observation.
- **A denied Pod list** leaves the object line and says the consumers are
  not established because Pods could not be listed. It never says there
  are none.

## The evidence renderer (S6)

`ui/evidence_text.py` is the one place that turns evidence into text:

- `observation_line(observation, now) -> Text`:
  `<resource> <group/version> in <scope>: <state> — <basis sentence> · observed <age>`.
  Stale evidence adds "stale".
- A **basis sentence for every `Basis`**, in one table. A test enumerates
  `Basis` so a new member cannot ship without one. `DENIED`, `UNSUPPORTED`
  and `UNAVAILABLE` sentences are distinct, and so are the `PARTIAL` ones
  (page limit, deadline, response too large, continuation expired).
- `not_established(reason) -> Text`, for a conclusion whose source is not
  conclusive.
- Every cluster-provided name goes through `Text(..., markup=False)`
  semantics, as the relationship view does, so a name containing `[bold]` is
  shown literally.

Widgets call these and never read a `Basis` themselves.

## Controller and lifecycle

`ui/config_propagation_controller.py` owns the run, on the pattern of the
relationship view:

- **Availability.** The palette entry is available on a ConfigMap or Secret
  row. Its `UnavailableReason` is only "select a ConfigMap or Secret" or the
  wrong view. A missing source never disables it: the view opens and says
  what is missing.
- **One exclusive worker** in its own group, with `exit_on_error=False`, so a
  failure is a notification, not a crash.
- **Cancellation.** The context-switch coordinator cancels the worker group
  before the swap, through a `cancel_config_propagation_workers()` on the
  controller, as it already does for the graph, Pulse and the outcome
  tracker.
- **Run tokens.** Each open starts a run with `RunTracker.next_run()`. The
  result is applied with `apply_if_current`, so it is dropped after a
  context switch, a credential replacement or a newer run, and the worker
  checks `ContextGuard.crossed(epoch)` as the relationship view does.
- **Discovery.** Sources are resolved from the client's discovery snapshot
  with `resolve`, so a cluster that does not serve the metadata projection
  reports `UNSUPPORTED`/`PROJECTION_UNSUPPORTED` for `config-object`, and
  the Pod list still runs.

## Security and privacy

- **No payloads.** ConfigMaps and Secrets are read as metadata only; the
  #409 reader refuses anything else.
- **No values from Pods.** The projection keeps references, never
  `env[].value`, annotations or status.
- **No new write path.** The restart suggestion is text; the existing
  approval-gated action is the only way to restart.
- **No hidden scan.** One namespace, the one the object is in.
- **Agent and MCP exposure** is out of scope, as #409 decided. Adding it
  later goes through `tools/` and the masking pipeline.

## Delivery

Each slice gets a sub-issue under #410 and its own pull request:

| Slice | Content |
| --- | --- |
| P1 | `core/config_consumers.py` (modes and projection), the shared extraction helpers in `k8s/relationship_facts.py`, the run in `core/`, the S6 renderer (#486), the controller, the view and its palette entry |
| P2 | session-observed changes: a bounded, UID-scoped record of the object's resource-version changes seen this session, compared with each container's start time to say "changed after this container started" for environment and `subPath` rows. It never orders resource versions and never uses `creationTimestamp` as a change time. |

## Testing

- **Projection:** every row of the modes table, init, sidecar and ephemeral
  containers, `subPathExpr`, `optional`, projected sources, a volume no
  container mounts, a Pod referencing another object, malformed specs, and a
  Pod with literal values that must not reach the result.
- **Evidence:** a denied Pod list, a truncated one, a 404 and a recreated
  object, a refused metadata projection, and the conclusion from absence
  shown only when `pods` is conclusive.
- **Renderer:** a sentence for every `Basis`, distinct sentences per state,
  stale and age text, and a name with markup shown literally.
- **Lifecycle:** a result after a context switch or a newer run is
  dropped, and the palette entry's availability.
- **Live:** a version-matrix scenario (S5) with a Pod that mounts a
  ConfigMap with and without `subPath` and reads it through `envFrom`,
  checking each mode on every supported version.

## Acceptance criteria of #410

| Criterion | Where |
| --- | --- |
| Navigate from a configuration object to its authorized consumers; environment, projection and `subPath` distinct | The view; Consumption modes (P1) |
| Core-only clusters get the complete baseline; denied, truncated, stale and unsupported forms explicit | Evidence; the renderer (P1) |
| No claim that an application applied its configuration | the `VOLUME` heading; Non-goals |
| Tests for env/envFrom, volumes, `subPath`, init containers, immutable, missing and recreated objects | Testing (P1); metadata-only changes (P2) |
| No payload read, rollout, hidden scan or model call; context switches cancel reads | Security and privacy; Controller and lifecycle |
| No sensitive literal leaks | Kept fields; the literal-value test |

## Non-goals

- Reading configuration data, mounted files, environment values or logs.
- Starting a restart, or any write.
- Workload templates without Pods, and references across namespaces.
- Whether an application rereads a file.
- Agent or MCP tools.
