# Task 1 Report — Resource Renderer Dispatch

## Files changed
- `src/korvid/ui/widgets/resource_table.py`
- `tests/ui/test_resource_table_dispatch.py`

## RED evidence
Command:
```bash
uv run pytest -p no:tach tests/ui/test_resource_table_dispatch.py -q
```
Output:
```text
FFFFFFFF                                                                 [100%]
E   AttributeError: module 'korvid.ui.widgets.resource_table' has no attribute '_row_renderer'
```

## GREEN / static checks
Commands:
```bash
uv run pytest -p no:tach tests/ui/test_resource_table_dispatch.py tests/ui/test_column_sorting.py tests/ui/test_drilldown.py tests/ui/test_helm_view.py tests/ui/test_olm_view.py -q
uv run ruff check src/korvid/ui/widgets/resource_table.py tests/ui/test_resource_table_dispatch.py
uv run ruff format --check src/korvid/ui/widgets/resource_table.py tests/ui/test_resource_table_dispatch.py
uv run mypy src/korvid/ui/widgets/resource_table.py
```
Outputs:
```text
86 passed in 78.07s
All checks passed!
2 files already formatted
Success: no issues found in 1 source file
```

## Commit SHA
- `9604b1f0`

## Self-review
- Replaced the `kind` if/elif chain with a registry-backed dispatcher.
- Kept the existing specialized render paths intact and preserved the generic fallback.
- Added a focused selector test that exercises the registry names for the pod and non-pod renderers.
- Verified the focused UI regression tests plus ruff and mypy all pass.

## Concerns
- None beyond the existing reliance on `getattr()` in the standard renderer adapter, which is covered by the focused tests.

## Fix
Commands:
```bash
uv run pytest -p no:tach tests/ui/test_resource_table_dispatch.py -q
uv run pytest -p no:tach tests/ui/test_resource_table_dispatch.py tests/ui/test_column_sorting.py tests/ui/test_drilldown.py tests/ui/test_helm_view.py tests/ui/test_olm_view.py -q
uv run ruff check src/korvid/ui/widgets/resource_table.py tests/ui/test_resource_table_dispatch.py
uv run ruff format --check src/korvid/ui/widgets/resource_table.py tests/ui/test_resource_table_dispatch.py
uv run mypy src/korvid/ui/widgets/resource_table.py
```
Outputs:
```text
9 passed in 0.17s
87 passed in 81.94s
All checks passed!
2 files already formatted
Success: no issues found in 1 source file
```
Commit SHA:
- `54aa65b89e8429d79d6f1b1eca2f75283221b35e`
