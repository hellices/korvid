# Keybindings

All keys, grouped by the context they act in. The footer legend adapts to the
current view: only the keys that act on the resource kind on screen are shown
(helm's `i`/`u`/`r`, node ops' `c`/`u`/`D`, workload restart/scale, …), and
off-view keys are inert. The in-app help overlay (`?`) always documents every
view and shows the effective keys, including any remaps.

| Key | Context | Action |
|-----|---------|--------|
| `:` | global | Open command bar — accepts `pods`, `deploy all`, `helm`, `ns <name>`, `ctx <name>`, `ai`, `model`, `q` |
| `?` | global | Help overlay — keybindings grouped by context plus `:` commands (Esc/q/`?` closes) |
| `/` | table | Open filter — name, `~fuzzy`, `/regex/`, `!exclude`, `-l k=v`, `-s` hide Completed (Enter keeps, Esc clears) |
| `/` | log pane | Open inline log search |
| `Enter` | table | Drill down: pods → containers; deploy → replicasets (history) → pods; helm release → revisions |
| `Esc` | table | Pop one drill-down level |
| `Ctrl-W` `v` / `w` / `q` | table | Split workspace into two panes / focus the other pane / close the focused pane |
| `Shift-N/A/C/M` | table | Sort by name / age / CPU / MEM (repeat flips ▲/▼; sorts on data, not rendered strings) |
| `0` | global | Toggle all-namespaces view |
| `1`-`9` | global | Jump to a favorite namespace (`favorite_namespaces` config, in order) |
| `d` | table | Describe selected resource (manifest + events) |
| `s` | pods table | Shell into selected pod (`kubectl exec`; offers `kubectl debug` fallback for distroless images) |
| `s` | nodes table | Node shell (`kubectl debug node/`; approval dialog — privileged pod with the host filesystem at `/host`, deleted on exit) |
| `Shift-F` | pods / services table | Port-forward the selected target (local port prompt; prefilled from declared ports) |
| `l` | pods table | Open / close log pane for selected pod |
| `L` | pods table | Merge logs of all currently filtered pods (up to 8) |
| `f` | log pane | Toggle JSON-formatted / raw display |
| `w` | log pane | Toggle line wrap |
| `t` | log pane | Toggle kubelet-timestamp prefix |
| `Ctrl-S` | log pane | Save the current buffer to `$XDG_DATA_HOME/korvid/logs/` (default `~/.local/share/korvid/logs/`) |
| `p` | log pane | Reload pane with previous (terminated) container logs |
| `n` / `N` | log pane | Jump to next / previous search hit |
| `Ctrl-D` | table | Delete selected resource (confirm dialog; cluster-scoped kinds require typing the name) |
| `r` | table | Rolling restart of selected deployment / statefulset / daemonset (confirm dialog) |
| `S` | table | Scale selected deployment / replicaset / statefulset (replica prompt + confirm dialog) |
| `R` | pods table | In-place resize of pod CPU/memory requests/limits (Kubernetes 1.35+; prompt + confirm dialog) |
| `I` | operators tables | Install the selected catalog operator (wizard + confirm dialog) or approve a pending InstallPlan |
| `c` / `u` | nodes table | Cordon / uncordon the selected node (confirm dialog with server dry-run preview) |
| `Shift-D` | nodes table | Drain the selected node — PDB-aware impact preview (evictions, PDB-blocked pods, skipped DaemonSet/mirror pods, emptyDir warnings), typed-name confirm, live progress; press again to cancel mid-drain (node stays cordoned) |
| `e` | table | Edit selected resource manifest in `$VISUAL`/`$EDITOR` (kubectl edit style; confirm dialog before the PUT) |
| `i` | pods table | Open hint details overlay for a troubled pod (full container trouble + recent Warning events) |
| `i` / `u` | helm table | Install a chart / upgrade the selected release (wizard + dry-run preview + confirm dialog; needs `helm` on `PATH`) |
| `r` | helm revisions table | Roll back the release to the selected revision (confirm dialog; needs `helm` on `PATH`) |
| `Ctrl-R` | chart picker | Manage chart repositories: list, add, refresh indexes |
| `Ctrl-T` | pods table | Transfer a file to/from the selected container (exec tar stream; upload needs approval) |
| `Ctrl-A` | global | Toggle AI agent panel |
| `q` | global | Quit |
| `Esc` | log pane | Close pane (or dismiss search / filter bar) |

## Remapping keys

App-level actions can be remapped via the `keybindings:` section of
`~/.config/korvid/config.yaml`, mapping an **action name** from the list
below to a new key (Textual key syntax — `x`, `f1`, `ctrl+q`, `shift+g`).
Keys handled outside bindings (`Enter` drill-down, `Esc` close/pop, and the
dialogs' own keys) are not remappable:

```yaml
keybindings:
  delete_resource: ctrl+x   # free Ctrl-D for the terminal
  sort_by_age: g
```

Action names: `quit`, `help`, `open_command`, `open_filter`,
`toggle_all_namespaces`, `describe`, `shell`, `logs`, `logs_multi`,
`log_format`, `log_wrap`, `log_timestamps`, `log_save`, `log_previous`,
`log_search_next`, `log_search_prev`, `sort_by_age`, `sort_by_cpu`,
`sort_by_mem`, `toggle_agent`, `delete_resource`, `rollout_restart`,
`resize_pod`, `scale_resource`, `edit_resource`, `hint_details`, `operator_install`,
`cordon_node`, `uncordon_node`, `drain_node`, `port_forward`, `transfer`,
`helm_install`, `helm_upgrade`, `helm_rollback`.

Unknown actions, duplicate keys, and keys that shadow another action's
default produce a startup warning and are skipped — never a crash. The
approval dialogs' confirm keys are **not remappable** by design: writes are
only ever confirmed by the fixed keystrokes. The help overlay (`?`) always
shows the effective keys.
