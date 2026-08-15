# Input-Latency Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the demonstrated no-op cursor repaint and make the 1,000-Pod benchmark measure key injection to cursor-row acknowledgement.

**Architecture:** Keep the production optimization inside `ResourceTable`'s existing in-place update path: restore the cursor only when the selected row key actually changed. Put the test-only input probe in the shared replay harness and call it from both deterministic and live runs so measurement semantics stay identical.

**Tech Stack:** Python 3.11+, Textual 8, pytest/pytest-asyncio, Ruff, mypy, existing large-cluster replay harness.

## Global Constraints

- Preserve cursor identity, viewport, sorting, filtering, and row-removal behavior.
- Do not introduce a frame-rate cap or change store/watch coalescing.
- Input latency means key injection to observed cursor-row change.
- Use a bounded monotonic timeout and fail explicitly if no test driver exists.
- Keep live cluster identity, ownership, UID, and guarded-mutation gates unchanged.
- Acceptance uses the unprofiled replay; `cProfile` remains diagnostic because it materially changes compositor cost.
- Do not publish an unverified live result, and do not infer one from a deterministic replay.
- The acceptance workload is a committed profile, and every documented command uses repository-relative profile paths and a configurable `<artifact-dir>` for outputs.
- The reported input percentile rests on a configurable number of `down`/`up` sample pairs, never on a single pair.

## File Structure

- Modify `src/korvid/ui/widgets/resource_table.py`: avoid redundant cursor restoration in the in-place path.
- Modify `tests/ui/test_table_diff_update.py`: pin the no-op cursor-move regression while retaining existing deletion/viewport coverage.
- Modify `tests/performance/replay.py`: define the shared direct cursor-acknowledgement probe and use it in deterministic replay.
- Modify `tests/performance/live.py`: use the same probe in guarded live replay.
- Modify `tests/performance/test_replay.py`: test successful acknowledgement and bounded timeout.
- Verify `tests/performance/test_live.py`: preserve the injected-clock contract.
- Add `tests/performance/profiles/steady-24eps-1k.json`: the committed acceptance workload the published result is measured against.
- Modify `tests/performance/cli.py`: expose and validate the cursor-probe knobs on both `replay` and `replay-live`.

---

### Task 1: Eliminate no-op cursor restoration

**Files:**
- Modify: `src/korvid/ui/widgets/resource_table.py:431-447`
- Test: `tests/ui/test_table_diff_update.py:163`
- Verify: `tests/ui/test_cursor_stability.py`

**Interfaces:**
- Consumes: `ResourceTable._cursor_snapshot() -> tuple[str, int] | None`
- Produces: unchanged `ResourceTable.show(...) -> None` behavior with no `move_cursor()` call when the selected row key survives in place.

- [ ] **Step 1: Write the failing no-op cursor test**

Add this test before `test_no_deferred_scroll_scheduled_when_cursor_unchanged`:

```python
async def test_in_place_update_does_not_move_unchanged_cursor() -> None:
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        table.focus()
        await pilot.press("down")
        await until(pilot, lambda: table.cursor_row == 1, label="cursor on beta")
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        original = table.move_cursor

        def spy(*args: Any, **kwargs: Any) -> None:
            calls.append((args, kwargs))
            original(*args, **kwargs)

        table.move_cursor = spy  # type: ignore[method-assign]  # test spy
        app.store.apply_event("pods", "default", "MODIFIED", _pod("beta", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/beta")[2]) == "Pending",
            label="phase cell updated",
        )

        assert calls == []
        assert table.cursor_row == 1
```

- [ ] **Step 2: Run the test to prove the regression**

Run:

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_table_diff_update.py::test_in_place_update_does_not_move_unchanged_cursor -q
```

Expected: FAIL because `ResourceTable.show()` calls `_restore_cursor()` and therefore `move_cursor()` after every eligible in-place update.

- [ ] **Step 3: Implement the selected-key guard**

Replace the in-place success block in `ResourceTable.show()` with:

```python
        if same_view and sort == self._last_sort and self._apply_in_place(pending):
            # In-place cell updates and appends leave the selected key alone.
            # Avoid reassigning the same cursor coordinate: DataTable treats
            # move_cursor() as repaint work even when nothing selected moved.
            current = self._cursor_snapshot()
            if restore is not None and (current is None or current[0] != restore[0]):
                offset = (self.scroll_x, self.scroll_y)
                self._restore_cursor(*restore, scroll=False)
                if self.cursor_row != restore[1]:
                    self.call_after_refresh(
                        self.scroll_to, *offset, animate=False, immediate=True, force=True
                    )
            return
```

- [ ] **Step 4: Run focused cursor and diff tests**

Run:

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_table_diff_update.py \
  tests/ui/test_cursor_stability.py -q
```

Expected: all tests PASS, including deletion above the cursor, deletion of the selected row, sort reorder, and viewport restoration.

- [ ] **Step 5: Lint and format Task 1 files**

Run:

```bash
.venv/bin/ruff check --fix \
  src/korvid/ui/widgets/resource_table.py \
  tests/ui/test_table_diff_update.py
.venv/bin/ruff format \
  src/korvid/ui/widgets/resource_table.py \
  tests/ui/test_table_diff_update.py
```

Expected: both commands exit 0.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/korvid/ui/widgets/resource_table.py tests/ui/test_table_diff_update.py
git commit -m "perf: skip unchanged cursor restoration" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 2: Measure direct cursor acknowledgement

**Files:**
- Modify: `tests/performance/replay.py:10-57, 483-511, 597-602`
- Modify: `tests/performance/live.py:97-105, 1382-1387`
- Test: `tests/performance/test_replay.py:22-29`
- Verify: `tests/performance/test_live.py:2131-2149`

**Interfaces:**
- Produces: `measure_cursor_input(pilot: Any, table: ResourceTable, key: str, *, now: Callable[[], float] = monotonic, timeout: float = 5.0) -> Awaitable[float]`
- Consumes: active Textual test driver's `send_message`, `ResourceTable.cursor_row`, and the existing injected live clock.

- [ ] **Step 1: Add failing success and timeout tests**

Add `measure_cursor_input` to the import from `tests.performance.replay`, add:

```python
from korvid.ui.widgets.resource_table import ResourceTable
from tests.ui.test_app import _pod, make_app
from tests.ui.waits import WaitTimeout, until
```

Then add:

```python
async def test_measure_cursor_input_returns_when_cursor_row_changes() -> None:
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        table.focus()

        elapsed = await measure_cursor_input(pilot, table, "down")

        assert elapsed >= 0.0
        assert table.cursor_row == 1


async def test_measure_cursor_input_times_out_when_cursor_cannot_move() -> None:
    app = make_app([_pod("only")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        table.focus()

        with pytest.raises(WaitTimeout, match="down.*row 0.*0.01s"):
            await measure_cursor_input(pilot, table, "down", timeout=0.01)
```

- [ ] **Step 2: Run the new tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/performance/test_replay.py::test_measure_cursor_input_returns_when_cursor_row_changes \
  tests/performance/test_replay.py::test_measure_cursor_input_times_out_when_cursor_cannot_move -q
```

Expected: collection FAILS because `measure_cursor_input` does not exist.

- [ ] **Step 3: Implement the shared probe**

In `tests/performance/replay.py`, import Textual events:

```python
from textual import __version__ as _textual_version
from textual import events
```

Add before `wait_for`:

```python
async def measure_cursor_input(
    pilot: Any,
    table: ResourceTable,
    key: str,
    *,
    now: Callable[[], float] = monotonic,
    timeout: float = 5.0,
) -> float:
    """Measure key injection until the table acknowledges a cursor-row change."""
    start_row = table.cursor_row
    app = pilot.app
    driver = app._driver
    if driver is None:
        raise RuntimeError("cursor input measurement requires an active Textual test driver")
    event = events.Key(key, None)
    event.set_sender(app)
    started = now()
    driver.send_message(event)
    try:
        async with asyncio.timeout(timeout):
            while table.cursor_row == start_row:
                await asyncio.sleep(0)
    except TimeoutError as exc:
        raise WaitTimeout(
            f"{key} cursor input from row {start_row} was not acknowledged within {timeout}s"
        ) from exc
    return now() - started
```

- [ ] **Step 4: Replace deterministic replay's Pilot measurements**

Replace the two `pilot.press()` timing blocks in `run_replay()` with:

```python
            recorder.record_input(await measure_cursor_input(pilot, table, "down"))
            recorder.record_input(await measure_cursor_input(pilot, table, "up"))
```

- [ ] **Step 5: Replace live replay's Pilot measurements**

Import `measure_cursor_input` from `tests.performance.replay`, then replace the two timing blocks with:

```python
            recorder.record_input(
                await measure_cursor_input(pilot, table, "down", now=now)
            )
            recorder.record_input(
                await measure_cursor_input(pilot, table, "up", now=now)
            )
```

- [ ] **Step 6: Run probe and harness tests**

Run:

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/performance/test_replay.py \
  tests/performance/test_live.py::test_run_live_replay_measures_input_latency_with_the_injected_clock -q
```

Expected: all tests PASS; the live injected-clock test still reports two samples with a maximum of `0.0`.

- [ ] **Step 7: Lint, format, and type-check Task 2 files**

Run:

```bash
.venv/bin/ruff check --fix \
  tests/performance/replay.py \
  tests/performance/live.py \
  tests/performance/test_replay.py
.venv/bin/ruff format \
  tests/performance/replay.py \
  tests/performance/live.py \
  tests/performance/test_replay.py
.venv/bin/mypy \
  tests/performance/replay.py \
  tests/performance/live.py
```

Expected: all commands exit 0.

- [ ] **Step 8: Commit Task 2**

```bash
git add tests/performance/replay.py tests/performance/live.py tests/performance/test_replay.py
git commit -m "fix: measure direct cursor acknowledgement" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 3: Requalify the corrected measurement

**Files:**
- Verify only: no repository file changes required.
- Artifacts: `<artifact-dir>/`

**Interfaces:**
- Consumes: the committed production and harness changes from Tasks 1 and 2.
- Produces: reproducible before/after JSON, Markdown, and `pstats` evidence from the committed acceptance profile.

- [ ] **Step 1: Run the unprofiled acceptance replay**

Run:

```bash
.venv/bin/python -m tests.performance.cli replay \
  --profile tests/performance/profiles/steady-24eps-1k.json \
  --json <artifact-dir>/optimized-24eps.json \
  --out <artifact-dir>/optimized-24eps.md
```

Expected: digest match is `true`, dropped updates are `0`, and cursor-input p95 is below `0.100s`.

- [ ] **Step 2: Run the apples-to-apples diagnostic profile**

Run:

```bash
.venv/bin/python -m tests.performance.cli replay \
  --profile tests/performance/profiles/steady-24eps-1k.json \
  --json <artifact-dir>/optimized-profiled-24eps.json \
  --out <artifact-dir>/optimized-profiled-24eps.md \
  --cpu-profile <artifact-dir>/optimized-24eps.pstats \
  --allocation-snapshot <artifact-dir>/optimized-24eps-alloc.txt
```

Expected: digest match is `true`, dropped updates are `0`, and the corrected profiled cursor metric no longer contains the two one-second Pilot waits.

- [ ] **Step 3: Run changed-surface quality gates**

Run:

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_table_diff_update.py \
  tests/ui/test_cursor_stability.py \
  tests/performance/test_replay.py \
  tests/performance/test_live.py::test_run_live_replay_measures_input_latency_with_the_injected_clock -q
.venv/bin/ruff check \
  src/korvid/ui/widgets/resource_table.py \
  tests/ui/test_table_diff_update.py \
  tests/performance/replay.py \
  tests/performance/live.py \
  tests/performance/test_replay.py
.venv/bin/ruff format --check \
  src/korvid/ui/widgets/resource_table.py \
  tests/ui/test_table_diff_update.py \
  tests/performance/replay.py \
  tests/performance/live.py \
  tests/performance/test_replay.py
```

Expected: all commands exit 0.

- [ ] **Step 4: Review final repository state**

Run:

```bash
git status --short --branch
git --no-pager log -5 --oneline --decorate
```

Expected: no uncommitted source changes and separate commits for the design, production optimization, and measurement correction.

### Task 4: Suppress off-screen cell repaints

**Files:**
- Modify: `src/korvid/ui/widgets/resource_table.py:527-621`
- Test: `tests/ui/test_table_diff_update.py`
- Verify: `tests/ui/test_cursor_stability.py`

**Interfaces:**
- Produces: `_VisibleRows(fixed_count: int, first: int, last: int)` with
  `contains(row_index: int) -> bool`.
- Preserves: immediate table-model updates, cache-generation invalidation,
  width growth, row add/remove/reorder, cursor, and viewport behavior.

**Implemented design correction**

The shipped solution does **not** use `int(scroll_y)`, per-row
`_get_row_region()`, `_absorb_widths(...) -> bool`, or unconditional private
cell writes.

- `_apply_in_place()` computes one `_VisibleRows` batch bound after removals
  and before appends. Visibility checks are then O(1) via
  `visible.contains(self.get_row_index(key))`.
- `_visible_rows()` derives inclusive painted-row bounds from fixed rows,
  header height, viewport height, and `scroll_offset.y` (matching Textual's
  rounded scroll offset) rather than truncating `scroll_y`.
- `_patch_row()` uses `_BATCH_CELL_WRITES` to gate the private
  `self._data[row_key][column.key] = new_cell` fast path to verified Textual
  majors. When the installed major is not verified, it falls back to
  `update_cell(..., update_width=False)` for correctness.
- `_absorb_widths(rows) -> None` only folds width growth into the columns and
  sets `_require_update_dimensions` when needed; callers must not expect a
  boolean "column grew" signal from it.
- `_apply_in_place()` issues one explicit `refresh()` only when a touched
  surviving row is currently visible. Width growth relies on Textual's
  dimension/update path to schedule the repaint it already needs.

**Verification**

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_table_diff_update.py \
  tests/ui/test_cursor_stability.py \
  tests/ui/test_table_column_widths.py -q
.venv/bin/ruff check \
  src/korvid/ui/widgets/resource_table.py \
  tests/ui/test_table_diff_update.py
.venv/bin/ruff format --check \
  src/korvid/ui/widgets/resource_table.py \
  tests/ui/test_table_diff_update.py
.venv/bin/mypy src/korvid/ui/widgets/resource_table.py
```

### Task 5: Repeat final requalification

**Files:**
- Verify only: no repository file changes required.
- Artifacts: `<artifact-dir>/`

- [ ] **Step 1: Run the final unprofiled acceptance replay**

```bash
.venv/bin/python -m tests.performance.cli replay \
  --profile tests/performance/profiles/steady-24eps-1k.json \
  --json <artifact-dir>/final-24eps.json \
  --out <artifact-dir>/final-24eps.md
```

Expected: cursor-input p95 is below `0.100s`, digest match is `true`, and
dropped updates are `0`.

- [ ] **Step 2: Run the final apples-to-apples diagnostic profile**

```bash
.venv/bin/python -m tests.performance.cli replay \
  --profile tests/performance/profiles/steady-24eps-1k.json \
  --json <artifact-dir>/final-profiled-24eps.json \
  --out <artifact-dir>/final-profiled-24eps.md \
  --cpu-profile <artifact-dir>/final-24eps.pstats \
  --allocation-snapshot <artifact-dir>/final-24eps-alloc.txt
```

Expected: digest match is `true`, dropped updates are `0`, and the corrected
cursor metric contains no Pilot idle waits.

- [ ] **Step 3: Run all changed-surface gates**

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_table_diff_update.py \
  tests/ui/test_cursor_stability.py \
  tests/ui/test_table_column_widths.py \
  tests/performance/test_replay.py \
  tests/performance/test_live.py::test_run_live_replay_measures_input_latency_with_the_injected_clock \
  tests/performance/test_live.py::test_run_live_replay_aborts_and_still_closes_clients_on_guard_failure \
  tests/performance/test_live.py::test_run_live_replay_propagates_cancelled_error_and_still_closes_clients -q
.venv/bin/ruff check \
  src/korvid/ui/widgets/resource_table.py \
  tests/ui/test_table_diff_update.py \
  tests/performance/replay.py \
  tests/performance/live.py \
  tests/performance/test_replay.py
.venv/bin/ruff format --check \
  src/korvid/ui/widgets/resource_table.py \
  tests/ui/test_table_diff_update.py \
  tests/performance/replay.py \
  tests/performance/live.py \
  tests/performance/test_replay.py
.venv/bin/mypy src/korvid/ui/widgets/resource_table.py \
  tests/performance/replay.py \
  tests/performance/live.py
```

Expected: every command exits `0`.

### Task 6: Make the acceptance measurement reproducible and statistically usable

**Files:**
- Add: `tests/performance/profiles/steady-24eps-1k.json`
- Modify: `tests/performance/replay.py`, `tests/performance/live.py`, `tests/performance/cli.py`
- Test: `tests/performance/test_profile.py`, `tests/performance/test_cli.py`, `tests/performance/test_replay.py`, `tests/performance/test_live.py`
- Docs: `docs/performance.md`, `README.md`, and this plan plus its design spec

**Interfaces:**
- Produces: `ReplayOptions.input_sample_pairs` (positive int, default 25) and
  `validate_input_sample_pairs(options)`, plus
  `validate_input_ack_timeout(options)` (finite and > 0 — `asyncio.timeout(inf)`
  never fires) and
  `sample_cursor_input(pilot, table, recorder, *, pairs, now, timeout, aborted)`
  shared by both harnesses. Both validators run at the top of `run_replay` and
  `run_live_replay`, before app startup or any live cluster work.
- Produces: `--input-ack-timeout FLOAT` and `--input-sample-pairs INT` on both
  `replay` and `replay-live`, validated as finite/positive and positive
  respectively, and forwarded into both `ReplayOptions` constructions.
- Preserves: cursor identity (each pair is a self-cancelling `down`/`up` round
  trip), the abort predicate before every sample, live identity/ownership
  gates, and artifact validation.

- [ ] **Step 1: Commit the acceptance profile**

The published input-latency result must be reproducible from the repository, so
the workload it is measured against is versioned rather than session-local:
schema 1, id `steady-24eps-1k`, seed 186, 1,000 objects, 20 namespaces, 24
events/s, 30 seconds, no bursts and no failure injections.

- [ ] **Step 2: Replace the n=2 input sample**

Two samples cannot support a percentile. The probe takes
`options.input_sample_pairs` `down`/`up` pairs, re-asking the abort predicate
before each individual sample, and every pair returns the cursor to its
original row so the row, freshness, and digest checks that follow are
unaffected by the count.

- [ ] **Step 3: Verify**

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/performance/test_profile.py \
  tests/performance/test_cli.py \
  tests/performance/test_replay.py \
  tests/performance/test_live.py \
  tests/performance/test_metrics.py -q
.venv/bin/ruff check tests/performance/ src/korvid/
.venv/bin/ruff format --check tests/performance/ src/korvid/
.venv/bin/mypy src/korvid tests/performance
```

Expected: every command exits `0`.
