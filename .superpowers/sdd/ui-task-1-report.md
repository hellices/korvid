# UI Wait Cleanup Task 1 Report

## Baseline

- Focused baseline: `uv run --no-sync pytest -p no:tach tests/ui/test_metrics_wiring.py -q`
- Result: `8 passed`

## Changes

- Removed the local `_until` helper from `tests/ui/test_metrics_wiring.py`.
- Reused `tests.ui.waits.until` instead.
- Added specific `label=` values to every `until` call in the file.
- Left existing time-semantic `pilot.pause(...)` waits unchanged.

## Verification

- `uv run --no-sync pytest -p no:tach tests/ui/test_metrics_wiring.py -q`
- `uv run --no-sync ruff check tests/ui/test_metrics_wiring.py`
- `uv run --no-sync ruff format --check tests/ui/test_metrics_wiring.py`

## Commit

- Pending

## Self-review

- No product code changed.
- No duplicate polling helper remains.
- All `until` calls now have file-specific labels.
- Focused checks passed after formatting.
