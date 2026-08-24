# Helm and operators

Package-manager views: the Helm release browser (install / upgrade /
rollback / uninstall) and the OLM operator catalog. All writes here run the
same [safety pipeline](ops.md#one-write-path-three-drivers) as every other
mutation — nothing executes until you approve the exact command, and every
run is audit-logged like any other write.

`:helm` lists the Helm releases installed in the current namespace (`0`
widens to all namespaces) — no helm binary required. korvid reads the
release Secrets Helm 3 stores in the cluster and decodes them in place, live
through the same watch pipeline as any other view. A `helm` binary on `PATH`
additionally unlocks the write actions below; without one the browser
explains what's missing instead of hiding the keys.

<section class="docs-storyboard" aria-labelledby="helm-storyboard-title">
  <div>
    <p id="helm-storyboard-title"><strong>Release lifecycle</strong></p>
    <ol>
      <li><strong>Install</strong><span>A search-first chart picker (`helm search repo`), a release/version/namespace wizard that surfaces a chart's required values and README, and a `--dry-run` preview before the approval dialog.</span></li>
      <li><strong>Inspect</strong><span>`Enter` on a release, Subscription, or CSV opens a read-only hierarchy tree of everything that root installed, down to the live Pods it owns; `h` shows revision history, `d` describes.</span></li>
      <li><strong>Upgrade</strong><span>The same wizard pinned to the release, reusing its current values by default; a real `helm-diff` plan replaces the plain dry-run preview when the plugin is installed.</span></li>
      <li><strong>Rollback</strong><span>Roll back to a selected revision — a diff preview only when the plugin is installed, otherwise the confirmation states the target revision with no manifest diff.</span></li>
    </ol>
  </div>
</section>

Install and upgrade stop *before* the confirmation dialog when helm itself
rejects the dry-run (a chart with unmet mandatory values, for example) —
korvid shows helm's error and offers to reopen the values editor rather than
let you approve a doomed command. Uninstall (`Ctrl-D` on a release row)
previews the removal with `helm uninstall --dry-run` and, because it
destroys every resource the release owns, requires **typing the release
name** in the confirmation dialog before it runs; release history is removed
with the release. Values entered through `$EDITOR` pass through a private
temp file deleted as soon as helm returns.

## Operator uninstall

`Ctrl-D` on a **Subscription** row starts the OLM uninstall flow. The
approval dialog spells out exactly what will happen: the Subscription is
deleted first, then its installed CSV (uid-pinned, so a replaced operator is
never deleted by mistake) — deleting the CSV is what removes the operator's
Deployment and RBAC through OLM's own garbage collection. **CRDs and custom
resources are kept**, so your workloads' custom objects survive the
uninstall. If the Subscription delete fails, the CSV is left untouched
(deleting it alone would just make OLM reinstall it); both deletes are
separately audit-logged. `Ctrl-D` on a **CSV** row redirects to the same flow
when korvid knows the owning Subscription, and falls back to a plain
resource delete otherwise.

## Operator catalog and installation (OLM)

Where [OLM](https://olm.operatorframework.io/) is installed, `:operators`
opens the cluster-wide catalog — every PackageManifest the cluster's catalog
sources serve; `:subscriptions`, `:csv`, and `:installplans` show the
installed side. Without OLM, `:operators` explains what is missing rather
than erroring. Press `I` on a catalog row to install: a wizard collects the
target namespace, a channel validated against the package's own channel
list, and an approval mode, then the **full Subscription manifest** is shown
in the standard approval dialog before anything is created — the same
prerequisite (OLM present) and approval-gate distinctions as every other
write here. On an InstallPlan row, `I` approves a pending manual plan.
Everything shown comes from the cluster's own catalog objects; korvid
hardcodes no operator knowledge. The AI agent gets a matching read-only
`list_operators` tool; installing stays behind the UI approval gate.
