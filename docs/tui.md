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
agent disabled.

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
