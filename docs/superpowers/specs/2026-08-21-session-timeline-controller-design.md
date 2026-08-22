# Session Timeline Controller Extraction

## Goal

Remove session-timeline production and interaction policy from `KorvidApp`
without changing timeline contents, context-epoch safety, Textual worker
supervision, or write-audit ordering.

This is the first independently deliverable part of the broader UI
decomposition. Write previews, resource-write flows, and logs remain separate
follow-up projects.

## Evidence

The session timeline was added after the previous UI-controller decomposition
stopped. Its app-owned implementation currently spans:

- watch-delta recording;
- Warning Event watch and bounded reconnect;
- append-failure reporting;
- context-switch and durable-write records;
- timeline modal opening and resource navigation;
- two dedicated Textual worker groups.

These methods depend on a small, coherent set of capabilities: the timeline
store, Warning Event stream, current epoch, discovered aliases, selected
resource, navigation callback, watch manager, and UI surface. They do not own
pane composition, context retargeting, mutation, or audit persistence.

## Chosen approach

Add `SessionTimelineController` in
`src/korvid/ui/session_timeline_controller.py`. The controller owns timeline
policy and delegates every Textual operation through `UiSurface`.

`KorvidApp` constructs the controller with:

- the optional `SessionTimeline`;
- the optional Warning Event stream factory;
- `WatchManager`;
- `ViewState`;
- `UiSurface`;
- `ContextSwitchCoordinator.epoch` and `.crossed` as the live epoch boundary;
- `WorkspaceController.selected_timeline_resource` for silent selected-row
  capture;
- `WorkspaceController.jump_to_object` for guarded object navigation.

The controller exists even when no timeline is configured. In that state it is
inert: it does not install a watch sink or start a worker, and `open()` emits the
existing unavailable notification.

## Ownership

### `SessionTimelineController`

The controller owns:

- wiring `WatchManager.on_event`;
- mapping watch protocol verbs into timeline entries;
- resolving Event involved-object aliases;
- all append exception/refusal reporting;
- Warning Event reconnect and permanent-denial policy;
- context-switch and audited-write record creation;
- timeline modal construction and dismissal handling;
- starting and stopping its named worker groups.

### `KorvidApp`

The app owns only:

- creation of the controller;
- `on_worker_state_changed`, which translates failed named workers into
  notifications.

`ContextSwitchCoordinator` owns the current epoch and the complete
quiesce/retarget/resume transaction. It records each switch phase through the
timeline port. `WorkspaceController` owns selected-row capture and object
navigation. `WriteCoordinator` owns durable audit append and the fail-closed
write perimeter; only after an audit append succeeds does it call
`SessionTimelineController.record_write()`. A timeline failure remains
non-fatal and cannot turn a failed audit into a success-shaped timeline record.

### `UiSurface`

`UiSurface` gains only the worker and notification capabilities this controller
already uses directly on `KorvidApp`:

- `notify(..., markup: bool = True)`;
- `run_worker(..., exit_on_error: bool = True)`;
- `cancel_workers(group: str)`.

`AppUiSurface` implements cancellation by cancelling and awaiting the app-owned
Textual worker group. Controllers still cannot access the worker manager or
screen stack directly.

## Data flow

### Startup

1. `KorvidApp.on_mount()` delegates timeline startup to the controller.
2. When enabled, the controller installs `record_watch_event` as the watch
   manager's post-store event sink.
3. The controller starts the Warning Event feed in its dedicated worker group.

### Context switch

1. `ContextSwitchCoordinator` records the `started` phase through its timeline
   port.
2. During teardown, the coordinator calls `await timeline.stop()`.
3. The coordinator retargets all cluster dependencies and increments its epoch
   exactly once when a context is applied.
4. The coordinator records `completed` only when the requested context was
   applied; failures remain on the epoch that owns the failed attempt.
5. The coordinator calls `timeline.start_warning_watch()` for whichever
   context remains active.

The controller's reconnect loop captures one epoch. It stops when that epoch no
longer matches, and cancellation propagates unchanged.

### Audited write

1. `WriteCoordinator` writes the audit record inside the single write
   perimeter.
2. Only after the durable append returns, it calls `timeline.record_write()`.
3. The controller appends the timeline entry and reports any refusal or
   internal timeline exception.

The timeline remains a non-authoritative view of the authoritative audit log.

### Modal navigation

1. `KorvidApp.action_timeline()` delegates to `timeline.open()`.
2. The controller captures the `ContextSwitchCoordinator` epoch and the
   `WorkspaceController` selected resource exactly once.
3. It opens `SessionTimelineScreen`.
4. On a goto result, it rejects a crossed epoch or starts the existing
   `WorkspaceController.jump_to_object` callback in the timeline navigation
   worker group.

## Error handling

- Timeline append exceptions are logged and surfaced with `markup=False`.
- Refused appends retain their existing diagnostic notification.
- `asyncio.CancelledError` from the Warning Event feed is never swallowed.
- HTTP 401, 403, and 405 stop the Warning Event feed immediately and visibly.
- Other feed failures use the existing bounded exponential backoff and stop
  after five consecutive failures.
- Worker failures remain `exit_on_error=False` and are reported by the app's
  existing worker-state handler.
- Cluster-controlled error text and resource names are never interpreted as
  Rich markup.

## Testing

Add direct controller tests for:

- disabled startup;
- watch-delta alias/epoch recording;
- append refusal and exception reporting;
- permanent Warning Event denial;
- bounded reconnect failure;
- context-switch and write records;
- unavailable/open modal behavior;
- stale modal navigation;
- worker-group cancellation.

Retain the existing app-level tests for startup wiring, context switches,
durable-write ordering, keybinding behavior, and navigation. These tests prove
that the adapter wiring preserves the security and lifecycle boundaries.

Run targeted pytest, Ruff, mypy, and Tach checks for the changed modules, then
run the full repository gate before the final commit.

## Out of scope

This design covered only the timeline extraction. The broader PR subsequently
extracted write previews and resource writes, logs, context switching, agent/MCP
proposal handling, and workspace navigation into their final controllers. Their
contracts are documented in `docs/dev/ui-controllers.md`; they are referenced
here only where they now provide a timeline dependency.
