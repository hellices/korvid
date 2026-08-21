# UI controllers

`ui/app.py` is being decomposed into focused controllers (issue #187). This
records where the boundaries are, so the next extraction has something to
follow and a reviewer can tell at a glance what a controller is allowed to
touch.

## The shape

```
KorvidApp  (ui/app.py)
│
│  owns: Textual composition and message translation, screens and modals,
│         navigation and scope, the write-approval perimeter, run_worker
│         ownership, and the audited execution path
│
│  reached through: WriteGate, ViewState, UiSurface
│
├── HelmController      (ui/helm_controller.py)      install/upgrade/rollback/uninstall
├── OperatorController  (ui/operator_controller.py)  OLM subscribe/approve/uninstall
├── ForwardController   (ui/forward_controller.py)   port-forward sessions
├── ShellController     (ui/shell_controller.py)     pod exec, debug fallback, node shell
├── TransferController  (ui/transfer.py)             post-approval file transfer
├── DebugController     (ui/debug.py)                gated kubectl debug runs
├── SessionTimelineController
│                       (ui/session_timeline_controller.py)  timeline producers and modal navigation
├── RelationshipSnapshotLoader
│                       (ui/relationship_controller.py)  bounded read-only graph LISTs
├── LogController        (ui/log_controller.py)          log stream tasks, buffer, pane lifecycle
├── WorkspaceState       (ui/workspace_state.py)         split-pane collection, focus, and view state
└── ...
```

Controllers do **not** import `app.py`. Dependencies arrive in the
constructor, and the load-bearing ones arrive as *named interfaces* rather
than anonymous callables.

### The relationship snapshot loader is a read-only outlier

`RelationshipSnapshotLoader` (issue #281) is listed above but does not follow
the controller shape, deliberately:

- it takes no `WriteGate`/`ViewState`/`UiSurface` — it never writes, never
  pushes a screen, and never starts a worker. Its only dependency is a
  `Lister` protocol (one `list_objects` method) plus its own bounds;
- every `load()` call is independent and holds no state across calls, so
  there is nothing for a `:ctx` switch to invalidate inside it;
- the app owns the flow around it: `action_relationships` starts the
  exclusive `relationships` worker (`exit_on_error=False`),
  `_load_relationships` re-checks the context epoch and the screen stack
  before pushing `RelationshipScreen`, `on_worker_state_changed` reports a
  failed load instead of exiting, and `_teardown_for_context_switch`
  cancels and awaits the whole group before the client is swapped;
- `RelationshipScreen` (`ui/widgets/relationship_screen.py`) renders one
  already-built graph and dismisses with a navigation target, which the app
  translates back into its ordinary `_jump_to_object` path — the screen
  never navigates on its own.

Because it holds no worker and no app reference, it needs none of the seams
below; it is included here so the inventory of what lives in `ui/` stays
complete.

### The log controller owns its own stream tasks

`LogController` (issue #187) owns the log subsystem's mutable state — the
live stream tasks, the shared display buffer, the reconnect/error flags, the
selected `(namespace, pod, container)` triples, the log-pane generation
counter, the pane display mode (`""`/`l`/`L`/`p`), and which workspace pane
owns the pane — together with the workflows that drive them: the `l`/`L`
open/toggle actions, the live and previous-log stream lifecycles with the
reconnect policy, and the display actions (`f`/`w`/`t`/`Ctrl-S`/`p`/`n`/`N`).
`KorvidApp` keeps only the Textual `action_*`/`agent_open_logs` entry points
as one-line delegates.

It differs from the shape above in two deliberate ways:

- **It supervises raw `asyncio.Task`s, not `run_worker` workers.** One task
  per panel, cancelled and reaped by the controller on reopen (`cancel_tasks`),
  close (`close`), and app teardown (`shutdown`, called from `on_unmount`).
  The fan-out and reconnect bookkeeping (which task ended, whether all streams
  ended, first-overflow banner) is simpler when the controller manages the set
  directly, and the streams must inherit the app context the `AppUIBridge`
  dispatch installs — spawning them as workers would not preserve that. This
  is the one place `UiSurface.run_worker`'s supervision is traded for direct
  ownership.
- **It reaches the pane through a narrow `LogPaneView` accessor, not
  `ViewState`.** The concrete `LogPane` widget stays mounted by Textual
  composition; the controller drives only `open`/`close`/`feed`/`replay`/
  `set_state`/banner/search/toggle through a structural `Protocol`, so its
  non-widget logic is unit-tested against a fake pane with no running app.
  Everything else — the selected pod, the visible rows, the container list,
  the focused-pane token used for split ownership, the context epoch and its
  guards, and the footer refresh — arrives as constructor-injected callables
  read at call time, so a `:ctx` retarget of `stream_logs` is observed. It
  takes `UiSurface` only for `notify`; approvals never apply because log
  streaming is a read.

`agent_open_logs` stays on the app: it owns the agent-priority checks (screen
stack, approval dialog) and the stale-generation guards that read
`LogController.pane_gen` around `cancel_tasks`, then calls `open_agent_logs`
once those clear.

### WorkspaceState owns the split-workspace state

`WorkspaceState` (`ui/workspace_state.py`, issue #48) owns the mutable state of
the split workspace: the pane collection (`PaneState` objects), the focused-pane
index, the monotonic table-id counter that names each `ResourceTable`, the
`ctrl+w` chord-pending flag, and — through the focused pane — the view state the
whole app reads (`current_kind`, `current_scope`, `current_namespace`,
`filter_pattern`, the `ResourceFilter`, per-kind `sorts`, and the `drill`
`NavigationStack`). It also owns the *pure transitions* over that state:
`split`, `focus_other`/`focus_index`/`focus_by_table_id`, `close_focused`, and
`collapse`, each returning the panes affected so the app can mount or unmount the
matching widget.

It is not a controller. It has no Textual import, no worker, and none of the
`WriteGate`/`ViewState`/`UiSurface` seams — it is a pure-Python state object plus
its transitions, unit-tested directly in `tests/ui/test_workspace_state.py` with
no running app. The division of labour with `KorvidApp` is deliberate:

- **`WorkspaceState` decides, `KorvidApp` renders.** The transitions mutate only
  in-memory state and never touch the DOM; the app calls them inside its
  `_nav_lock` and then mounts/unmounts the `ResourceTable` widget for the pane
  the transition reports. Because the transition is total and side-effect-free,
  the app never has to reconstruct "which pane changed" from widget state.
- **The raw fields are private to the owner.** `KorvidApp` no longer holds
  `_panes`, `_focused_pane`, `_pane_counter`, or `_pane_chord_pending`, and
  keeps **no** compatibility proxy for them — reaching pane state means going
  through `self._workspace`. The action surface the rest of the app already used
  (`_pane`, `current_kind`/`current_scope`, `filter_pattern`, `_resource_filter`,
  `_sorts`, `_drill`) is retained as thin delegation properties that read and
  write straight through the workspace, and `AppViewState` reads
  kind/scope/namespace from it directly.
- **Shared discovery state stays on the app.** The alias map (`aliases`) is a
  live dict created in `__main__.py` and mutated by background discovery
  workers, so it is not workspace-owned; moving it would be cosmetic delegation,
  not ownership, and it stays where the workers can reach it.

### Why interfaces and not callables

The first extraction used one callable per dependency and `HelmController`
ended up with 21. Measuring what the remaining areas would need showed that
does not scale:

| Area | Methods | Distinct app attributes used |
|---|---:|---:|
| OLM | 15 | 26 |
| port-forward | 14 | 27 |
| logs | 24 | 36 |
| shell / debug | 19 | 41 |

Worse than the count, `Callable[..., bool]` erases the argument contract. The
approval and revalidation hooks carry the security-relevant keywords
(`action`, `meta`, `op_factory`, `epoch`), and under an ellipsis mypy stops
checking them — dropping `epoch=epoch` from a context check type-checked
clean. As `WriteGate` it fails with *Missing named argument "epoch"*.

So the boundaries that matter are named:

- **`WriteGate`** (`ui/write_gate.py`) — approval, revalidation, the
  fail-closed audit precondition, and the context epoch. One implementation,
  `AppWriteGate`, adapting the app. Approval has two typed entry points,
  `confirm` and `confirm_interactive`; they differ in who records the intent
  audit, which is spelled out below.
- **`ViewState`** (`ui/view_state.py`) — what the user is currently looking
  at: the focused kind and scope, alias resolution, the selected row, and a
  `resources(kind, scope)` query. Read-only structurally, not just by
  convention: there are no setters, `aliases()` returns a `Mapping` view,
  and neither the `ResourceStore` nor `KorvidConfig` is exposed — the
  first would let a controller `clear` the view, and the second is only
  shallowly frozen, so its `keybindings` and `agent_options` dicts are
  mutable. Configuration arrives as `readonly()` and
  `default_namespace()`, which is all any controller uses. Implemented by
  `AppViewState`.
- **`UiSurface`** (`ui/ui_surface.py`) — the Textual capabilities a
  controller may use: `notify`, `push_screen`, `run_worker`, `cancel_workers`,
  `progress` and screen inspection, plus the terminal capabilities an
  interactive child process needs (`suspend`, `refresh`, `call_from_thread`).
  `push_screen` is generic over the screen's result type,
  so a callback written for a different screen is a type error; `notify`
  takes the `Literal` severity Textual accepts rather than a bare `str`;
  and screens are *asked about* rather than handed over — `screen_depth()`
  and `is_current_screen(screen)` — because the live stack would let a
  controller `pop` or reorder screens, and a live `Screen` carries
  `dismiss` and `app`, which is app access routed around the surface.
  Implemented by `AppUiSurface`.

`UiSurface.run_worker` gives supervision, not context safety: a `:ctx`
switch cancels the groups it knows hold a stale-cluster connection, by
name - `hint-events`, `relationships`, and `timeline-warning-events` (the
last through `SessionTimelineController.stop`, which calls
`UiSurface.cancel_workers`) - so a worker in any other group that was
started before the switch keeps running against the cluster it captured.
Controllers that outlive an await revalidate through
`WriteGate.context_intact` or the epoch they captured.

`AppWriteGate` is an adapter rather than the app inheriting `WriteGate`
because Textual's `App` metaclass conflicts with `ABCMeta` — the same reason
`AppUIBridge` exists.

## What stays on the app

These are deliberately *not* distributed into controllers, because each one
must have exactly one implementation:

| Concern | Why it stays |
|---|---|
| `_push_write_confirmation` | The approval gate for a write that is an API call. Agent writes and user writes enter here and nowhere else. Reached through `WriteGate.confirm`. |
| `_push_interactive_confirmation` | The approval gate for a write whose approved form is an interactive subprocess. Reached through `WriteGate.confirm_interactive`. |
| `_write_context_intact` | Revalidation after an awaited gap, including the `:ctx` epoch check. Reached through `WriteGate.context_intact`. |
| `_run_write` | Audit-before-mutation, fail-closed. Never called by a controller. |
| `run_worker` ownership | Cancellation and exclusivity belong to the app that owns the event loop. |

A controller composes an operation *factory* and hands it to the gate. The
ordering is the invariant, not the location of the helm/kubectl call: a
declined dialog constructs nothing, and the app awaits the factory from its
own worker only after the intent audit record has persisted.

### Two approval contracts, and where the audit lives

`confirm` covers the ordinary case: the gate audits the intent, fail-closed,
and only then awaits the operation. Nothing about the write is unknown to it,
so it can record everything before anything happens.

`confirm_interactive` (#236) exists because the shell flows — the `kubectl
debug` fallback and `kubectl debug node/` — cannot honour that contract. Their
approved form is a subprocess that suspends the app, and the facts worth
auditing only exist once it has started: the pod kubectl actually created, its
uid, and the session's exit outcome. `_run_write` cannot know any of them.

So the split is explicit rather than accidental:

| | `confirm` | `confirm_interactive` |
|---|---|---|
| dialog | gate | gate |
| epoch recheck after the dialog | gate | gate |
| write reservation | gate (`_run_write`) | the flow's `@_tracks_cluster_write` coroutine |
| fail-closed intent audit | gate (`_run_write`) | **the flow** |
| outcome audit | — | the flow |

The invariant is unchanged — no approved write reaches a cluster before an
intent record has persisted — but for the interactive flows it is `debug.run`
and `_run_node_shell` that enforce it, each blocking the subprocess when the
append fails. `tests/ui/test_node_shell.py::test_node_shell_blocked_when_audit_append_fails`
pins that, and it is the reason this row reads "the flow" rather than "gate".

## Late binding

Dependency getters are read at call time, not captured at construction:

- a `:ctx` switch retargets clients (the helm wrapper pins `--kube-context`
  per instance), so a captured client would write to the previous cluster;
- the active scope, kind and namespace change constantly;
- tests patch app methods after constructing the app, and a bound method
  captured in `__init__` would freeze whatever existed at that moment.

`HelmController` takes `helm=lambda: self._helm` rather than `helm=self._helm`
for the first reason, and wraps the editor entry points in lambdas for the
third.

## Completed extraction record

Least coupled first, one responsibility per change, with characterization
tests added before the move where the behaviour is not already pinned.

1. ~~Helm workflows~~ — done (#187)
2. ~~OLM / operator workflows~~ — done (#187)
3. ~~The `WriteGate` / `ViewState` / `UiSurface` seams~~ — done (#187);
   this is what dropped `HelmController` from 21 dependencies to 6
4. ~~Port-forward~~ — done (#187)
5. ~~Shell / debug / node shell~~ — done (#187)
6. ~~Session timeline producers and modal lifecycle~~ — done (the
   post-#187 timeline extraction); `SessionTimelineController` owns the
   watch-delta sink, the Warning-event feed, and the goto/navigate flow
7. ~~Log streams, buffer, and pane lifecycle~~ — done;
   `LogController` owns the stream tasks, display buffer, reconnect/error
   flags, selected triples, pane generation, pane mode, and pane owner, plus
   the `l`/`L` open-toggle, live/previous stream lifecycles, and the display
   actions. The app keeps the Textual entry points as thin delegates.
8. ~~Split-workspace state~~ — done (#48); `WorkspaceState`
   (`ui/workspace_state.py`) owns the pane collection, focused-pane index,
   table-id counter, `ctrl+w` chord flag, and the focused pane's view state,
   together with the pure `split`/`focus`/`close`/`collapse` transitions. The
   app mounts and unmounts the `ResourceTable` widgets and keeps the view-state
   accessors as delegation properties; it holds no raw pane fields.

Issue #238 showed that logs and describe were technically extractable without
introducing a new pane-composition seam, and issue #245 kept describe as a
deliberate low-ROI non-extraction. Logs are now extracted through the narrow
`LogPaneView` accessor (above); describe stays on the app for the same
low-ROI reason. `SessionTimelineController` owns the timeline-specific
boundary; context retargeting stays in `KorvidApp` because it is tied to the
app's epoch management, selected-row capture, and navigation worker ordering.
