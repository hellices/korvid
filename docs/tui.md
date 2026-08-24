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

The status row at the top is always on screen: current kube context, the
active namespace scope (or `all`), and — collapsed by default — the few keys
that matter most in the current view (`~` expands the full grouped legend).
The selected row is not a snapshot: every table is watch-backed, so its
fields update live as the cluster changes underneath your cursor. Press `?`
at any time to see the complete effective key set for the current view,
including any remaps from your config.

## Follow one signal

A typical investigation starts at a troubled row and ends at its cause:

1. `/` filters the table down to the workload you care about (name,
   `~fuzzy`, `/regex/`, a label selector, or `-s` to hide Completed).
2. `d` describes the selected resource — its manifest and recent events —
   for the full picture behind a status like `CrashLoopBackOff`.
3. `l` opens the log pane for the selected pod (`L` merges every currently
   filtered pod instead).
4. `g` opens the operational relationship graph: the resource's direct
   dependents and dependents-of-dependents, with a coverage banner stating
   how complete that view is.

## Work with logs

`L` streams every currently visible pod's logs together, each line prefixed
`[pod/container]`. `f` toggles between raw text and colour-highlighted
JSON for lines that look like JSON; `p` reloads the pane from the previous
(terminated) container instance. `/` opens inline search in the pane, and
`n` / `N` jump between hits. The pane holds a bounded ring buffer of 5000
lines — a one-time banner marks the pane once older lines have been
dropped. Streams reconnect automatically on a transient error or an
unexpected EOF; after five consecutive failed attempts the header shows an
error state and a notification is raised.

## Change scope without losing context

korvid always watches exactly one explicit scope. The startup namespace
resolves from `-n`/`--namespace`, then `namespace:` in
`~/.config/korvid/config.yaml`, then your kubeconfig context's namespace,
then `default`. Switch with `:ns <name>`, the `:ns` picker, `0` for
all-namespaces, or `1`–`9` for your configured `favorite_namespaces`. A
watch denied by RBAC stops with one concise notice instead of retrying or
fanning out into other namespaces.

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

```yaml
views:
  pods:
    columns:
      - name: TEAM
        label: team
```

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
