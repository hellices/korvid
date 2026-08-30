# Keybindings

Korvid shows only the keys that act on the current view. Press `?` for the complete effective set, including remaps; press `~` to expand the top-bar legend.

<img class="docs-keymap" src="../assets/keybindings-context-map.svg" width="460" height="1012" alt="Korvid key contexts. GLOBAL keys : ? 0 lead to TABLE. TABLE keys Enter d g / T inspect resources, filter tables, and open the bounded timeline. PODS keys l L Ctrl-T open logs, merged logs, and file transfer; PODS leads to LOGS. LOGS keys / f w p search, toggle JSON/raw format, wrap, and show previous logs. TABLE branches to guarded WRITE. WRITE keys r S Ctrl-D restart, scale, and delete; each requires a fresh approval keystroke.">

## Move and inspect

| Key | What it does |
|---|---|
| `:` | Open the command bar |
| `?` | Show the effective keys for every view |
| `~` | Expand or collapse the top-bar legend |
| `/` | Filter a table or search the log pane |
| `Enter` / `Esc` | Drill in / return one level |
| `0` | Toggle all namespaces |
| `1`–`9` | Jump to a configured favorite namespace |
| `d` | Describe the selected resource |
| `g` | Open operational relationships |
| `T` | Open the bounded session timeline |
| `l` / `L` | Open selected or merged pod logs |
| `Ctrl-T` | Transfer files to or from the selected pod |
| `Ctrl-W v/w/q` | Split, focus, or close a workspace pane |
| `Ctrl-A` / `Ctrl-X` | Toggle the Agent / stop its current turn |
| `q` | Quit |

## Act in context

| Context | Keys |
|---|---|
| Pods | `l` logs · `s` shell · `Shift-F` port-forward |
| Deployments / StatefulSets | `r` restart · `S` scale |
| DaemonSets | `r` restart |
| ReplicaSets | `S` scale |
| Nodes | `c` cordon · `u` uncordon · `Shift-D` drain |
| Helm releases | `i` install · `u` upgrade · `h` revisions |
| Helm revisions | `r` rollback |

## Remap an app action

```yaml
keybindings:
  delete_resource: ctrl+k
  sort_by_age: z
```

Unknown, duplicate, or shadowing remaps warn and are skipped. Keys handled by drill-down, closing, and dialogs are not remappable. The approval dialogs' confirm keys are **not remappable**: every write still requires the fixed fresh keystroke. Action names come from the app itself; an unrecognised name is skipped at startup with a warning that lists every valid action name. Press `?` for the complete effective set.
