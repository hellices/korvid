# PR #279 round 3 inline review fixes

## Status
- Fixed the two open inline findings in `tests/performance/replay.py` / `tests/performance/test_replay.py`.
- Investigated the suppressed width-idle comment by strengthening an existing characterization test only.
- No production `check_idle` / `Idle` change was added.

## RED

### 1. Cursor bounds validation
Added `test_measure_cursor_input_rejects_out_of_bounds_expected_rows` first.

Command:

```bash
.venv/bin/python -m pytest -p no:tach -q \
  tests/performance/test_replay.py -k out_of_bounds_expected_rows
```

Observed RED:
- `2 failed`
- both cases reached `driver.send_message(...)`, proving `measure_cursor_input` still tried to inject `up` from row 0 and `down` from the last row instead of failing immediately.

### 2. Replay clock propagation
Added `test_replay_passes_its_monotonic_clock_to_cursor_sampling` first.

Command:

```bash
.venv/bin/python -m pytest -p no:tach -q \
  tests/performance/test_replay.py -k monotonic_clock_to_cursor_sampling
```

Observed RED:
- `1 failed, 1 passed`
- the injected-clock case still saw builtin `monotonic`, proving `run_replay` was not forwarding `options.monotonic_fn`.

### 3. Suppressed width-idle comment
Strengthened `test_offscreen_width_growth_still_repaints` to assert that off-screen width growth eventually republishes `table.virtual_size.width` with no unrelated scroll/sort/table operation.

Command:

```bash
.venv/bin/python -m pytest -p no:tach -q \
  tests/ui/test_table_diff_update.py -k offscreen_width_growth_still_repaints
```

Observed result on current production code:
- `1 passed`
- characterization already holds, so no production fix was warranted.

## Changes

1. `tests/performance/replay.py::measure_cursor_input`
   - kept unsupported-key validation unchanged.
   - added immediate expected-row bounds validation before key injection.
   - error message now names key, start row, expected row, and valid range exactly.

2. `tests/performance/test_replay.py`
   - added explicit out-of-bounds tests for `up` at row 0 and `down` at the last row.
   - added an unsupported-key regression test.
   - converted the old single-row timeout test into `test_measure_cursor_input_times_out_when_a_valid_move_is_not_acknowledged`, so timeout coverage still exercises a valid target row after the new bounds check.
   - added a regression test proving `run_replay` passes `options.monotonic_fn or monotonic` into `sample_cursor_input`.

3. `tests/performance/replay.py::run_replay`
   - forwarded the same monotonic clock selection live already uses:

   ```python
   now=options.monotonic_fn if options.monotonic_fn is not None else monotonic
   ```

4. `tests/ui/test_table_diff_update.py`
   - strengthened the off-screen width-growth characterization to prove the idle dimension pass republishes `virtual_size.width` without a compensating table operation.
   - no production code changed for this item.

## GREEN

Focused green after the TDD cycle:

```bash
.venv/bin/python -m pytest -p no:tach -q \
  tests/performance/test_replay.py tests/ui/test_table_diff_update.py tests/ui/test_table_column_widths.py
```

Result: `66 passed`

Requested broader verification:

```bash
.venv/bin/python -m pytest -p no:tach -q \
  tests/performance tests/ui/test_table_diff_update.py tests/ui/test_table_column_widths.py
```

Result: `287 passed`

Static checks:

```bash
.venv/bin/ruff check \
  tests/performance/replay.py \
  tests/performance/test_replay.py \
  tests/ui/test_table_diff_update.py \
  tests/ui/test_table_column_widths.py

.venv/bin/ruff format --check \
  tests/performance/replay.py \
  tests/performance/test_replay.py \
  tests/ui/test_table_diff_update.py \
  tests/ui/test_table_column_widths.py

.venv/bin/mypy src/ \
  tests/performance/replay.py \
  tests/performance/test_replay.py \
  tests/ui/test_table_diff_update.py \
  tests/ui/test_table_column_widths.py

.venv/bin/python -m tach check
```

Results:
- `All checks passed!`
- `4 files already formatted`
- `Success: no issues found in 144 source files`
- `✅ All modules validated!`

## Evidence on the suppressed comment

The strengthened off-screen width test now proves all of the following on current code:
- the widened off-screen cell increases `status.content_width`;
- `table.virtual_size.width` increases afterwards;
- no plain `refresh()` call is issued by the diff path.

That matches the documented production path in `ResourceTable._absorb_widths`: width growth sets `_require_update_dimensions`, Textual's idle dimension pass republishes `virtual_size`, and layout repaint follows from that reactive update. The existing implementation already satisfies the comment's concern, so adding a production `check_idle` hook would be speculative.

## Concerns
- None.
