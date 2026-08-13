# Task 4 Report — Suppress off-screen cell repaints

## Summary

Implemented Task 4 in `ResourceTable` with strict TDD:

- added the three required viewport-aware refresh tests,
- captured RED failures before touching production code,
- implemented the smallest safe fix inside `ResourceTable`,
- re-ran focused regressions plus lint/type checks.

## Files changed

- `src/korvid/ui/widgets/resource_table.py`
- `tests/ui/test_table_diff_update.py`
- `.superpowers/sdd/task-4-report.md`

## RED phase

Command:

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_table_diff_update.py::test_offscreen_cell_update_changes_data_without_repaint \
  tests/ui/test_table_diff_update.py::test_visible_cell_update_repaints_once \
  tests/ui/test_table_diff_update.py::test_offscreen_width_growth_still_repaints -q
```

Exact RED failures:

```text
FFF                                                                      [100%]
=================================== FAILURES ===================================
___________ test_offscreen_cell_update_changes_data_without_repaint ____________
tests/ui/test_table_diff_update.py:58: in test_offscreen_cell_update_changes_data_without_repaint
    assert calls == []
E   AssertionError: assert [((), {}), ((...ose': False})] == []
E
E     Left contains 3 more items, first extra item: ((), {})
E     Use -v to get more diff
____________________ test_visible_cell_update_repaints_once ____________________
tests/ui/test_table_diff_update.py:75: in test_visible_cell_update_repaints_once
    assert len(calls) == 1
E   AssertionError: assert 3 == 1
E    +  where 3 = len([((), {}), ((), {'repaint': True, 'layout': True, 'recompose': False}), ((), {'repaint': True, 'layout': True, 'recompose': False})])
__________________ test_offscreen_width_growth_still_repaints __________________
tests/ui/test_table_diff_update.py:96: in test_offscreen_width_growth_still_repaints
    assert len(calls) == 1
E   AssertionError: assert 4 == 1
E    +  where 4 = len([((), {}), ((), {'repaint': True, 'layout': True, 'recompose': False}), ((), {'repaint': True, 'layout': True, 'recompose': False}), ((), {'repaint': True, 'layout': True, 'recompose': False})])
=========================== short test summary info ============================
FAILED tests/ui/test_table_diff_update.py::test_offscreen_cell_update_changes_data_without_repaint
FAILED tests/ui/test_table_diff_update.py::test_visible_cell_update_repaints_once
FAILED tests/ui/test_table_diff_update.py::test_offscreen_width_growth_still_repaints
3 failed in 0.75s
```

## Root-cause notes

- The intended failure was real: inherited `DataTable.update_cell()` always calls `refresh()`.
- Additional refresh noise came from pending layout/scrollbar work already queued before the spy attached.
- After tracing refresh callsites, the test harness needed one surgical stabilization step: `await pilot.pause()` before spying so the tests observe only refreshes caused by the modified event.
- Width growth already causes a Textual layout refresh through `_require_update_dimensions` on idle; adding an extra manual `self.refresh()` in that path doubled repaint work.

## Implementation

### Production changes

1. Added `ResourceTable._row_is_visible(key: str) -> bool`.
2. Replaced per-cell `update_cell(..., update_width=False)` calls in `_patch_row()` with direct subclass-internal model updates:
   - `self._data[row_key][column.key] = new_cell`
   - `self._update_count += 1` once per changed row batch
3. Changed `_absorb_widths(...)` to return `bool` for column growth.
4. In `_apply_in_place()`:
   - tracked whether any changed row is visible,
   - requested exactly one explicit repaint only for visible changed rows **without** width growth,
   - let width-growth updates rely on Textual's already-scheduled layout refresh.

### Test changes

Added the three required behavior tests plus the refresh spy helper.  
Applied one surgical correction to the example: each new refresh test pauses once after initial load and scrollability checks before installing the spy, preventing unrelated queued refreshes from polluting the count.

## GREEN phase

Command:

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_table_diff_update.py::test_offscreen_cell_update_changes_data_without_repaint \
  tests/ui/test_table_diff_update.py::test_visible_cell_update_repaints_once \
  tests/ui/test_table_diff_update.py::test_offscreen_width_growth_still_repaints -q
```

Output:

```text
...                                                                      [100%]
3 passed in 0.94s
```

## Verification

Focused regression command:

```bash
.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_table_diff_update.py \
  tests/ui/test_cursor_stability.py \
  tests/ui/test_table_column_widths.py -q
```

Output:

```text
.................................                                        [100%]
33 passed in 7.13s
```

Lint / format / type command:

```bash
.venv/bin/ruff check --fix src/korvid/ui/widgets/resource_table.py tests/ui/test_table_diff_update.py && \
.venv/bin/ruff format src/korvid/ui/widgets/resource_table.py tests/ui/test_table_diff_update.py && \
.venv/bin/mypy src/korvid/ui/widgets/resource_table.py
```

Output:

```text
All checks passed!
2 files left unchanged
Success: no issues found in 1 source file
```

## Self-review: Textual cache / width / visibility semantics

- **Model correctness preserved:** `_data` updates happen immediately, so `get_row()`, later scrolls, filtering, and sorting all see the new cell value right away.
- **Cache generation preserved:** `_update_count` still increments on changed rows, so Textual's row/offset caches are invalidated like `update_cell()` would do.
- **Visible-row repaint preserved:** any changed row intersecting the current viewport still causes one explicit `refresh()`.
- **Off-screen repaint suppression preserved:** off-screen cell changes update the model but do not force an otherwise invisible repaint.
- **Width-growth repaint preserved:** column growth still changes `column.content_width`, marks `_require_update_dimensions = True`, and receives a layout refresh from Textual's idle dimension pass.
- **Add/remove/reorder behavior preserved:** existing `add_row`, `remove_row`, and rebuild fallback paths remain unchanged.
- **Cursor / viewport behavior preserved:** cursor-follow and viewport-restore logic still runs through the same outer `show()` path and stayed green in `tests/ui/test_cursor_stability.py`.
- **Protected internals rule respected:** direct `DataTable` internals are touched only inside `ResourceTable`, mirroring `update_cell()`'s data write and `_update_count` bump while intentionally omitting per-cell refresh batching.

## Commit

Commit SHA: `PENDING`

Commit message:

```text
perf: skip repaints for offscreen row updates
```

with required trailer:

```text
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```

## Concerns

- This optimization intentionally depends on current Textual internals: `_data`, `_update_count`, `_get_row_region()`, and the idle/layout refresh triggered by `_require_update_dimensions`. The regression tests now pin that contract, but a future Textual internal change could require revisiting this batching path.
- The provided sketch needed one surgical correction: width-growth repaint should not also call an extra manual `refresh()` because Textual already repaints during the dimension/layout pass, and forcing both creates duplicate repaint work.
