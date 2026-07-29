# Helm and operators

Package-manager views: the Helm release browser (with install / upgrade /
rollback) and the OLM operator catalog. All writes here run the same
[safety pipeline](ops.md#the-safety-model) as every other mutation.

## Helm release browser

`:helm` lists the Helm releases installed in the current namespace — no
helm binary required (press `0` to widen the view to all namespaces).
korvid reads the release Secrets Helm 3 stores in the cluster
(`type=helm.sh/release.v1`) and decodes them in place, showing name,
revision, status, chart, and app version, live-updated through the same
watch pipeline as any other view.  `Enter` drills into the release's
revision history (newest first, like `helm history`); `d` describes a
release or a single revision with the decoded metadata and the
user-supplied values — the rendered manifest blob is deliberately left
out.

## Helm install / upgrade / rollback

When a `helm` binary is on `PATH`, the browser gains write actions
(without one the keys explain what's missing and nothing else changes):

| Key | View | Action |
|---|---|---|
| `i` | `:helm` | Install: search-first chart picker (`helm search repo` per keyword), release/version/namespace wizard, optional values in `$EDITOR` |
| `u` | `:helm` | Upgrade the selected release — same wizard, pinned to that release; the picker pre-searches the release's chart name; defaults to reusing the release's current values (`--reuse-values`) |
| `r` | revision drill-down | Roll back the release to the selected revision |
| `Ctrl-R` | chart picker | Manage chart repositories: list configured repos, add one (name + URL), refresh indexes (`helm repo update`) |

The chart picker opens instantly and fetches charts per keyword — an
empty search lists everything, and a loading indicator shows while
`helm search repo` runs.  Repository management only touches the local
helm configuration (`helm repo add`/`update` never talk to the
cluster), so it is a typed form rather than an approval dialog.

Install and upgrade render a preview before the confirmation
dialog: install and upgrade run `--dry-run` (with `--hide-secret`, helm
3.13+, so generated Secrets stay masked), and when the
[helm-diff](https://github.com/databus23/helm-diff) plugin is installed,
upgrade and rollback show a real diff against the live release instead.
If the preview render fails or times out, the confirmation dialog still
opens — just without a preview.
Rollback has a preview **only** with the plugin — without it the
confirmation dialog states the release and target revision but shows no
manifest diff.
Nothing executes until you approve the exact command in the confirmation
dialog, and every run is audit-logged like any other write — no audit
log, no writes.  Values entered through `$EDITOR` are passed via a
private temp file that is deleted as soon as helm returns.  OCI registry
auth stays outside korvid — configure it with the helm CLI itself.

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
