# Input-latency performance correction

## Goal

Make the 1,000-Pod, 24-events/second workload remain routinely interactive
while reporting cursor latency that represents a user-visible acknowledgement
rather than test-driver quiescence.

The focused success criteria are:

1. cursor-input p95 measures key injection to cursor-row change;
2. the corrected deterministic measurement is below 100 ms without diagnostic
   CPU profiling;
3. metadata-only watch churn does not repaint an unchanged table selection;
4. cursor identity, scroll position, sorting, filtering, and row-removal
   behavior remain unchanged;
5. dropped updates and digest mismatches remain zero.

## Evidence and root cause

The existing 1,000-Pod, 24-events/second replay reproduced the reported shape:
event-to-render p95 was 156 ms, cursor-input p95 was 2.206 seconds, and sampled
CPU peaked at 99.2%.

The CPU profile attributes 24.05 of 31.20 sampled seconds to Textual compositor
refreshes. `ResourceTable.show()` restores the cursor after every in-place
update, even when the selected row key and index did not move. `move_cursor()`
invalidates the table and contributes to repeated rendering of the visible
viewport.

The 2-second input result is not a direct cursor acknowledgement. Textual
`Pilot.press()` performs two CPU-idle waits, each bounded at one second. Both
wait to their ceiling while the compositor is saturated. Injecting the same key
and waiting only for the cursor row to change reduced the profiled measurement
to about 205 ms without changing production code.

The first corrected 30-second replay reduced cursor-input p95 to 159 ms but did
not meet the 100 ms target. A focused five-second trace found 349 off-screen
cell changes and only 6 visible cell changes, while `ResourceTable.refresh()`
ran 379 times. Textual's `DataTable.update_cell()` refreshes the entire widget
for every changed cell, including rows outside the viewport. This unnecessary
off-screen repaint pressure is the remaining demonstrated production
bottleneck.

## Considered approaches

### Measurement correction only

This removes roughly two seconds of test-driver delay and makes reports honest,
but leaves avoidable production repaint work in place.

### Render optimization only

This improves production responsiveness, but the benchmark continues to report
the driver's quiescence heuristic rather than input acknowledgement.

### Combined focused correction

Correct the input probe and remove the demonstrated no-op cursor move. This is
the chosen approach because each change addresses one confirmed root cause,
does not introduce a new scheduler or frame-rate policy, and keeps the existing
event-coalescing behavior.

## Design

### Cursor-preserving in-place updates

`ResourceTable` will compare the selected row key after `_apply_in_place()` with
the pre-update cursor snapshot.

- If the same row key remains selected, it will not call `_restore_cursor()`.
- If removals changed the selected key, it will use the existing restoration
  path to follow the original resource or clamp the deleted selection.
- If the cursor row changed, it will retain the existing deferred viewport
  restoration that counters Textual's scheduled scroll.
- Rebuild, reorder, view-switch, and sort-change paths remain unchanged.

This keeps the optimization local to the in-place path and avoids a general
render throttle, which could trade input latency for event-to-render latency.

### Viewport-aware cell updates

The in-place diff will update `DataTable`'s cell model and cache generation
without immediately refreshing the widget for every cell. One repaint is
requested after the batch only when at least one changed row intersects the
current viewport.

An off-screen value that grows a column remains a repaint condition because the
new width changes the visible table layout even when the value itself is not
visible. Row additions, removals, reorder fallbacks, and dimension updates keep
their existing paths.

This is not virtualization and does not defer data correctness: `get_row()`,
sorting, filtering, a later scroll, and final digest checks see the new value
immediately. It only suppresses a screen refresh that cannot display the
changed row.

### Direct input acknowledgement

The performance harness will expose one helper that:

1. snapshots the `ResourceTable` cursor row;
2. injects one Textual key event through the active test driver;
3. yields to the event loop until the expected cursor row is observed;
4. fails with a bounded, named timeout if the cursor does not move;
5. returns the monotonic elapsed time.

Replay and live qualification will both use this helper. It will not call
`Pilot.press()`, `Pilot.pause()`, or Textual's CPU-idle heuristic while the
sample is active. The probe remains test-only; production input handling is
unchanged.

The down/up pair remains the input sample so the selected resource returns to
its original position. The initial table has 1,000 rows, so both movements are
well-defined.

### Metrics and diagnostic overhead

The report field remains `input_latency`, but its documentation will define it
as key injection to cursor-row acknowledgement. CPU profiles remain diagnostic
artifacts rather than the acceptance environment for the 100 ms input budget:
`cProfile` instruments every Python call and materially changes compositor
cost. Before/after diagnostic profiles must use the same instrumentation.

### K9s comparison

The exact comparison requires the dedicated `aks-korvid-contract-test` cluster,
which is not currently present in the active Azure subscription and whose
kubeconfig endpoint no longer resolves. The installed comparator is K9s
0.50.18.

When the cluster is restored, run at least three alternating Korvid/K9s trials
on the same host and terminal dimensions against the versioned
`aks-live-1k.json` schedule. Use isolated K9s state directories, `--readonly`,
all namespaces, the Pods view, and a fixed two-second refresh. Run the existing
guarded mutation driver externally so neither client owns workload generation.

Stock K9s cannot expose informer-receipt-to-draw timestamps, rendered-update
digests, or backlog depth. A valid direct comparison therefore covers common
observable metrics only: startup, cursor acknowledgement, process CPU, RSS,
achieved churn, and final cluster state. Event-to-render comparison requires an
ephemeral instrumented K9s build and must not be inferred from PTY output.

## Error handling

- Key injection fails explicitly if no active driver exists.
- Cursor acknowledgement uses a monotonic deadline and names the key and
  expected row in its timeout.
- The live workload keeps all existing identity, ownership, UID, and guarded
  mutation checks.
- An unavailable K9s target is reported as a blocked comparison, never as a
  passing or estimated result.

## Testing and verification

1. Add a `ResourceTable` regression proving a no-op in-place update does not
   move the cursor or request cursor-driven refresh work.
2. Add regressions proving an off-screen cell update changes table data without
   repaint, a visible update repaints once, and off-screen width growth still
   repaints.
3. Preserve tests for deletion above the cursor, deletion of the selected row,
   sorting, and viewport restoration.
4. Add input-probe tests for down/up acknowledgement and timeout behavior.
5. Run the targeted resource-table and performance replay/live tests.
6. Run Ruff and mypy on changed files; run Tach only if imports cross package
   boundaries.
7. Rerun the 1,000-Pod, 24-events/second replay without `cProfile` for the input
   acceptance result and with `cProfile` for an apples-to-apples CPU profile.
8. Confirm zero dropped updates, a matching final digest, and no event-latency
   regression.

## Non-goals

- Replacing Textual's compositor or `DataTable`.
- Introducing a global render frame-rate cap.
- Claiming an exact K9s event-to-render result from stock K9s.
- Provisioning or mutating an Azure cluster that fails the existing live-run
  identity and ownership gates.
