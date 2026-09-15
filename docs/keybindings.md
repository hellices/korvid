# Keybindings

Korvid shows only the keys that act on the current view. Press `?` for the complete effective set, including remaps; press `~` to expand the top-bar legend.

<img class="docs-keymap" src="../assets/keybindings-context-map.svg" width="460" height="1012" alt="Korvid key contexts. GLOBAL keys : ? 0 lead to TABLE. TABLE keys Enter d g / T inspect resources, filter tables, and open the bounded timeline. PODS keys l L Ctrl-T open logs, merged logs, and file transfer; PODS leads to LOGS. LOGS keys / f w p search, toggle JSON/raw format, wrap, and show previous logs. TABLE branches to guarded WRITE. WRITE keys r S Ctrl-D restart, scale, and delete; each requires a fresh approval keystroke.">

## Move and inspect

| Key | What it does |
|---|---|
| `:` | Open the command bar |
| `:pulse` / `:problems` | Inspect [current problems, recent warnings, and coverage](pulse.md) |
| `Ctrl-P` | Search app actions and built-in commands by intent |
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

`Ctrl-P` opens the Action Palette: type what you want to do — `scale`, `logs`,
`context` — and korvid ranks the app actions and the built-in `:` commands
against it. Canonical spellings match exactly too: an action's own id
(`delete_resource`, `logs_multi`), a command's text (`pulse`, `ai`), and its
`:` form (`:pulse`, `:agent`). Each row reads *category · what it does · how
to run it* — the key that action currently answers to, remaps included, or
the command's canonical `:` spelling.
Resource views (`:pods`, `:deploy`) are not rows: they come from the live alias
table, so `:` stays the way to open one. `:q` is not a row either — the bound
Quit action is its single entry. The `1`-`9` favorite-namespace shortcuts are
not rows either — they take a number the palette has no generic way to invoke,
so `?` documents them and `:ns`/the numeric keys stay the route. An action
that does not apply to the current view — or that applies but would currently
do nothing, because nothing is selected, the row carries no hint, no search
is running, the view does not render the column a sort key sorts by, the log
pane is already full, or this session was started without the capability it
needs — stays searchable
and shows the reason instead of vanishing, so
the palette also answers "why did that key do nothing?". A reason is written
to fit a row on a narrow terminal, so where the key's own toast names a
resource (the node a running drain is evicting, the node cordon is waiting
on, the pod that would not fit the log pane) the row states the fact and the
toast keeps the name. Such a row is greyed but not skipped: the arrow, Page
and Home/End keys put the cursor on it like any other row so the reason can
be read, and clicking it does the same. It still cannot run — Enter and a
click on it dispatch nothing and leave the palette open. `Esc` closes the
palette, and so does `Ctrl-P`, which the modal binds as a close key of its
own.
Selecting a write action opens the same approval dialog the key opens, which
still needs its own fresh keystroke.

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

Unknown, duplicate, or shadowing remaps warn and are skipped. Keys handled by drill-down, closing, and dialogs are not remappable. The approval dialogs' confirm keys are **not remappable**: every write still requires the fixed fresh keystroke. The palette's own key moves like any other, under the action name `open_action_palette`; the modal's close keys do not move with it, so `Esc` and the built-in `Ctrl-P` both close the palette whichever key opened it. Action names come from the app itself; an unrecognised name is skipped at startup with a warning that lists every valid action name. Press `?` for the complete effective set.
