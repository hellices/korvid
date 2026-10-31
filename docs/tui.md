# Browsing the cluster

Everything about reading the cluster: tables, filters, custom columns, live
metrics, the split workspace, the log viewer, namespace scope,
and context switching. Keys referenced here are listed in
[keybindings.md](keybindings.md).

<figure class="docs-visual docs-visual--annotated">
  <div class="docs-visual__stage">
    <img src="../assets/scenes/cockpit-poster.png" width="1280" height="720" loading="lazy" alt="Korvid pod table for a synthetic shop namespace with the crash-looping payment worker selected and its BackOff warning in the ops hint strip">
    <span class="docs-visual__pin" style="--x: 12%; --y: 97%;" aria-hidden="true">1</span>
    <span class="docs-visual__pin" style="--x: 50%; --y: 18%;" aria-hidden="true">2</span>
    <span class="docs-visual__pin" style="--x: 50%; --y: 3%;" aria-hidden="true">3</span>
  </div>
  <figcaption>
    <ol>
      <li><strong>Context and namespace</strong> stay visible while you navigate.</li>
      <li><strong>Resource evidence</strong> is watch-backed and filterable in place.</li>
      <li><strong>Effective keys</strong> follow the current view; <kbd>?</kbd> shows the complete set.</li>
    </ol>
  </figcaption>
</figure>

## Read the cockpit

The status row along the bottom is always on screen: current kube context
and the active namespace scope (or `all`). The key legend rides the top of
the screen and is collapsed by default to the few keys that matter most in
the current view (`~` expands the full grouped legend). The selected row is
not a static snapshot: every table is watch-backed, so its fields update
live as the cluster changes underneath your cursor. It is still a *view of
what korvid has watched* — a describe, a log stream, or an agent tool
issues its own fresh read against the API server. Press `?` at any time to
see the complete effective key set for the current view, including any
remaps from your config.

Use `:` to select a resource view. Tab completion includes both discovered
resource aliases and built-in commands. When API groups share a resource name,
use the qualified plural, such as `:deployments.apps` or
`:helmreleases.helm.toolkit.fluxcd.io`. The latter opens Flux resources;
`:helm` and `:helmreleases` continue to open the Secret-backed Helm browser.
Qualified views retain their resource identity through watches, describe, and
drill-down.

korvid discovers the cluster's API resources when it connects. A kind the
cluster starts serving later, such as a CustomResourceDefinition a Helm chart
or an operator installs during the session, opens without a restart:

- **An unknown `:` command reads discovery once more** before it is reported
  as unknown, then opens the view if the cluster now serves that kind. To
  spare the API server repeated typos, an unknown command reads it again at
  most once every 30 seconds.
- **`:rediscover`** (or **Refresh API resources** in the Action Palette) reads
  it now and says what changed: kinds added, kinds no longer served, and
  kinds now served at another version.
- **What could not be read is kept.** When a group version cannot be read,
  the kinds korvid already knew from it stay, and the notification names it.
  A read that a context switch overtakes is discarded, and none starts while
  a switch is under way.
- **A name keeps its meaning.** A name such as `:widgets` stays on the
  resource it already opened, even when a newly installed CRD from another
  API group has the same plural; open that one by its qualified name. A
  name whose resource is no longer served is dropped, not given to another.

## Ask in the way you already think

Three keys answer three different questions, so none of them replaces another:

- `:` is for a destination you can name — `:deployments`, `:ns prod`,
  `:ctx staging` — plus the built-in commands, with tab completion over
  discovered aliases.
- `?` is the exhaustive reference: every effective key for every view,
  including your remaps.
- `Ctrl-P` searches by intent. Type `scale`, `restart`, or `context` and
  korvid ranks matching app actions and `:` commands, each row reading
  *category · what it does · how to run it*. Selecting a row runs the same
  route that key runs, so a write still opens the same approval dialog and
  still waits for a fresh keystroke. Canonical spellings match exactly too:
  an action's own id (`delete_resource`), a command's text (`pulse`), and
  its `:` form (`:pulse`). An action that does not apply to the current
  view — or that applies but would do nothing right now, with nothing
  selected, no hint on the row, no search running, or a capability this
  session started without — stays searchable and
  shows why instead of disappearing. On a terminal too short to spare the
  room, the palette takes the height it needs so that reason stays readable
  rather than clipped — and resizing the terminal under the open palette
  re-draws the row you were on whole at the new size instead of cutting its
  last words. `Esc` closes the palette.

After an approved direct-TUI Deployment scale or rollout restart, korvid
separately tracks whether the controller converges. API acceptance is never presented as
completion. Open the newest retained result with `:outcomes` or **Deployment
operation result** in the Action Palette. The bounded tracker reports
completed, stalled, superseded, replaced, stopped, or incomplete evidence and
retains at most three results for roughly five minutes of observation.
Context switching stops old-cluster trackers before the client is replaced.
From the result screen, Pod navigation, events, describe, and logs revalidate
the exact Pod UID; a replacement is refused instead of silently retargeted.

## Follow one signal

Not sure which resource to inspect? The one-line [Pulse summary](pulse.md)
continuously exposes current findings, recent Warning observations, and gaps
without moving the workspace. `:pulse` / `:problems` open the detail; Enter
verifies the selected resource's UID before using normal navigation. Recent
events are not a claim that the problem is still active.

A typical investigation starts at a troubled row and ends at its cause:

1. `/` filters the table down to the workload you care about (name,
   `~fuzzy`, `/regex/`, a label selector, or `-s` to hide Completed).
2. `d` describes the selected resource — its manifest and recent events —
   for the full picture behind a status like `CrashLoopBackOff`.
3. `l` opens the log pane for the selected pod (`L` merges the first 8
   currently filtered pods instead).
4. `g` opens the operational relationship graph: the resource's direct
   dependencies and its direct dependents — one hop each way — with a
   coverage banner stating how complete that view is. `d` toggles a
   bounded transitive expansion of the dependents side; see
   [resource-relationships.md](resource-relationships.md) for its depth and
   node caps.

## Work with logs

`L` streams the first 8 currently visible pods' logs together, each line
prefixed `[pod/container]`; when more pods match, a notification names the
cap and how many matched rather than quietly widening the stream. `f`
toggles between raw text and colour-highlighted JSON for lines that look
like JSON; `p` reloads the pane from the previous (terminated) container
instance. `/` opens inline search in the pane, and `n` / `N` jump between
hits. The pane holds a bounded ring buffer of 5000
lines — a one-time banner marks the pane once older lines have been
dropped. Streams reconnect automatically on a transient error or an
unexpected EOF; after five consecutive failed attempts the header shows an
error state and a notification is raised.

### Configuration propagation

Changing a ConfigMap or Secret does not always change what a running
container sees. Select one in its table and run `:consumers` (or
**Explain configuration propagation** in the Action Palette) to see each Pod
container in its namespace that uses it, grouped by how:

| How it is used | What a change means |
|---|---|
| Environment variables (`envFrom`, `env[].valueFrom`) | Read when the container starts; a change needs a rollout restart, which asks for your approval |
| A mounted volume, plain or projected | Updated in place by the kubelet, eventually, unless the object is immutable; whether the application rereads it is not verified |
| A `subPath` or `subPathExpr` mount | Never updated; a change needs a rollout restart |
| A volume no container mounts | Nothing reads it |
| An image pull secret | Used only when an image is pulled |

Init and ephemeral containers get notes of their own: a finished init
container sees a change only when its Pod restarts, and an ephemeral
container cannot be restarted, so a rollout leaves it behind: a new one is
needed for it to read a changed environment variable or `subPath` file.

The view reads the object's metadata and the namespace's Pods when it opens,
never the object's data. Pods arrive whole, so their literal `env[].value`
fields reach korvid too, but the view keeps and shows only references, never
a value. Each source gets a
line saying what was read, its state and how old it is, and an open view
marks it stale once it ages. "No Pod references it" appears only when every
Pod could be listed; a denied, truncated or stale list says why the set of
consumers is not established. CSI and other volume plugins are not checked. Whether the object is
immutable is never claimed, because only its metadata is read. Enter on a
row opens that Pod.

The view also tells you whether an environment variable or `subPath` row is
behind a change. Each run records the object's resource version. When
you reopen the view after that version moved, an **Object change** column
appears, and a note bounds the latest change seen: after the request that last saw the
old version was sent, and before the response with the new one arrived.
Each environment and `subPath` row says the object **changed after this
container started**, **changed before this container started**, or **not
established**. The container's start is its current `state.running.startedAt`.
A container that is not running has no start, so its row says not
established. The node's clock and korvid's are allowed to differ by up to
one minute, so a change within a minute of the start is not established
either.

These limits apply:

- Only changes korvid saw count: the view must have been open both before
  and after the change, in the same context and session, against the same
  object incarnation.
- A new resource version can carry only a metadata change, so the view says
  the object changed, not that its data did.
- korvid keeps no history across restarts and never reads
  `creationTimestamp` as a change time.

### Workload creation failures

A Deployment can exist, or be scaled up, while its Pods are rejected before
they are created: the Pod list is empty, so nothing on it explains why.
Select a Deployment, ReplicaSet, StatefulSet, DaemonSet, Job or CronJob and
run `:blocked` (or **Explain workload creation failures** in the Action
Palette). The view shows the failures recorded for that workload next to its
namespace's ResourceQuotas and LimitRanges, and what its next Pod could run
into:

| Section | What it shows |
|---|---|
| Header | The workload and the counters its controller last reported, such as desired and created replicas |
| Recorded creation failures | Each `FailedCreate` event about the workload or its ReplicaSets or Jobs, and each `ReplicaFailure` condition, with how often and when it was last recorded, and what the rejection says |
| ResourceQuotas | Each quota's `used / hard` values, grouped as compute, storage, object counts and other, with a resource at or over its limit marked, and its scopes |
| LimitRanges | Each constraint: defaults, minimums, maximums and limit-to-request ratios |
| Possible constraints | What the next Pod, as its template asks, could run into under the policies as now observed: a quota it would exceed or that requires a field it leaves unset, a Pod count at its limit, a LimitRange minimum, maximum or ratio, and, for a StatefulSet, the same for the claims a new Pod would create |

How to read it:

- **A rejection is attributed only as far as its message goes.** A message
  that names a quota links to it. A LimitRange message is attributed to "a
  LimitRange in this namespace", because LimitRanger messages do not say
  which. A rejection an admission webhook or a ValidatingAdmissionPolicy
  wrote is shown verbatim and attributed to nothing.
- **Quantities are shown as the cluster wrote them**, and compared exactly:
  `1Gi` is never rounded or converted, and `0.5` equals `500m`.
- **Times are kept apart.** A rejection was recorded at its own time; the
  quotas are as read now. Quota status is not a reservation, and the next
  create may still differ.
- **Absence is said only when it is a fact.** "No ResourceQuota" appears
  only when every quota could be listed. A quota whose status has not caught
  up with its spec is marked, and is never called "at its limit". Events
  expire, so finding no recorded failure does not show that none happened.
- **A possible constraint is never a cause or a forecast.** It is computed
  from the template, after the LimitRanges' Container defaults, against the
  policies as read now. Quota status is not a reservation, other Pods are
  created concurrently, and admission webhooks are not observed, so the next
  create may still differ. A row about a claim holds only if the claim is
  created: a new Pod reuses a claim that already exists. "No ResourceQuota or
  LimitRange observed constrains this template" appears only when every
  quota and LimitRange was read and none applies. Otherwise the view says
  why that is not established, for example a RuntimeClass, whose overhead is
  not read, or a claim with no storage class, which may get the cluster's
  default.

The view reads the workload, its ReplicaSets or Jobs, the namespace's
Events, ResourceQuotas and LimitRanges when it opens, and nothing else. The
workload's Pod template arrives whole, but only its requests, limits and
scheduling fields are kept: no environment value, command or image. Enter on
a row opens the object it is about. Nothing is changed: korvid does not
scale, retry or edit a policy for you.

## Change scope without losing context

korvid always watches exactly one explicit scope. The startup namespace
resolves from `-n`/`--namespace`, then `namespace:` in
`~/.config/korvid/config.yaml`, then your kubeconfig context's namespace,
then `default`. Switch with `:ns <name>`, the `:ns` picker, `0` for
all-namespaces, or a `1`–`9` [namespace shortcut](#namespace-shortcuts). A
watch denied by RBAC stops with one concise notice instead of retrying or
fanning out into other namespaces.

Both `:ns <name>` and the namespace picker preserve the current resource view;
selecting a namespace while browsing deployments does not switch to pods.

### Namespace shortcuts

Keys `1`–`9` are namespace slots. Your `favorite_namespaces` are pinned to
the first slots in their configured order. The first time you switch to
another namespace with `:ns <name>`, `:<view> <name>` or the namespace picker,
it takes the lowest free slot, so the namespaces you actually work in get
numbers in the order you first visit them. A namespace the latest listing does
not contain (a typo) gets no slot. Pressing a slot key only switches scope,
and listing namespaces never assigns a slot or navigates.

Automatic slots are stable. A namespace keeps its number on every later
refresh and after a restart: korvid saves the assignment per cluster (context
name and API server) in `$XDG_STATE_HOME/korvid/namespace-slots.json`. A
namespace that disappears from the listing keeps its number but is marked
unavailable, so the key is refused instead of reused. When it reappears, the
same key works again.

`?` lists the current slots, and the `:ns` picker shows each namespace's slot
number. Once all nine are taken, other namespaces stay reachable through the
picker and `:ns <name>`. To repack the slots, run `:slots` (or `:ns-slots`).
It drops the namespaces that are no longer listed and moves the rest up, in
their current order, so the freed numbers come back. It previews every slot
that would change and saves only after you confirm with `Enter`; `Esc` keeps
the current slots. `:slots` touches neither your configuration nor your
keybindings.

If the namespace listing fails or RBAC denies it, korvid keeps the last known
slots, marks the map as stale in `?`, and never probes namespaces one by one.
Pinned slots and `:ns <name>` keep working. After a `:ctx` switch, only pinned
slots answer until the new cluster's saved slots are restored.

`:ctx` switches kubeconfig context. korvid probes the target first — loads
its credentials in isolation and runs a self-access review — so an
unreachable or expired context fails with a toast while you stay connected
to the current cluster; only a successful probe tears down watches,
port-forwards, and log streams and retargets everything at the new
cluster. `Ctrl-W v` splits the workspace into two independent panes (own
kind, namespace, and filter); `Ctrl-W w` moves focus, `Ctrl-W q` closes the
focused pane.

## Shape the table

Add columns sourced from labels, annotations, or a bounded JSONPath subset
under `views:` in `config.yaml`:

Use a qualified key such as `helmreleases.helm.toolkit.fluxcd.io` to configure
one API group independently. Qualified settings take precedence over a bare
plural default, and the same choice is used for values, headers, sorting, and
agent resource listings. The synthetic Helm browser does not inherit a Flux
view's columns.

```yaml
views:
  pods:
    columns:
      - name: TEAM
        label: team
```

Custom-column values support Rich markup. Malformed markup, such as an
unmatched `[/]`, is displayed literally instead of stopping the TUI, including
when a watch updates the value.

The pods table's `%CPU/R` / `%MEM/R` columns are always relative to the
declared request (`CPU` and `MEM` show absolute usage), but their colour
keys off the most severe **limit** the usage approaches across every
applicable ceiling (container and, on K8s 1.34+, pod-aggregate) — never off
the request alone. Only when no limit bounds the usage does the colour fall
back to the request ratio, capped at yellow: bursting above a request is
expected, never critical. Without `metrics.k8s.io` installed the columns
show `-` and korvid keeps polling, picking up a later install with no
restart.

## Preview impact before a write

Delete, rollout restart, a known workload scale-down, and Pod resize show
an **advisory** graph-derived impact section above the dry-run preview in
their approval dialog — see [Operations](ops.md) for the full guarded-write
path all of them share. The section is matched to the selected row by
**exact identity, UID included**: when a row carries no UID the section is
omitted entirely rather than guessing by name.

For example, `S` (scale) only loads and summarizes this section when
korvid can tell the requested count is a *known decrease* — a scale-up, a
no-op, or a row whose current count can't be read gets the ordinary
`old -> new` confirmation with no graph section at all.
