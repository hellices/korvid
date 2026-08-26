# Keybindings

Korvid shows only the keys that act on the current view. Press `?` for the complete effective set, including remaps; press `~` to expand the top-bar legend.

<img class="docs-keymap" src="../assets/keybindings-context-map.svg" width="460" height="820" alt="Context map of Korvid's keys by context. GLOBAL keys : ? 0 — command bar, effective-key help, namespace scope — work in every view and lead to TABLE keys Enter d g l, where / filters the table. TABLE branches down to LOGS keys / f w p — search, follow, wrap, previous — and across to guarded WRITE keys r S Ctrl-D, restart, scale and drain, each of which still requires a fresh approval keystroke.">

## Move and inspect

| Key | What it does |
|---|---|
| `:` | Open the command bar |
| `?` | Show the effective keys for every view |
| `~` | Expand or collapse the top-bar legend |
| `/` | Filter a table or search the log pane |
| `Enter` / `Esc` | Drill in / return one level |
| `0` / `1`–`9` | Change namespace scope |
| `d` | Describe the selected resource |
| `g` | Open operational relationships |
| `l` / `L` | Open selected or merged pod logs |
| `Ctrl-W v/w/q` | Split, focus, or close a workspace pane |
| `Ctrl-A` / `Ctrl-X` | Toggle the Agent / stop its current turn |
| `q` | Quit |

## Act in context

| Context | Keys |
|---|---|
| Pods | `l` logs · `s` shell · `Shift-F` port-forward |
| Workloads | `r` restart · `S` scale |
| Nodes | `c` cordon · `u` uncordon · `Shift-D` drain |
| Helm | `i` install · `u` upgrade · `r` rollback |

## Remap an app action

```yaml
keybindings:
  delete_resource: ctrl+k
  sort_by_age: z
```

Unknown, duplicate, or shadowing remaps warn and are skipped. Keys handled by drill-down, closing, and dialogs are not remappable. The approval dialogs' confirm keys are **not remappable**: every write still requires the fixed fresh keystroke. Action names come from the app itself; an unrecognised name is skipped at startup with a warning that lists every valid action name. Press `?` for the complete effective set.
