# Workload Creation Constraints Design

**Issue:** [#411](https://github.com/hellices/korvid/issues/411) (design: [#515](https://github.com/hellices/korvid/issues/515))  
**Release:** v0.7.0  
**Builds on:** [capability-aware diagnostics](2026-10-25-capability-aware-diagnostics-design.md) (S1–S6, merged) and the reads, renderer and lifecycle of [ConfigMap and Secret propagation](2026-10-28-config-propagation-design.md) (#410, merged)  
**Status:** Proposed — awaiting maintainer review

## Purpose

A Deployment can exist, or be scaled up, while its Pods are rejected before
they are created. The Pod list is then empty, so nothing on it explains the
rejection, and node capacity is the wrong place to look. The reason is
usually namespace policy:

- a **ResourceQuota** whose `hard` limit the next Pod would exceed, or that
  tracks a resource the Pod does not specify;
- a **LimitRange** whose minimum or maximum the Pod's requests or limits
  fall outside, or whose ratio between them it exceeds.

Today the operator has to find the controller's `FailedCreate` event, read
its message, and look the quota or LimitRange up by hand. This design adds
a read-only view, opened on a selected workload. It shows the creation
failures recorded for that workload next to the namespace's quotas and
LimitRanges, and keeps four answers apart:

- **a recorded rejection**: a controller recorded that it could not create
  a Pod, and why;
- **a current possible constraint**: a policy that, as now observed, the
  workload's next Pod could run into;
- **no standard policy observed**: every quota and LimitRange in the
  namespace was read, and none applies;
- **not established**: a read was denied, truncated, stale or unsupported.

It is the second consumer of the #409 foundation, after #410, and reuses its
readers, renderer, run tokens and lifecycle.

## Decisions to confirm in review

This design was written without a clarifying conversation, so these choices
are stated rather than assumed agreed:

1. **Five workload kinds.** The view opens on a Deployment, ReplicaSet,
   StatefulSet, DaemonSet or Job: each of their controllers records
   `FailedCreate` when the API server rejects a Pod it creates. A CronJob
   creates Jobs, not Pods; its Jobs can be inspected directly. A bare Pod is
   rejected at the client, not by a controller.
2. **Reads at run time.** As in #410, the view reads what it needs when it
   opens, through the #409 readers, and keeps nothing between runs. The
   watch store keeps summaries without the Pod template or Events, and
   holding them all session long would serve one occasional view.
3. **Events are listed and filtered locally.** The readers do not send field
   selectors, and a Deployment's failures are recorded on several
   ReplicaSets, which one selector cannot name. So the view lists the
   namespace's Events within the maximum budgets and keeps only the
   `FailedCreate` events about the workload. A busy namespace can truncate
   the list, and the view says so. Adding field selectors to the reader
   boundary is a possible follow-up, not part of #411.
4. **Messages are classified by fixed phrases, never trusted further.** The
   rejection message is the API server's text, not an API field. The view
   recognises the quota and LimitRange admission phrasings below; a message
   it does not recognise is shown as recorded, verbatim, and attributed to
   nothing.
5. **Quantities are shown as the cluster wrote them.** `hard`, `used`,
   `min`, `max` and defaults are shown verbatim. Comparisons parse them
   into exact decimals with `parse_quantity`, never as strings and never as
   floats. A computed amount, such as what remains of a quota, is shown in
   the unit family its limit uses (see Quantities).
6. **"Possible constraint" is the second slice.** The first slice shows
   recorded rejections and the namespace's policies. Whether the
   workload's next Pod would run into a policy needs the template's
   demand with LimitRange defaults applied and each quota scope's
   applicability, so it is its own pull request.
7. **The command is `:admission`.** `:quota` is the ResourceQuota view's
   kind alias, and `:creation` would leave `c` prefixes harder to complete.
   The palette entry is **Explain workload creation failures**. As in #410,
   no default key is bound.

## Approaches considered

1. **Read at run time through the #409 readers (chosen).** One bounded GET
   and up to four bounded LISTs, each with an `Observation`, rendered by S6.
   Every failure is reported in the shared vocabulary, and context switches
   cancel and drop results as in #410.
2. **Extend Pulse.** Pulse already raises a Deployment's `ReplicaFailure`
   condition (`pulse.deployment.replica-failure`) as an attention item, but
   not the Events behind it, nor the policies, and its findings are not
   observations. Explaining a finding needs the policy reads anyway, and
   converging Pulse with the #409 vocabulary is a separate follow-up (#409,
   "After #410 and #411").
3. **Dry-run a Pod.** A server-side dry-run create would answer exactly,
   but it is a write request, needs `create` permission, and runs every
   admission webhook. #411 rules it out.

## The view

Opened on a workload row, from `:admission` or the palette entry:

```text
Deployment shop/api   desired 3 · created 0
  deployments apps/v1 in namespace shop: available — read · observed 1s ago
  replicasets apps/v1 in namespace shop: available — read · observed 1s ago
  events events.k8s.io/v1 in namespace shop: available — read · observed 1s ago
  resourcequotas v1 in namespace shop: available — read · observed 1s ago
  limitranges v1 in namespace shop: available — read · observed 1s ago

Recorded creation failures — what the controller recorded, when it was recorded
  ReplicaSet api-7d9f   12× · last 2m ago   quota compute: requests.cpu requested 500m, used 2, limited 2
  ReplicaSet api-7d9f   3× · last 9m ago    a LimitRange in this namespace: maximum memory usage per Container is 1Gi, but limit is 2Gi

ResourceQuotas — current values; not a reservation, and the next create may still differ
  compute       requests.cpu   used 2 / hard 2     at its limit
                limits.memory  used 3Gi / hard 8Gi
  objects       pods           used 10 / hard 20
                count/configmaps used 4 / hard 10
  scopes: NotTerminating

LimitRanges — current constraints; other admission plugins can also change a Pod
  defaults      Container  cpu      default 500m · defaultRequest 100m · max 1
                Container  memory   default 512Mi · max 1Gi

The rejections were recorded at the times shown; the quotas are as read now.
```

- **One line per source** comes first, rendered by S6.
- **The header** names the workload and its counters: for a Deployment,
  ReplicaSet or StatefulSet, `spec.replicas` and `status.replicas`; for a
  DaemonSet, `desiredNumberScheduled` and `currentNumberScheduled`; for a
  Job, `parallelism` and `status.active`. They come from the workload's own
  status, so they say what the controller last reported, not how many Pods
  exist.
- **Recorded creation failures** are rows of `FailedCreate` events and
  `ReplicaFailure` conditions, newest first, each with what it is about,
  its count and age, and its classification.
- **ResourceQuotas** and **LimitRanges** list every object in the
  namespace with their values. Quota resources are grouped as compute,
  storage, object counts and other, and a resource at or over its limit is
  marked.
- **Enter** on a quota or LimitRange row opens that object in its table; on
  a failure row, it opens the object the failure is about.
- **No action is offered.** The view does not change replicas, quotas,
  limits or the template.

## Reads

All reads are in the workload's namespace, under one run token:

| Key | Source | Verb | Role | Kept by the projection |
| --- | --- | --- | --- | --- |
| `workload` | the workload's resource in `apps/v1` or `batch/v1` | GET, object, expected UID | confirms | see Projections |
| `replicasets` | `replicasets` in `apps/v1`, Deployments only | LIST, maximum budgets | confirms | name, UID, controller owner UID, `ReplicaFailure` condition |
| `events` | `EVENTS` (`events.k8s.io/v1`, then core `v1`) | LIST, maximum budgets | confirms | `FailedCreate` events about the workload or its ReplicaSets |
| `quotas` | `resourcequotas` in `v1` | LIST | rules out | name, UID, `spec.hard`, `spec.scopes`, `spec.scopeSelector`, `status.hard`, `status.used` |
| `limit-ranges` | `limitranges` in `v1` | LIST | rules out | name, UID, `spec.limits` |

- The workload and the quotas and LimitRanges are required. The
  ReplicaSets are required for a Deployment, since its failures are
  recorded on them. The Events are required, with the `confirms` role
  `EVENTS` forces: Events expire, so they can confirm a failure but never
  show that none happened.
- The workload is not a payload resource, so it is read whole. ReplicaSets
  arrive whole too. Both carry a Pod template with literal environment
  values, so each is projected at once and only the projection leaves the
  run, as in #410.
- No Pod is read: the question is about Pods that do not exist.

## Projections

**Workload.** The projection keeps identity (name, namespace, UID), the
counters above, a `ReplicaFailure` condition's reason, message and time,
and for P2 the template's demand:

- each container's and init container's name, `resources.requests` and
  `resources.limits`, in declaration order, and an init container's
  `restartPolicy`, which marks a sidecar;
- the Pod's `priorityClassName`, `activeDeadlineSeconds`,
  `runtimeClassName` and the Pod-level `resources`, when set;
- a StatefulSet's `volumeClaimTemplates`: each claim's name,
  `resources.requests.storage`, and its `storageClassName`, keeping an
  omitted name apart from an explicitly empty one.

Nothing else of the template is kept: no environment, command, image,
volume source or annotation.

**ReplicaSets.** Only those whose controller owner reference has the
Deployment's UID are kept. A ReplicaSet owned by another Deployment of the
same name, or by none, is not the workload's.

**Events.** An event is kept only when its `reason` is `FailedCreate` and
its subject is the workload or one of its ReplicaSets, matched by **UID**:
`regarding.uid` for `events.k8s.io/v1`, `involvedObject.uid` for core `v1`.
An event about an object with the right kind and name but another UID is
about an earlier incarnation. It is not shown, but the view counts it
("2 failures recorded for an earlier Deployment named api are not shown").
Each kept event keeps its subject, `note` or `message`, count and time:

- the count is `series.count`, then `deprecatedCount`, for
  `events.k8s.io/v1`, and `count` for core `v1`, and 1 when none is set;
- the time is chosen by `k8s/events.py`'s precedence. Q1 adds
  `deprecatedLastTimestamp` after `lastTimestamp` and
  `deprecatedFirstTimestamp` after `firstTimestamp`, so an older controller's
  repeating event served through `events.k8s.io/v1` is dated by its last
  occurrence, not by its creation.

**Quotas and LimitRanges** are kept as listed above. They carry no
payload.

## Classifying a recorded rejection

The controller's message wraps the API server's rejection, for example
`Error creating: pods "api-7d9f-x2kq" is forbidden: exceeded quota: compute,
requested: requests.cpu=500m, used: requests.cpu=2, limited: requests.cpu=2`.
The view recognises these phrasings:

| Phrase | Classification | Names a policy |
| --- | --- | --- |
| `exceeded quota: <name>, requested: …, used: …, limited: …` | quota exceeded | quota `<name>` |
| `failed quota: <name>: must specify <resources>` | quota requires a field | quota `<name>` |
| `status unknown for quota: <name>` | quota not yet computed | quota `<name>` |
| `minimum <resource> usage per <Container\|Pod\|PersistentVolumeClaim> is …` | below a LimitRange minimum | none |
| `maximum <resource> usage per <…> is …` | above a LimitRange maximum | none |
| `<resource> max limit to request ratio per <…> is …` | above a LimitRange ratio | none |
| anything else | other rejection | none |

- A quota is attributed only by the name the message gives. When that name
  is among the listed quotas, the row links to it; when it is not, the row
  says the quota is no longer present, or that the list is not conclusive.
  The requested, used and limited amounts are those the message recorded,
  at the event's time, not now.
- LimitRanger messages do not name the LimitRange. The row says "a
  LimitRange in this namespace", and never guesses which one.
- An unrecognised message is shown verbatim, as plain text, under "other
  rejection". It may come from an admission webhook, a Pod Security
  admission, or a quota on a resource korvid does not know. The view does
  not attribute it.
- A `ReplicaFailure` condition is classified the same way by its message.
  It reports the controller's latest failure, so it can outlive the Events
  that recorded it.

## Quantities

- **Shown verbatim.** Every value from the cluster is shown as written, so
  `1Gi` stays `1Gi` and `0.5` stays `0.5`.
- **Compared exactly.** `parse_quantity` parses the full Kubernetes
  grammar into a `Decimal`. A resource is "at its limit" when
  `used >= hard`. A value that does not parse is shown as written, and its
  comparison is "not established".
- **Resource names are normalised before comparison.** `cpu`, `memory` and
  `ephemeral-storage` in a quota mean `requests.cpu`, `requests.memory` and
  `requests.ephemeral-storage`, and are shown under the name the quota uses.
- **Grouped by meaning:**
  - compute: `requests.cpu`, `limits.cpu`, `requests.memory`,
    `limits.memory`, their short forms, and hugepages;
  - storage: `requests.storage`, the per-storage-class
    `<class>.storageclass.storage.k8s.io/requests.storage`, and the
    `ephemeral-storage` resources;
  - object counts: `pods`, `count/<resource>[.<group>]`,
    `persistentvolumeclaims` and its per-class form, `services`,
    `services.loadbalancers`, `services.nodeports`, `configmaps`,
    `secrets`, `replicationcontrollers` and `resourcequotas`;
  - other: extended resources (`requests.<domain>/<name>`) and anything
    unrecognised.
- **Computed amounts** (P2) use the unit family of the limit they relate to:
  binary suffixes (`Ki` … `Ei`) when the limit is written with one, decimal
  ones (`m`, `k` … `E`) otherwise. The largest suffix that represents the
  amount exactly is chosen; an amount no suffix represents exactly is shown
  in base units with the resource's unit word ("cores", "bytes", "objects").
  So 1.5 cores of remaining CPU is `1500m`, never a rounded `1` or `2`.

## Evidence

- **Stale quota status.** The quota controller fills `status.hard` and
  `status.used` after `spec.hard` changes. A quota's values are not
  current when it has no status, when its `status.hard` differs from its
  `spec.hard` as parsed quantities, or when `status.used` lacks an entry for
  a resource in `status.hard`: its usage is then unknown, which is also
  what makes admission reject with `status unknown for quota`. The quotas observation is then downgraded to
  `PARTIAL`/`STALE_STATUS` with `downgrade_stale_status`, naming the quota,
  and its rows say "status not yet updated". No quota is called "at its
  limit" from stale status.
- **Absence.** "No ResourceQuota in namespace shop" and "no LimitRange in
  namespace shop" are said only when that list is conclusive. A recorded
  failure is never ruled out: the Events source cannot show that nothing
  happened.
- **Recreated workloads.** The GET carries the selected row's UID. When the
  object read has another UID, the view says the workload was recreated,
  shows only the new incarnation's evidence, and matches Events against the
  new UID.
- **One time per source.** Events are dated individually, and the policies
  by their read. The view never presents a rejection and current quota
  usage as one snapshot: the note under the policies says the rejections
  were recorded at their own times, and the quotas are as read now.
- **Missing APIs.** A source the cluster does not serve, or that RBAC
  denies, keeps its S6 line. The other sources are still read and shown.

## Possible constraints (P2)

P2 adds a fourth section, **Possible constraints**, computed from the
template's demand and the policies as now observed. Each row is a
possibility, never a cause.

**The template's demand.** One new Pod's requests and limits per resource
are computed from the projected template:

- A container that sets a limit but no request gets the limit as its
  request, as API defaulting does before admission. LimitRange `Container`
  defaults then fill what is still missing: `defaultRequest` a request,
  `default` a limit. The view says these are the configured defaults, and
  that other admission plugins can change a Pod further.
- When more than one LimitRange gives a different default for the same
  resource, Kubernetes does not define which one applies. That resource's
  demand is then not established, and so is every row and scope that
  depends on it.
- The Pod's demand is computed as Kubernetes computes its effective
  request: app containers and sidecars (restartable init containers, in
  declaration order) are summed, and each other init container counts only
  if it alone, with the sidecars started before it, needs more.
- Pod-level `resources` override per resource and separately for requests
  and limits: a Pod-level CPU request replaces the computed CPU request and
  leaves the memory demand as the containers give it.
- A `runtimeClassName` can add a RuntimeClass `overhead` at admission.
  RuntimeClasses are cluster-scoped and not read, so with one set, the CPU
  and memory demand is not established, and neither is "nothing
  constrains this template".

**Quota applicability.** A quota applies to the Pod only if every scope
does:

| Scope | Applies when |
| --- | --- |
| `Terminating` / `NotTerminating` | `activeDeadlineSeconds` is set / unset |
| `BestEffort` / `NotBestEffort` | the Pod's QoS class is BestEffort / is not: no container and no Pod-level `resources` request or limit CPU or memory, after defaults |
| `PriorityClass` (selector) | `priorityClassName` satisfies the `In`, `NotIn`, `Exists` or `DoesNotExist` expression; with no `priorityClassName`, admission may assign a global-default PriorityClass, which is cluster-scoped and not read, so applicability is not established |
| `CrossNamespacePodAffinity` | not projected; applicability is not established |
| any other | applicability is not established |

**Rows.** For each applicable quota and each resource it limits:

- "would exceed" when `used + demand > hard`, with the remaining amount;
- "requires a field" when the quota limits `requests.cpu`, `cpu`,
  `limits.cpu`, `requests.memory`, `memory` or `limits.memory` and some
  container or init container does not specify it, even after defaults,
  matching `must specify`. Only these keys require a field; a hugepages or
  extended-resource quota does not. The check is per container, so the
  projection keeps which container lacks what, not only the Pod's sum.
  With the `PodLevelResources` feature, the quota evaluator skips this
  check for a resource the Pod-level `resources` set. Whether the feature
  is enabled is not observed, so for a template that sets that resource at
  Pod level, "requires a field" is not established rather than reported;
- "at its limit" for the `pods` and `count/pods` counts when `used >= hard`.

For each LimitRange, the Pod's per-container and per-Pod values are checked
against `min`, `max` and `maxLimitRequestRatio`.

A StatefulSet's claims are a separate, conditional demand. A new Pod reuses
any claim that already exists, as a replacement Pod or a scale-up over
retained claims does, and the view does not read claims. So claim rows say
"if a new claim is created": each claim template's storage request is
checked, per storage class, against the storage and claim-count quotas and
the `PersistentVolumeClaim` LimitRanges, and never added to the Pod's
demand. A claim template that omits `storageClassName` may get the
cluster's default class at admission, which is not read. Its per-class
checks are then not established, and so is "nothing constrains this
template". An explicitly empty `storageClassName` means no class, so no
per-class quota applies to it.

**What P2 never says.** That the next create will fail or succeed: quota
status is not atomic, other controllers create and delete objects
concurrently, and admission webhooks are not observed. A rolling update's
surge Pods, and how many Pods could still fit, are not computed. When every
quota and LimitRange is conclusive and nothing applies or would be
exceeded, the section says "No ResourceQuota or LimitRange observed
constrains this template".

## Controller and lifecycle

These follow #410:

- One exclusive worker in its own group, under a `RunTracker` token. The
  view opens only if the token is current, the view, scope and selection
  are unchanged, and no other dialog is open.
- The context-switch coordinator cancels the group by name, and a failed
  worker is reported through `REPORTED_WORKER_GROUPS`.
- Availability reasons are `MISSING_CAPABILITY` without a connection,
  `WRONG_VIEW` outside the five workload views, and `NO_SELECTION`.
- The screen re-renders its evidence lines and notes against its clock, so
  a policy that ages past its freshness policy is marked stale and stops
  ruling anything out.

## Security and privacy

- **No write of any kind**: no dry-run, probe Pod, scale or policy edit.
- **No payload read.** No ConfigMap, Secret or Pod is read. The workload
  and its ReplicaSets carry literal environment values in their templates;
  they are projected before the run returns, and a test checks that a
  literal value never reaches the report.
- **Messages are shown as plain `Text`**, never parsed as markup, and are
  bounded to 500 characters. They are not stored, logged, or sent to an
  agent.
- **One namespace.** Nothing is read cluster-wide.

## Delivery

Each slice gets a sub-issue under #411 and its own pull request:

| Slice | Content |
| --- | --- |
| Q1 core | the projections, the message classifier, quantity grouping and comparison, stale-status downgrade, and the run in `core/` |
| Q1 view | the controller, the view, `:admission` and its palette entry, a version-matrix scenario (a namespace with `pods: "0"` and a Deployment whose ReplicaSet records `exceeded quota`), and docs |
| Q2 | possible constraints: the template's demand with defaults, quota scope applicability, the exact-unit formatting of computed amounts, LimitRange checks, and claim templates |

## Testing

- Quantities: every suffix family and exponent form; equality across units
  (`1Gi` and `1073741824`); `0.5` against `500m`; an unparsable value;
  normalised short names; grouping of each resource kind.
- Classification: each phrase in the table; a quota named in the message
  but absent from the list; a message with several LimitRange violations;
  an unrecognised webhook message shown verbatim; a markup-looking message
  shown literally.
- Events: `events.k8s.io/v1` and core `v1` shapes, including a
  `deprecatedCount` and `deprecatedLastTimestamp` without a series; matching by UID, not
  name; an earlier incarnation's events counted and hidden; a Deployment's
  events through its owned ReplicaSets only; a truncated list.
- Policies: multiple quotas; scopes and scope selectors shown; missing and
  stale status downgraded, including matching hard maps with a missing
  `used` entry; changing status between runs; an empty
  conclusive list versus a denied or truncated one.
- Recreated workloads; a workload kind outside the five.
- The literal-value test: a template `env[].value` is absent from the
  report's `repr`.
- Lifecycle, as in #410: a newer run, a context switch, new credentials, a
  moved selection and an open dialog each drop the result.
- P2: defaults applied only to missing values; conflicting defaults from
  two LimitRanges; init containers and sidecars in order; Pod-level
  resources overriding one resource only; a `runtimeClassName`;
  conditional claim rows, with an omitted and an empty storage class;
  each scope row in the table, including a PriorityClass selector with no
  `priorityClassName`; "would exceed" at,
  just under and just over the limit; "requires a field" for one container
  of several, not for a hugepages quota, and not established when the
  Pod-level `resources` set that resource; LimitRange
  min, max and ratio per container, Pod and claim; remaining amounts
  formatted exactly.

## Acceptance criteria of #411

| Criterion | Where |
| --- | --- |
| A workload with no created Pods shows recorded failures next to the namespace's quotas and LimitRanges | The view; Reads (Q1) |
| CPU, memory, storage and count quantities render correctly, with no naive comparison or misleading conversion | Quantities (Q1; computed amounts in Q2) |
| Recorded denial, possible constraint, no relevant policy and unknown evidence stay distinct | Purpose; Evidence (Q1); Possible constraints (Q2) |
| Missing APIs, denied reads, expired events, multiple scoped quotas, stale status and truncated lists give no false success or root cause | Evidence; Classifying a recorded rejection |
| Tests for units, defaults, min/max, scoped and multiple quotas, changing status, mismatched event UIDs and recreated workloads | Testing |
| Bounded, cancellable, context-safe reads; no mutation, Secret payload or model call | Reads; Controller and lifecycle; Security and privacy |

## Non-goals

- Dry-run or probe creates, and any write.
- Node capacity, scheduling, taints and affinity: a Pod that was created
  but not scheduled is Pulse's and the Pod view's concern.
- Admission webhooks and Pod Security admission, beyond showing their
  recorded message verbatim.
- CronJobs, bare Pods, and workloads in other namespaces.
- Raising these findings in the #389 attention surface, and agent or MCP
  tools: possible follow-ups once the view has shipped.
