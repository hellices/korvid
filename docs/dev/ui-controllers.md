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
│         and run_worker ownership
│
│  reached through: WriteGate, ViewState, UiSurface
│
├── WriteCoordinator    (ui/write_coordinator.py)    the write security perimeter
├── ResourceWriteController
│                       (ui/resource_write_controller.py)  delete/restart/edit/scale/resize/cordon/drain
├── HelmController      (ui/helm_controller.py)      install/upgrade/rollback/uninstall
├── OperatorController  (ui/operator_controller.py)  OLM subscribe/approve/uninstall
├── ForwardController   (ui/forward_controller.py)   the shift+f dialog and port-forward sessions
├── ShellController     (ui/shell_controller.py)     pod exec, debug fallback, node shell
├── TransferController  (ui/transfer.py)             the ctrl+t journey: dialogs, approval, stream
├── DebugController     (ui/debug.py)                gated kubectl debug runs
├── SessionTimelineController
│                       (ui/session_timeline_controller.py)  timeline producers and modal navigation
├── RelationshipSnapshotLoader
│                       (ui/relationship_controller.py)  bounded read-only graph LISTs
├── LogController        (ui/log_controller.py)          log stream tasks, buffer, pane lifecycle
├── WorkspaceController  (ui/workspace_controller.py)    navigation, drill, hierarchy, relationship, and pane flows
├── WorkspaceState       (ui/workspace_state.py)         split-pane collection, focus, and view state
├── AgentUiController    (ui/agent_ui_controller.py)     agent session, turn tasks, UI bridge reads, agent writes
├── ProposalController   (ui/proposal_controller.py)     external MCP write proposals: intake, review, execution, expiry
├── ContextSwitchCoordinator
│                       (ui/context_switch_coordinator.py)  the `:ctx` epoch and the quiesce/retarget/resume transaction
├── ResourceInspectController
│                       (ui/resource_inspect_controller.py)  describe, Secret masking, container pick, hint details
├── IntegrationController
│                       (ui/integration_controller.py)  `:mcp` on/off/follow and `:tp` status/hint
├── CommandRouter        (ui/command_router.py)          which owner an unresolved `:` command belongs to
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
- `WorkspaceController` owns the flow around it (see below):
  `show_relationships` starts the exclusive `relationships` worker
  (`exit_on_error=False`), `_load_relationships` re-checks the context epoch
  and the screen stack before pushing `RelationshipScreen`, and
  `cancel_relationship_workers` cancels and awaits the whole group before the
  client is swapped. `KorvidApp.on_worker_state_changed` still reports a
  failed load instead of exiting, because worker-state events are dispatched
  to the app that owns the event loop;
- `RelationshipScreen` (`ui/widgets/relationship_screen.py`) renders one
  already-built graph and dismisses with a navigation target, which the
  controller translates back into its ordinary `jump_to_object` path — the
  screen never navigates on its own.

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
  directly, and the streams must inherit the app context `AppContextDispatch`
  installs — spawning them as workers would not preserve that. This
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

- **`WorkspaceState` decides, `WorkspaceController` drives, `KorvidApp`
  renders.** The transitions mutate only in-memory state and never touch the
  DOM; `WorkspaceController` calls them inside its `_nav_lock` and then asks
  the `WorkspaceSurface` to mount/unmount the `ResourceTable` widget for the
  pane the transition reports. Because the transition is total and
  side-effect-free, nothing has to reconstruct "which pane changed" from
  widget state.
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

### WorkspaceController owns the workspace workflows

`WorkspaceController` (`ui/workspace_controller.py`, issue #187) owns the
cohesive workflows over `WorkspaceState` and the workspace-only mutable state
they mutate:

- **resource-view navigation and scope** — `navigate`/`navigate_command`,
  `toggle_all_namespaces`, `favorite_namespace`, the `_navigate_locked`
  kind/scope transition, and `sync_metrics_poller`, all serialized through the
  `_nav_lock` it owns;
- **filter and per-kind sort** — `set_filter`/`clear_filter`, `sort_by`,
  `sort_command`, the sort picker and header-click paths;
- **drill down/pop** — `drill_into`/`pop_drill` with the bounded `prewarm_view`
  and its per-`(kind, scope)` lease accounting in `stop_watch_if_unused`, so an
  overlapping drill never reaps a stream a pane or another pre-warm still needs;
- **the component-hierarchy tree** — `open_hierarchy`/`refresh_hierarchy`/
  `reopen_hierarchy_return`, the OLM/helm ref gathering, and the stale-result
  guard in `_on_hierarchy_pick` that discards a tree action taken across a
  `:ctx` switch;
- **the operational relationship graph** — `show_relationships`,
  `_load_relationships`, `on_relationship_result`, and
  `cancel_relationship_workers`, run in the `relationships` worker group;
- **the two-pane split lifecycle** — the `ctrl+w` chord, `split_pane`,
  `focus_other_pane`, `close_focused_pane`, and the context-switch
  `collapse_split`, with the focus-class, hint, status and binding refreshes;
- **the shared goto** — `jump_to_object`, reused by the hierarchy, relationship
  and timeline flows.

The **workspace-only mutable state** moved with the workflows: `_nav_lock`, the
drill `_prewarm_leases`, the open tree's `_hierarchy_ctx` rebuild inputs, the
hierarchy-goto `_jump_poll_attempts` budget, the `_render_pending`
coalescing set, and the metrics poller's served `_metrics_target`. `KorvidApp`
holds none of them; its action/message handlers for these flows
(`on_navigate_command`, `action_sort_*`, `on_key`, `action_relationships`,
`on_data_table_row_selected`, …) are one-line delegates.

It reaches Textual through three named boundaries plus a few typed collaborator
ports, so its logic is unit-tested in `tests/ui/test_workspace_controller.py`
with no running app:

- **`UiSurface`** for notifications, workers, screens, progress and screen
  inspection — the same surface every other controller uses;
- **`WorkspaceSurface`** (`AppWorkspaceSurface`) for the workspace widgets the
  app still owns: rendering a pane, mounting/removing a pane table, the
  focus-class and empty-state refreshes, the describe-pane dismissal, and the
  open hierarchy tree. The controller constructs the `HierarchyScreen`/
  `RelationshipScreen` and pushes them through `UiSurface`, but never queries an
  arbitrary widget;
- **`ContextGuard`** (`ContextSwitchCoordinator`) for the `:ctx` epoch, the
  in-flight flag, and the stream-read guard it revalidates against across every
  awaited gap. `crossed(epoch)` is the old `_ctx_switch_crossed`.

The **`:ctx`-switch coordinator is `ContextSwitchCoordinator`**
(`ui/context_switch_coordinator.py`), not the app. It calls a narrow
reset/quiesce API on the workspace controller: `quiesce_for_context_switch`
(fold the split, stop and de-target the metrics poller, clear the drill, cancel
the relationship workers, clear the filter — the workspace-only halves of the
teardown) and `reset_view_after_switch` (adopt pods in the new cluster's default
namespace). It serializes with navigation by taking the workspace controller's
`nav_lock`, which `:mcp` toggles and write execution share for the same
reason.

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
  `WriteCoordinator` (`ui/write_coordinator.py`), which *is* the perimeter
  rather than an adapter over one: it is a plain class, so it inherits the
  ABC directly. Approval has two typed entry points, `confirm` and
  `confirm_interactive`; they differ in who records the intent audit, which
  is spelled out below.
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
- **`WorkspaceSurface`** (`ui/workspace_controller.py`) — the workspace
  widgets `WorkspaceController` drives but does not own: rendering a pane,
  mounting/removing a pane table, the focus-class/empty-state refreshes, the
  describe-pane dismissal, and the open hierarchy tree. Widget *lookup,
  construction and mounting* stay in `KorvidApp` behind this named surface, so
  the controller never queries an arbitrary widget. Implemented by
  `AppWorkspaceSurface`.
- **`ContextGuard`** (`ui/workspace_controller.py`) — the `:ctx`-switch epoch,
  the in-flight flag, and the stream-read guard every flow revalidates
  against; `crossed(epoch)` is the old `_ctx_switch_crossed`. It is a narrow
  read-only view of the switch state, deliberately *not* the whole
  `WriteGate`. Implemented directly by `ContextSwitchCoordinator`, which is
  also the code that mutates that state — there is no adapter and no second
  copy of the epoch.
- **`ContextSurface`** / **`SessionConfiguration`**
  (`ui/context_switch_coordinator.py`) — the two app-owned halves of a switch:
  the widgets and UI-bus posts the transaction drives (completion words, the
  describe pane, the inline namespace picker, the hint worker group, the
  status/render refreshes) and the session configuration a proven switch is
  adopted into (active context, default namespace, capability gates,
  context-pinned CLI wrappers). Implemented by `AppContextSurface` and
  `AppSessionConfiguration`; neither runs any part of the transaction.
- **`AgentPanelPort`** (`ui/agent_ui_controller.py`) — the chat panel as the
  agent session may drive it: visibility, the header the live runtime renders
  into, the two unconfigured-state hints, and the transcript operations a turn
  performs. Implemented by `AppAgentPanel`.
- **`AgentScreens`** (`ui/agent_ui_controller.py`) — the screen the agent may
  observe, must not disturb, and may fill: the approval-dialog and
  describe-screen guards, a top-screen identity token for before/after
  comparison, `dismiss_if_current`, the selected row key, and the non-modal
  describe pane. Implemented by `AppAgentScreens`.
- **`AgentProposals`** (`ui/agent_ui_controller.py`) — the three external
  write-proposal tool calls, and nothing else about proposals. Implemented by
  `AppProposalOps`, which still holds the store, review loop and execution
  path (their own extraction is next).
- **`BridgeDispatch`** (`ui/bridge_dispatch.py`) — where a foreign `UIBridge`
  coroutine is allowed to run. `AppContextDispatch` owns the app-context
  snapshot and the in-flight dispatch set: the app activates it on mount and
  shuts it down on unmount, so a pre-mount MCP request or one racing teardown
  is refused as "UI not ready" instead of composing widgets in the caller's
  context (issue #165).

`UiSurface.run_worker` gives supervision, not context safety: a `:ctx`
switch cancels the groups it knows hold a stale-cluster connection, by
name - `hint-events`, `relationships`, and `timeline-warning-events` (the
last through `SessionTimelineController.stop`, which calls
`UiSurface.cancel_workers`) - so a worker in any other group that was
started before the switch keeps running against the cluster it captured.
Controllers that outlive an await revalidate through
`WriteGate.context_intact` or the epoch they captured.

`AppViewState`, `AppUiSurface`, `AppWorkspaceSurface`, `AppContextSurface`,
`AppSessionConfiguration`, `AppAgentPanel`, `AppAgentScreens` and
`AppProposalOps` are adapters rather
than the app inheriting the ABCs, because Textual's `App` metaclass conflicts
with `ABCMeta` — the same reason `AppUIBridge` exists.
`WriteCoordinator` has no such constraint, so it implements `WriteGate`
itself: there is no adapter between the interface controllers call and the
code that enforces it.

## The write security perimeter

`WriteCoordinator` (`ui/write_coordinator.py`) owns the single implementation
of the ordering every cluster mutation passes through, and the perimeter
state that ordering depends on:

| Owned | Why it is here and nowhere else |
|---|---|
| `confirm` | The approval gate for a write that is an API call. Agent writes, controller writes and keybinding writes enter here and nowhere else. |
| `confirm_interactive` | The approval gate for a write whose approved form is an interactive subprocess. |
| `confirm_screen` | Every approval dialog, so the protected-context layer (issue #83) can never be forgotten — the coordinator owns that marker and re-adopts it on each `:ctx` switch. |
| `context_intact` / `identity_intact` / `scale_identity_intact` / `uid_intact_after_fetch` | Revalidation after an awaited gap: the `:ctx` epoch, the screen stack, the selection, the origin pane and its scope, the captured uid, and (for a scale) the captured replica count. |
| `write_target` / `write_origin` / `current_replicas` | Resolving what a keybinding write is aimed at, before the flow's first await. |
| `permitted` / `precheck_keybinding_write` | The SubjectAccessReview pre-check and its one-shot fail-open warning. |
| `audit_write` | The one chokepoint where a write reaches the audit log — and, only after that durable append returned, the session timeline. |
| `run` / `run_shielded` | Audit-before-mutation, fail-closed, under the reservation. |
| `reserve_write` / `reserved` / `active_writes()` | The in-flight cluster-write count `:ctx` switching consults, reserved **synchronously** where the coroutine is constructed. |
| `dry_run_preview` / `impact_preview` | Display support with their own deadlines; they fail open and can never approve, execute or reserve a write. |

`KorvidApp` keeps the Textual action/message entry points that *raise* these
flows, `run_worker` ownership (cancellation and exclusivity belong to the app
that owns the event loop), and the `:ctx`-switch coordinator that consults
`active_writes()`. It holds no duplicate of the ordering and no proxy back
into it: `AppWriteGate` is gone, and controllers receive `self._writes`
directly.

A controller composes an operation *factory* and hands it to the gate. The
ordering is the invariant, not the location of the helm/kubectl call: a
declined dialog constructs nothing, and the coordinator awaits the factory
from a supervised worker only after the intent audit record has persisted.

### The workflows sit above it, and cannot route around it

`ResourceWriteController` (`ui/resource_write_controller.py`) owns the flows a
keybinding raises against the selected row — delete (including the helm and
OLM redirects), rollout restart, the `$EDITOR` round-trip, scale, in-place pod
resize, cordon/uncordon and drain — plus the drain's own lifecycle state
(`_drain_worker`, `_drain_node`), because pressing the drain key again must
find and cancel the worker it started.

It owns *composition*, not security. It holds a `WriteOps` handle for exactly
two read-only purposes — server-side dry-run previews and the drain plan — and
otherwise only builds the operation *factories* `WriteCoordinator.confirm`
constructs after approval. Every approval, revalidation, reservation, audit
record and mutation is the coordinator's. `KorvidApp` keeps
`action_delete_resource`, `action_rollout_restart`, `action_edit_resource`,
`action_scale_resource`, `action_resize_pod`, `action_cordon_node`,
`action_uncordon_node` and `action_drain_node` as one-line delegates, and
shares two of the controller's helpers — `node_target` (with `ShellController`)
and `edit_in_external_editor` (with `HelmController`) — so neither grows a
second copy.

`tests/ui/test_resource_write_controller.py` drives every flow against a real
`WriteCoordinator` over fake Textual and view surfaces, so "the workflow
cannot bypass the perimeter" is observed rather than asserted: a broken audit
sink blocks the mutation, a declined dialog constructs no operation factory,
and each approved flow's mutation count matches its `WriteCoordinator.run`
count.

### One write, one reservation

`reserve_write()` is available to a flow that needs the count held *before* the
coordinator's own `run` takes it — the worker hand-off, and any awaited gap
between approval and the mutation. The operator uninstall is the one flow that
needs it (its post-approval staleness re-fetch is such a gap). It does not
stack the two: it releases its prelude reservation at the point `run` builds
its coroutine, and because both `run`'s reservation and the release are
synchronous, no event-loop iteration — and therefore no `:ctx` switch — fits
between them. Exclusion is continuous and the in-flight count during a
mutation is exactly one.

### Two approval contracts, and where the audit lives

`confirm` covers the ordinary case: the gate audits the intent, fail-closed,
and only then awaits the operation. Nothing about the write is unknown to it,
so it can record everything before anything happens.

`confirm_interactive` (#236) exists because the shell flows — the `kubectl
debug` fallback and `kubectl debug node/` — cannot honour that contract. Their
approved form is a subprocess that suspends the app, and the facts worth
auditing only exist once it has started: the pod kubectl actually created, its
uid, and the session's exit outcome. `WriteCoordinator.run` cannot know any
of them.

So the split is explicit rather than accidental:

| | `confirm` | `confirm_interactive` |
|---|---|---|
| dialog | gate | gate |
| epoch recheck after the dialog | gate | gate |
| write reservation | gate (`WriteCoordinator.run`) | the flow's own `reserve_write()` coroutine |
| fail-closed intent audit | gate (`WriteCoordinator.run`) | **the flow** |
| outcome audit | — | the flow |

The invariant is unchanged — no approved write reaches a cluster before an
intent record has persisted — but for the interactive flows it is `debug.run`
and `_run_node_shell` that enforce it, each blocking the subprocess when the
append fails. `tests/ui/test_node_shell.py::test_node_shell_blocked_when_audit_append_fails`
pins that, and it is the reason this row reads "the flow" rather than "gate".

## The agent's session and its UI bridge

`AgentUiController` (`ui/agent_ui_controller.py`, issue #187 / Deep Task 6)
owns everything about the built-in agent that used to live on `KorvidApp`:

| Owned | Notes |
|---|---|
| runtime, model, settings, capability profile, configurator/rebuild/disconnect seams, the `:ai off` disconnect marker, the follow flag | `:ai`, `:ai off`, `:ai follow`, `:ai payload` and `:model` are all handled here; the app routes the command word and nothing else |
| the turn task | A bare app-loop task, created through the `TurnTasks` port: the interrupt key must cancel *this* turn, the queued interrupt-and-submit replacement starts from the cancelled task's done callback, and `shutdown()` cancels and reaps it |
| the screen context the model is told about | Composed from `WorkspaceState` and the selected row, plus the one-shot `:ctx` note the switch coordinator hands over through `note_context_switch` |
| follow mirroring | Successful cluster reads are mirrored through the injected serialized bridge (the composition root's `_UIBridgeProxy`), falling back to the controller's own `AgentUIBridge` |
| every `UIBridge` read | evidence open, navigate, filter, drill, logs, describe — each with the approval-dialog and describe-screen guards |
| the direct agent write | `agent_request_write`, its target manifest/uid/ownership lookups, the dry-run preview, the resize impact lines, and the write-op construction the proposal path shares |

It owns no security ordering: `agent_request_write` builds an operation
factory, asks `WriteCoordinator.permitted`, waits for a `ConfirmScreen` only a
user keystroke can resolve, and executes through `WriteCoordinator.run_shielded`.
An approval never surfaces while the panel is collapsed or another screen is
stacked, and an unanswered one expires rather than hanging the turn.

Proposals are explicitly *not* here: `AgentProposals` is a three-method port
the agent controller forwards to, and `ProposalController` implements it (see
below).

`KorvidApp` keeps `action_toggle_agent`, `on_agent_prompt_submitted` and
`action_interrupt_agent` as one-line delegates, the `AgentPanel` widget in
`compose`, the status-bar label (read from `runtime` / `blocked_in_protected()`),
and two shared helpers — `_target_uid` and `_managed_note`/`_managed_note_from` —
which the shell, transfer, proposal and resource-write flows reach through the
controller that owns them.

`AppUIBridge` is now `AgentUIBridge(app._agent_ui, app._bridge_dispatch)`: it
holds no app reference and calls no app method.

## External write proposals

`ProposalController` (`ui/proposal_controller.py`, issue #110 / Deep Task 7)
owns the whole external-proposal inbox, which used to live on `KorvidApp`:

| Owned | Notes |
|---|---|
| the `ProposalStore` reference and the submit/get/cancel intake | It *is* the `AgentProposals` implementation the agent controller forwards to; the app exposes no proposal store property and no proposal tool method |
| provenance and the terminal-outcome audit | `provenance()` shell-quotes untrusted MCP `clientInfo`, so every `key=` field in an audit detail is korvid's own; outcome appends are best-effort (they mutate nothing) and bind to the proposal's own `context` |
| the update subscription and external-change handling | `subscribe()` registers the store callbacks; both are marshalled onto the UI loop through the `ProposalEvents` port, because the store is shared with the MCP server's thread |
| the pending-proposal status label | `status_label()`; the app's `_refresh_status` reads it |
| the `:proposals` review loop | Oldest first, one decision at a time, on a supervised worker in the `proposal-review` group through the `ReviewTasks` port — never `exclusive`, because cancelling a claimed execution would strand the record |
| the approval dialog | `WriteCoordinator.confirm_screen`, resolved only by real key input, refused outright when another screen is stacked, and treated as a dismissal after `APPROVAL_TIMEOUT` (a dismissed proposal stays pending until its own TTL) |
| operation rebuild and re-validation | The stored record never carries an executable closure: `build_write_op` runs again at review time, and context epoch, kube context, RBAC and the UID binding are each rechecked before the dialog *and* after the claim |
| execution, settlement and failure | The claim is taken under the shared `nav_lock` so it linearizes with the `:ctx`/`:mcp` expiry sweeps; the mutation goes through `WriteCoordinator.run` and nothing else; a cancelled worker settles the record as `failed` with the cluster outcome explicitly uncertain, and the terminal audit append is shielded |
| the audited expiry sweep | `expire_all(reason)` for `:ctx` and the `:mcp on`/`:mcp off` transitions, `shutdown()` (close, then sweep) for unmount |

It owns no security ordering either: every accepted proposal enters
`WriteCoordinator.run` exactly once, so the reserve → fail-closed intent audit
→ mutate → outcome audit ordering has a single implementation. The proposal
dialog is deliberately separate from the agent's own write approval: this flow
is user-initiated (`:proposals`), so it has no panel gate, but it can never
stack over another dialog where a stray keystroke could approve.

Write-operation construction is *not* duplicated: `builder` is a late-binding
`WriteOpBuilder` (structurally `AgentUiController`, which owns that
construction for the direct agent write), so read-only mode, audit
availability, kind resolution and argument validation are rechecked by the one
implementation.

`KorvidApp` keeps `on_external_proposals_changed`,
`on_external_proposal_expired`, the `:proposals` command word, the
`proposals_label=` status read, the three `:ctx`/`:mcp` sweep calls and the
unmount call — all one-line delegates — plus three small adapters
(`AppProposalScreens`, `AppReviewTasks`, `AppProposalEvents`).

## Runtime context switching

`ContextSwitchCoordinator` (`ui/context_switch_coordinator.py`, issue #36 /
Deep Task 8) owns the whole `:ctx` transaction, which used to live on
`KorvidApp`:

| Owned | Notes |
|---|---|
| the switch epoch and the in-flight claim | It *is* the session's single `ContextGuard`, so the state every controller revalidates against is the state the transaction mutates — no adapter, no second copy |
| the listing / probe / swap collaborators | `list_contexts`, `probe_context`, `switch_context` arrive from the composition root; all None in a build with no cluster connection, which every entry point reports rather than half-doing |
| the `:ctx` picker and its completion prefetch | `show_picker()` builds an explicit display-label → name map (decoding the ` (current)` suffix would corrupt a context whose name ends in it); the prefetch task is owned here and cancelled *and reaped* by `shutdown()` |
| the no-op check and the pre-probe guards | A session started without `-c` falls back to the kubeconfig's active context, so `:ctx <active>` is still a no-op; an unknown name is refused before the probe |
| the blocker set | Busy agent, reserved write (`WriteCoordinator.active_writes`), any stacked dialog, and the inline namespace picker — re-checked inside `nav_lock` after the probe, because the probe awaits network I/O |
| the MCP quiesce | The embedded server drains *before* anything is torn down; a server that will not stop aborts the switch with the old context fully usable |
| the ordered teardown | logs → describe → workspace quiesce → namespace prefetch/completions → watches → forwards (+ audit flush) → store → hint workers → hint cache → timeline feed |
| the retarget and its recovery | On a mid-swap failure the old context is restored; only "even the fallback failed" returns not-ok, and the session is then told to restart |
| the atomic application | Epoch bump, identity/namespace/capabilities, protected-context marker, audit attribution, forward registry re-open, workspace view reset, context-pinned CLI wrappers, agent note — all in one synchronous slice, so nothing observes a session half on either cluster |
| the resume | Timeline `completed` (only when the requested target is what got applied) and the Warning feed, the MCP restart, the watch restart and the metrics sync — all still inside `nav_lock` |

The ordering *is* the safety property, and it is pinned by
`tests/ui/test_context_switch_coordinator.py` against typed fakes for every
port, not only through a running app:

- the probe precedes the first destructive step, so a failed probe strands
  nothing;
- the MCP quiesce and the proposal expiry precede the first fallible teardown
  await, so a raising participant cannot leave old-run proposals reviewable;
- the epoch moves exactly once, and only when a context is actually applied —
  a failed probe or a total swap failure leaves it untouched;
- a write reservation blocks the switch, and every flow that awaited across
  one sees `crossed(epoch)`.

The late-bound participant accessors (`workspace=`, `logs=`, `timeline=`, …)
exist because each of those controllers takes this coordinator as its
`ContextGuard` and so cannot be constructed first. They hand over the real
typed collaborator: no step of the transaction is routed back through
`KorvidApp`.

`KorvidApp` keeps `on_show_context_picker` and `on_switch_context_command` as
one-line delegates, `self._ctx.start()` on mount, `await self._ctx.shutdown()`
on unmount, and the two adapters (`AppContextSurface`,
`AppSessionConfiguration`). It holds no switch epoch, no in-flight flag, and no
context collaborator.

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
9. ~~Workspace orchestration~~ — done (#187); `WorkspaceController`
   (`ui/workspace_controller.py`) owns navigation/scope, filter/sort, the
   drill pre-warm/watch-release flow, the hierarchy tree and relationship-graph
   flows, the split-pane lifecycle, and the workspace-only state (`_nav_lock`,
   pre-warm leases, tree rebuild context, jump-poll budget, render-coalescing
   set, metrics target). It reaches Textual through `UiSurface`,
   `WorkspaceSurface` and `ContextGuard`; the app keeps compose/widget
   construction and thin action/message delegates, and the `:ctx` coordinator
   calls the controller's `quiesce_for_context_switch`/`reset_view_after_switch`
   reset API under the shared `nav_lock`.
10. ~~The write security perimeter~~ — done (#187); `WriteCoordinator`
    (`ui/write_coordinator.py`) owns approval, epoch/identity revalidation,
    the synchronous write reservation, the fail-closed intent audit, audited
    execution, the dry-run/impact previews, and the protected-context marker.
    It implements `WriteGate` directly, so `AppWriteGate` is gone and the app
    keeps only the action/message entry points that raise a write flow.
11. ~~Resource and node write workflows~~ — done (#187);
    `ResourceWriteController` (`ui/resource_write_controller.py`) owns delete,
    rollout restart, the editor round-trip, scale, in-place pod resize,
    cordon/uncordon and drain, together with the drain worker/target state and
    the workload-eligibility identities (`RESTARTABLE`, `SCALABLE`). It is
    backed exclusively by `WriteCoordinator`; the app keeps thin action
    delegates and re-exports the eligibility sets for `_ACTION_VIEWS` and the
    agent write ops.
12. ~~The agent session and its UI bridge~~ — done (#187);
    `AgentUiController` (`ui/agent_ui_controller.py`) owns the runtime /
    settings / profile / follow state, the turn task with its
    interrupt-and-submit lifecycle, the screen context, follow mirroring, all
    `UIBridge` reads, and the direct approval-gated agent write. `AppUIBridge`
    became an adapter over it plus `AppContextDispatch`
    (`ui/bridge_dispatch.py`).
13. ~~External write proposals~~ — done (#110/#187);
    `ProposalController` (`ui/proposal_controller.py`) owns the store, the
    submit/get/cancel intake behind `AgentProposals`, provenance and outcome
    audit, the update subscription and status label, the one-at-a-time
    `:proposals` review with its own approval dialog and timeout, the
    operation rebuild and every re-validation, the claimed execution through
    `WriteCoordinator`, the interrupted-execution settlement, and the audited
    expiry sweeps `:ctx`, `:mcp` and unmount drive. `AppProposalOps` is gone;
    the app holds no proposal state.
14. ~~Runtime context switching~~ — done (#36/#187);
    `ContextSwitchCoordinator` (`ui/context_switch_coordinator.py`) owns the
    switch epoch and in-flight claim (it *is* the session's `ContextGuard`),
    the listing/probe/swap collaborators, the picker and its completion
    prefetch, the no-op/blocker/guard refusals, the MCP quiesce, the ordered
    teardown, the retarget with its restore path, the atomic application of a
    proven switch, and the timeline/watch/metrics resume. `AppContextGuard` is
    gone; the app keeps two message delegates and two adapters
    (`AppContextSurface`, `AppSessionConfiguration`).

15. ~~The remaining integration and inspection flows~~ — done (#187);
    three owners, not one bag:
    - `ForwardController` gained `open_dialog`, so the whole shift+f journey
      (forwardable-kind check, `:ctx` read gate, missing registry/kubectl,
      Service TCP-port resolution, dialog, post-dialog epoch revalidation,
      launch) lives with the session lifecycle it starts; it took `ViewState`
      for the one selection read that journey makes.
    - `TransferController` gained the user-facing half of ctrl+t: the
      selection guards, the container pick, the transfer dialog with its
      read-only remote listing and pod-uid binding, the upload approval, and
      the progress modal. Its perimeter is the real `WriteCoordinator`
      (`confirm_screen` + `reserved`), and the one screen-stack action it
      needs is the narrow `TransferScreens.dismiss_if_current` — `UiSurface`
      deliberately cannot pop screens.
    - `ResourceInspectController` (`ui/resource_inspect_controller.py`) owns
      describe (selected and named), the shared Secret-masking rule, the
      provider footer, the container rows and shell/logs pick, the
      hint-details overlay, the pods store lookups those share, and the
      pod-identity guard the debug and transfer flows bind an approved action
      to. It reads the two mounted widgets it needs — the table row cursor
      and the hint strip — through `InspectSurface`.
    - `IntegrationController` (`ui/integration_controller.py`) owns the
      optional integrations and all four pieces of state the app used to
      hold: the MCP follow flag, and the telepresence hinted/probing/reprobe
      trio. It reaches the proposal sweeps through `IntegrationProposals`
      and the `:ctx` navigation lock through `SwitchSerializer`.
    - `CommandRouter` (`ui/command_router.py`) decides which of those owners
      an unresolved `:` command belongs to, and produces exactly one message
      of its own — the unknown-command report. `OperatorController` answers
      the `:operators` half through `explain_missing_catalog()`, because only
      the OLM owner can tell an undiscovered API group from a syntax error on
      a discovered view.

    Selection reads (`selected_ns_name`, `selected_uid`) moved onto
    `AppViewState`, which is where the interface already declared them; the
    app holds one `self._view` and no longer implements them.

Issue #238 showed that logs and describe were technically extractable without
introducing a new pane-composition seam, and issue #245 kept describe as a
deliberate low-ROI non-extraction at the time. Both are now extracted — logs
through the narrow `LogPaneView` accessor (above), describe through
`ResourceInspectController` — because the app-as-integration-hub cost had
grown past the per-flow saving. `SessionTimelineController` owns the
timeline-specific boundary, and `ContextSwitchCoordinator` owns the `:ctx`
transaction — the app keeps only the two `:ctx` message handlers as one-line
delegates.
