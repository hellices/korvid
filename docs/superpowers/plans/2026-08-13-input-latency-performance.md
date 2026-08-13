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
- Do not claim a stock-K9s event-to-render metric.

## File Structure

- Modify `src/korvid/ui/widgets/resource_table.py`: avoid redundant cursor restoration in the in-place path.
- Modify `tests/ui/test_table_diff_update.py`: pin the no-op cursor-move regression while retaining existing deletion/viewport coverage.
- Modify `tests/performance/replay.py`: define the shared direct cursor-acknowledgement probe and use it in deterministic replay.
- Modify `tests/performance/live.py`: use the same probe in guarded live replay.
- Modify `tests/performance/test_replay.py`: test successful acknowledgement and bounded timeout.
- Verify `tests/performance/test_live.py`: preserve the injected-clock contract.

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

### Task 3: Requalify and record comparison limits

**Files:**
- Verify only: no repository file changes required.
- Artifacts: `/Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/`

**Interfaces:**
- Consumes: the committed production and harness changes from Tasks 1 and 2.
- Produces: reproducible before/after JSON, Markdown, and `pstats` evidence plus an explicit K9s execution/blocker record.

- [ ] **Step 1: Run the unprofiled acceptance replay**

Run:

```bash
.venv/bin/python -m tests.performance.cli replay \
  --profile /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/steady-24eps-1k.json \
  --json /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/optimized-24eps.json \
  --out /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/optimized-24eps.md
```

Expected: digest match is `true`, dropped updates are `0`, and cursor-input p95 is below `0.100s`.

- [ ] **Step 2: Run the apples-to-apples diagnostic profile**

Run:

```bash
.venv/bin/python -m tests.performance.cli replay \
  --profile /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/steady-24eps-1k.json \
  --json /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/optimized-profiled-24eps.json \
  --out /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/optimized-profiled-24eps.md \
  --cpu-profile /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/optimized-24eps.pstats \
  --allocation-snapshot /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/optimized-24eps-alloc.txt
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

- [ ] **Step 4: Verify the K9s comparator and live-target blocker**

Run:

```bash
/opt/homebrew/bin/k9s version --short
kubectl config get-contexts aks-korvid-contract-test
az aks show \
  --resource-group rg-korvid-contract-test \
  --name aks-korvid-contract-test \
  --query '{id:id,state:powerState.code}' -o json
```

Expected in the current environment: K9s reports `0.50.18`; the kubeconfig context exists but its endpoint does not resolve; Azure reports that the dedicated cluster/resource group is absent. Record the direct K9s comparison as blocked, not passed or estimated.

- [ ] **Step 5: Review final repository state**

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
- Produces: `ResourceTable._row_is_visible(key: str) -> bool`
- Changes: `ResourceTable._absorb_widths(...) -> bool`, returning whether a column grew.
- Preserves: immediate table-model updates, cache-generation invalidation, width growth, row add/remove/reorder, cursor, and viewport behavior.

- [ ] **Step 1: Add failing viewport-aware refresh tests**

Add a refresh spy:

```python
def _spy_refresh(table: ResourceTable) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    original = table.refresh

    def spy(*regions: Any, **kwargs: Any) -> Any:
        calls.append((regions, kwargs))
        return original(*regions, **kwargs)

    table.refresh = spy  # type: ignore[method-assign]  # test spy
    return calls
```

Add three tests:

```python
async def test_offscreen_cell_update_changes_data_without_repaint() -> None:
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        calls = _spy_refresh(table)

        app.store.apply_event(
            "pods", "default", "MODIFIED", _pod("pod-39", phase="Pending")
        )
        await until(
            pilot,
            lambda: str(table.get_row("default/pod-39")[2]) == "Pending",
            label="offscreen cell updated",
        )

        assert calls == []


async def test_visible_cell_update_repaints_once() -> None:
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        calls = _spy_refresh(table)

        app.store.apply_event(
            "pods", "default", "MODIFIED", _pod("pod-00", phase="Pending")
        )
        await until(
            pilot,
            lambda: str(table.get_row("default/pod-00")[2]) == "Pending",
            label="visible cell updated",
        )

        assert len(calls) == 1


async def test_offscreen_width_growth_still_repaints() -> None:
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        status = table.ordered_columns[2]
        original_width = status.content_width
        calls = _spy_refresh(table)

        app.store.apply_event(
            "pods", "default", "MODIFIED", _pod("pod-39", phase="CrashLoopBackOff")
        )
        await until(
            pilot,
            lambda: status.content_width > original_width,
            label="offscreen width absorbed",
        )

        assert len(calls) == 1
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_table_diff_update.py::test_offscreen_cell_update_changes_data_without_repaint \
  tests/ui/test_table_diff_update.py::test_visible_cell_update_repaints_once \
  tests/ui/test_table_diff_update.py::test_offscreen_width_growth_still_repaints -q
```

Expected: FAIL because inherited `DataTable.update_cell()` refreshes once per changed cell.

- [ ] **Step 3: Batch model updates and repaint only visible changes**

Add `_row_is_visible()`:

```python
    def _row_is_visible(self, key: str) -> bool:
        row_index = self.get_row_index(key)
        fixed_count = min(self.fixed_rows, self.row_count)
        if row_index < fixed_count:
            return True
        fixed_height = sum(row.height for row in self.ordered_rows[:fixed_count])
        header_height = self.header_height if self.show_header else 0
        viewport_height = max(self.size.height - header_height - fixed_height, 0)
        row_region = self._get_row_region(row_index)
        row_top = row_region.y - header_height - fixed_height
        visible_top = int(self.scroll_y)
        visible_bottom = visible_top + viewport_height
        return row_region.height + row_top > visible_top and row_top < visible_bottom
```

Replace `_patch_row()`'s `update_cell()` loop with direct model updates:

```python
        row_key = RowKey(key)
        changed = False
        for column, old_cell, new_cell in zip(columns, old_cells, cells, strict=True):
            if not _cells_equal(old_cell, new_cell):
                self._data[row_key][column.key] = new_cell
                changed = True
        if changed:
            self._update_count += 1
        return changed
```

Change `_absorb_widths()` to return `bool`, initialize `grew = False`, set it
when a column grows, and `return grew`.

In `_apply_in_place()`, collect whether any changed row is visible. After
absorbing touched widths, call `self.refresh()` exactly once if a changed row
is visible or any column grew:

```python
        repaint = False
        for key, cells in pending:
            if key not in current_set:
                self.add_row(*cells, key=key)
                touched.append((key, cells))
            elif self._patch_row(key, cells, columns):
                touched.append((key, cells))
                repaint = repaint or self._row_is_visible(key)
        self._emitted = dict(pending)
        if touched:
            grew = self._absorb_widths(touched)
            if repaint or grew:
                self.refresh()
        return True
```

- [ ] **Step 4: Run focused table and cursor tests**

Run:

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_table_diff_update.py \
  tests/ui/test_cursor_stability.py \
  tests/ui/test_table_column_widths.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Run lint, format, and type checks**

Run:

```bash
.venv/bin/ruff check --fix \
  src/korvid/ui/widgets/resource_table.py \
  tests/ui/test_table_diff_update.py
.venv/bin/ruff format \
  src/korvid/ui/widgets/resource_table.py \
  tests/ui/test_table_diff_update.py
.venv/bin/mypy src/korvid/ui/widgets/resource_table.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/korvid/ui/widgets/resource_table.py tests/ui/test_table_diff_update.py
git commit -m "perf: skip repaints for offscreen row updates" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 5: Repeat final requalification

**Files:**
- Verify only: no repository file changes required.
- Artifacts: `/Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/`

- [ ] **Step 1: Run the final unprofiled acceptance replay**

```bash
.venv/bin/python -m tests.performance.cli replay \
  --profile /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/steady-24eps-1k.json \
  --json /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/final-24eps.json \
  --out /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/final-24eps.md
```

Expected: cursor-input p95 is below `0.100s`, digest match is `true`, and
dropped updates are `0`.

- [ ] **Step 2: Run the final apples-to-apples diagnostic profile**

```bash
.venv/bin/python -m tests.performance.cli replay \
  --profile /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/steady-24eps-1k.json \
  --json /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/final-profiled-24eps.json \
  --out /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/final-profiled-24eps.md \
  --cpu-profile /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/final-24eps.pstats \
  --allocation-snapshot /Users/hwang-inhwan/.copilot/session-state/1d927e2d-c5c1-49c4-a894-61ccd45ddaa4/files/final-24eps-alloc.txt
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

- [ ] **Step 4: Reconfirm the K9s blocker without mutating infrastructure**

```bash
/opt/homebrew/bin/k9s version --short
kubectl config get-contexts aks-korvid-contract-test
az aks show \
  --resource-group rg-korvid-contract-test \
  --name aks-korvid-contract-test \
  --query '{id:id,state:powerState.code}' -o json
kubectl --context aks-korvid-contract-test get namespaces --request-timeout=5s
```

Expected in the current environment: K9s is `0.50.18`, while the AKS resource
group is absent and the kubeconfig endpoint does not resolve. Record the direct
comparison as blocked, not passed or estimated.
