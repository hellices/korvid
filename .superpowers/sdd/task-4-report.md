# Task 4 report

## RED evidence
Command:
`uv run pytest -p no:tach tests/performance/test_metrics.py -k rolls_back_tracemalloc_if_task_creation_fails -q`

Observed failure:
- `AssertionError: assert ['start'] == ['start', 'stop']`
- `pytest.PytestUnraisableExceptionWarning: Exception ignored in: <coroutine object ProcessSampler._run ...>`

## GREEN evidence
Command:
`uv run pytest -p no:tach tests/performance/test_metrics.py -k rolls_back_tracemalloc_if_task_creation_fails -q`

Observed success:
- `1 passed, 15 deselected in 0.05s`

Full file verification:
- `16 passed in 0.04s`

## Self-review
- Added the smallest regression test for start-up rollback when `asyncio.create_task()` fails after tracemalloc ownership is acquired.
- Fixed `ProcessSampler.start()` with scoped rollback that releases owned tracemalloc, resets sampler state, and closes the unstarted coroutine before re-raising.
- Preserved double-start rejection, overlapping sampler ownership, and externally-owned tracemalloc behavior.
- Verified with focused test, full `tests/performance/test_metrics.py`, ruff, mypy, and `git diff --check`.
