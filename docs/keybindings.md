# Keybindings

Korvid shows only the keys that act on the current view. Press `?` for the complete effective set, including remaps; press `~` to expand the top-bar legend.

<img class="docs-keymap" src="../assets/keybindings-context-map.svg" width="1200" height="520" alt="Context map connecting Korvid's global, table, log, and guarded-write keys">

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
  delete_resource: ctrl+x
  sort_by_age: g
```

Unknown, duplicate, or shadowing remaps warn and are skipped. Keys handled by drill-down, closing, and dialogs are not remappable. The approval dialogs' confirm keys are **not remappable**: every write still requires the fixed fresh keystroke.
