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
- a live epoch getter;
- a callback that captures the currently selected resource without warning;
- a callback that reuses the app's existing `_jump_to_object` navigation path.

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

The app continues to own:

- creation of the controller;
- the current context epoch;
- silent selected-row capture;
- `_jump_to_object`;
- global context-switch ordering;
- `on_worker_state_changed`, which translates failed named workers into
  notifications;
- durable audit append and the fail-closed write perimeter.

After a durable audit append succeeds, the app calls
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

1. `KorvidApp.on_mount()` calls `timeline.start()`.
2. When enabled, the controller installs `record_watch_event` as the watch
   manager's post-store event sink.
3. The controller starts the Warning Event feed in its dedicated worker group.

### Context switch

1. The app records the `started` phase through the controller.
2. During teardown, the app calls `await timeline.stop()`.
3. The app retargets all cluster dependencies and increments the epoch.
4. The app records `completed` only when the requested context was applied.
5. The app calls `timeline.start_warning_watch()` for whichever context remains
   active.

The controller's reconnect loop captures one epoch. It stops when that epoch no
longer matches, and cancellation propagates unchanged.

### Audited write

1. The app writes the audit record.
2. Only after the durable append returns, it calls `timeline.record_write()`.
3. The controller appends the timeline entry and reports any refusal or
   internal timeline exception.

The timeline remains a non-authoritative view of the authoritative audit log.

### Modal navigation

1. `KorvidApp.action_timeline()` delegates to `timeline.open()`.
2. The controller captures the epoch and selected resource exactly once.
3. It opens `SessionTimelineScreen`.
4. On a goto result, it rejects a crossed epoch or starts the existing
   `_jump_to_object` callback in the timeline navigation worker group.

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

- write-preview extraction;
- resource-write controller extraction;
- log controller extraction;
- context-switch coordinator extraction;
- agent/MCP proposal refactoring;
- pane, hierarchy, or navigation redesign;
- a line-count target for `app.py`.
