# Helm and operators

Package-manager views: the Helm release browser (with install / upgrade /
rollback / uninstall) and the OLM operator catalog. All writes here run
the same [safety pipeline](ops.md#the-safety-model) as every other
mutation.

## Helm release browser

`:helm` lists the Helm releases installed in the current namespace — no
helm binary required (press `0` to widen the view to all namespaces).
korvid reads the release Secrets Helm 3 stores in the cluster
(`type=helm.sh/release.v1`) and decodes them in place, showing name,
revision, status, chart, and app version, live-updated through the same
watch pipeline as any other view.  `Enter` opens the release's
[hierarchy tree](#hierarchy-tree); `h` drills into the release's
revision history (newest first, like `helm history`); `d` describes a
release or a single revision with the decoded metadata and the
user-supplied values — the rendered manifest blob is deliberately left
out.

## Hierarchy tree

`Enter` on a Helm release, an OLM Subscription, or a CSV opens a read-only
tree of everything that root installed and how the pieces hang together
(issue #120):

```
▼ HelmRelease default/web
  ▼ Deployment/web-nginx
    ▼ ReplicaSet/web-nginx-7d9c  1/1
        Pod/web-nginx-7d9c-x2kf  Running 1/1
    Service/web-nginx
    ConfigMap/web-nginx-config
```

- **Helm roots** list the objects rendered in the release's manifest,
  decoded from the release Secret (no helm binary; one Secret list + get
  per open, nothing per node).
- **Operator roots** prefer the cluster's own bookkeeping, in order: the
  Operator object's `status.components.refs` (includes the CSV, CRDs, and
  RBAC), then the Subscription's InstallPlan `status.plan`, then — for a
  CSV — watched Deployments whose ownerReferences point at it. Where no
  source is available the tree degrades to the root alone.
- **Runtime descendants** (Deployment → ReplicaSet → Pod) come from
  ownerReferences against the live store, so the tree shows what is
  running now, not just what was rendered. Nodes carry live status where
  the store has it (pod phase, ready counts); a declared object the store
  watches but cannot find is marked `(missing)`. The tree refreshes with
  the store while open, keeping the selected object and any branches you
  collapsed.

`Enter` on a node jumps to that kind's real view with the cursor on the
object — logs, describe, edit, delete, and further drill-down all work
there unchanged, and `Esc` there brings the tree back (over the view it
was opened from, cursor still on the node you picked), so drilling into
a component never strands you on an unfiltered list.  `d` describes the
node directly; `Esc` inside the tree returns to the
browser. The tree itself is read-only: every write still happens in the
real views behind the usual approval gates.

## Helm install / upgrade / rollback / uninstall

When a `helm` binary is on `PATH`, the browser gains write actions
(without one the keys explain what's missing and nothing else changes):

| Key | View | Action |
|---|---|---|
| `i` | `:helm` | Install: search-first chart picker (`helm search repo` per keyword), release/version/namespace wizard, optional values in `$EDITOR` |
| `u` | `:helm` | Upgrade the selected release — same wizard, pinned to that release; the picker pre-searches the release's chart name; defaults to reusing the release's current values (`--reuse-values`) |
| `F1` | install/upgrade wizard | The chart's README in a scrollable pager (`helm show readme`) |
| `r` | revision drill-down | Roll back the release to the selected revision |
| `Ctrl-D` | `:helm` | Uninstall the selected release (`helm uninstall`) |
| `Ctrl-R` | chart picker | Manage chart repositories: list configured repos, add one (name + URL, plus an optional CA file for a private-CA repository — passed to `helm repo add --ca-file`), refresh indexes (`helm repo update`) |

The chart picker opens instantly and fetches charts per keyword — an
empty search lists everything, and a loading indicator shows while
`helm search repo` runs.  Repository management only touches the local
helm configuration (`helm repo add`/`update` never talk to the
cluster), so it is a typed form rather than an approval dialog.
Pressing Enter on a repository row browses that repo's charts: the
picker underneath scopes its search to the repo (the `repoName/`
prefix, typed for you) — the natural "what does this repo serve?"
step right after adding one.

Installing a chart you don't know is no longer a blind flight: when
the chart ships a `values.schema.json`, the wizard lists its
**required values** (field path plus type or valid choices — e.g.
`mode: daemonset | deployment | statefulset`) before you install, and
`F1` opens the chart's README without leaving the wizard.  Choosing
"edit in `$EDITOR`" opens on the chart's **own annotated default
values** (`helm show values`) instead of an empty stub — edit what you
need and save; content left unchanged (or reduced to comments) keeps
the chart defaults with no override file.  All of it comes from the
chart itself via `helm show`/`helm pull` — repo-local, no cluster
access, and advisory: a chart without a schema or README simply shows
nothing extra.

Install and upgrade render a preview before the confirmation
dialog: install and upgrade run `--dry-run` (with `--hide-secret`, helm
3.15+, so generated Secrets stay masked; older helm renders an
error-only fallback without the flag, keeping the render verdict), and when the
[helm-diff](https://github.com/databus23/helm-diff) plugin is installed,
upgrade and rollback show a real diff against the live release instead
(if the diff plugin fails, upgrade falls back to the plain `--dry-run`
render; rollback keeps its diff-only preview).
When helm itself rejects the dry-run — a chart with mandatory values
renders an `execution error` without them, for example — the flow stops
*before* the confirmation dialog: the real install would fail the same
way, so korvid shows helm's error and offers to reopen the values
editor (with your previous inputs intact) or retry, instead of letting
you approve a doomed command.  If the preview merely times out or fails
for environmental reasons, the confirmation dialog still opens and
notes that the preview was unavailable.
Rollback has a preview **only** with the plugin — without it the
confirmation dialog states the release and target revision but shows no
manifest diff.
Nothing executes until you approve the exact command in the confirmation
dialog, and every run is audit-logged like any other write — no audit
log, no writes.  Values entered through `$EDITOR` are passed via a
private temp file that is deleted as soon as helm returns.  OCI registry
auth stays outside korvid — configure it with the helm CLI itself.

Uninstall (`Ctrl-D` on a release row) previews the removal with
`helm uninstall --dry-run` and, because it destroys every resource the
release owns, requires **typing the release name** in the confirmation
dialog before it runs.  Release history is removed with the release
(helm's default); the revision drill-down stays read-only apart from
rollback — uninstall only works from the release list.

## Operator uninstall

`Ctrl-D` on a **Subscription** row starts the OLM uninstall flow.  The
approval dialog spells out exactly what will happen before you confirm:

- the Subscription is deleted first,
- then its installed CSV (`status.installedCSV`, uid-pinned so a
  replaced operator is never deleted by mistake) — deleting the CSV is
  what removes the operator's Deployment and RBAC through OLM's garbage
  collection,
- **CRDs and custom resources are kept** — korvid never deletes them, so
  your workloads' custom objects survive the uninstall.

If the Subscription delete fails, the CSV is left untouched (deleting
the CSV alone would just make OLM reinstall it).  Both deletes are
separately audit-logged.

`Ctrl-D` on a **CSV** row redirects to the same flow when korvid can
find the owning Subscription (visit `:subscriptions` first so it is
known); deleting a CSV that still has a Subscription would only trigger
a reinstall.  A CSV with no known Subscription falls back to a plain
resource delete.

## Operator catalog (OLM)

Where [OLM](https://olm.operatorframework.io/) is installed, `:operators`
opens the operator catalog cluster-wide (catalog entries live in catalog
namespaces, so the view defaults to all namespaces; `:operators <ns>`
scopes it): every PackageManifest the cluster's catalog
sources serve, with its catalog, default channel, and available channels.
`:subscriptions`, `:csv`, and `:installplans` show the installed side with
typed columns (subscription channel/state, CSV version/phase).  Without
OLM, `:operators` explains what is missing instead of erroring.

Press `I` on a catalog row to install: a wizard collects the target
namespace, channel (validated against the package's own channel list),
and approval mode — then the **full Subscription manifest** is shown in
the standard approval dialog before anything is created.  On an
InstallPlan row, `I` approves a pending manual plan (the dialog lists the
CSVs the approval unblocks).  Everything shown comes from the cluster's
catalog objects — korvid hardcodes no operator knowledge — and both
writes go through the same SSAR pre-check, approval gate, and fail-closed
audit log as every other mutation.  The AI agent gets a matching read-only
`list_operators` tool (catalog + installed subscriptions); installing
stays behind the UI approval gate.
