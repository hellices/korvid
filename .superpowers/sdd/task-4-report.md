# Task 4 Report

## Status
DONE

## Commit SHA(s)
- `94b2a1e925de8925eecd6c0b33d41e7d6ebe93c7`
- `afb8d4c5d98d297e048824bde7896aa1fc09b83d`

## Files Changed
- `tests/performance/metrics.py`
- `tests/performance/test_metrics.py`
- `.superpowers/sdd/task-4-report.md`

## RED

Command:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q
```

Output:
```text
==================================== ERRORS ====================================
______________ ERROR collecting tests/performance/test_metrics.py ______________
ImportError while importing test module '/Users/hwang-inhwan/workspace/kube.worktrees/large-cluster-qualification-issue-186/tests/performance/test_metrics.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
../../../.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/lib/python3.12/importlib/__init__.py:90: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/performance/test_metrics.py:9: in <module>
    from tests.performance.metrics import (
E   ModuleNotFoundError: No module named 'tests.performance.metrics'
=========================== short test summary info ============================
ERROR tests/performance/test_metrics.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.29s
```

## GREEN / Validation

Command:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q
uv run ruff check --fix tests/performance/metrics.py tests/performance/test_metrics.py
uv run ruff format tests/performance/metrics.py tests/performance/test_metrics.py
uv run mypy tests/performance/metrics.py tests/performance/test_metrics.py
```

Output:
```text
........                                                                 [100%]
8 passed in 0.07s
All checks passed!
2 files left unchanged
Success: no issues found in 2 source files
```

Additional targeted validation:

Command:
```bash
uv run pytest -p no:tach tests/performance/test_profile.py tests/performance/test_workload.py -q
```

Output:
```text
..........                                                               [100%]
10 passed in 1.16s
```

Command:
```bash
git diff --check
```

Output:
```text
```

## Self-Review
- Reused `ReadTelemetryEvent` exactly as requested for API accounting.
- Kept the change isolated to the new Task 4 metrics module and its tests.
- Verified nearest-rank percentile semantics, coalescing/dropped update accounting, API path preservation, least-squares RSS slope, stable JSON shape, Markdown labels, and `ProcessSampler` warm-up behavior.
- Used frozen dataclasses for published values and kept mutable collection state inside `BenchmarkRecorder`.

## Concerns
- `psutil` does not ship typing stubs in this environment, so `tests/performance/metrics.py` uses an explicit `# type: ignore[import-untyped]` with a reason to satisfy strict mypy while preserving the required dependency.

## Fix Review Findings

### Finding 1: Immutable published API mappings and copied JSON payloads

RED command:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q -k immutable_and_payloads_are_copied
```

RED output:
```text
F                                                                        [100%]
=================================== FAILURES ===================================
_______ test_api_summary_mappings_are_immutable_and_payloads_are_copied ________
tests/performance/test_metrics.py:130: in test_api_summary_mappings_are_immutable_and_payloads_are_copied
    with pytest.raises(TypeError, match="does not support item assignment"):
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   Failed: DID NOT RAISE TypeError
=========================== short test summary info ============================
FAILED tests/performance/test_metrics.py::test_api_summary_mappings_are_immutable_and_payloads_are_copied
1 failed, 8 deselected in 0.06s
```

GREEN commands:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q -k immutable_and_payloads_are_copied
uv run pytest -p no:tach tests/performance/test_metrics.py -q
```

GREEN output:
```text
.                                                                        [100%]
1 passed, 8 deselected in 0.06s

.........                                                                [100%]
9 passed in 0.07s
```

### Finding 2: Reject ProcessSampler double-start

RED command:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q -k rejects_double_start
```

RED output:
```text
F                                                                        [100%]
=================================== FAILURES ===================================
__________________ test_process_sampler_rejects_double_start ___________________
tests/performance/test_metrics.py:274: in test_process_sampler_rejects_double_start
    with pytest.raises(RuntimeError, match="already running"):
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   Failed: DID NOT RAISE RuntimeError
=========================== short test summary info ============================
FAILED tests/performance/test_metrics.py::test_process_sampler_rejects_double_start
1 failed, 9 deselected in 0.07s
```

GREEN commands:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q -k rejects_double_start
uv run pytest -p no:tach tests/performance/test_metrics.py -q
```

GREEN output:
```text
.                                                                        [100%]
1 passed, 9 deselected in 0.05s

..........                                                               [100%]
10 passed in 0.07s
```

### Finding 3: Start/own tracemalloc only when needed

RED command:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q -k tracemalloc
```

RED output:
```text
.F                                                                       [100%]
=================================== FAILURES ===================================
___________ test_process_sampler_starts_and_stops_owned_tracemalloc ____________
tests/performance/test_metrics.py:335: in test_process_sampler_starts_and_stops_owned_tracemalloc
    samples = await sampler.stop()
              ^^^^^^^^^^^^^^^^^^^^
tests/performance/metrics.py:210: in stop
    await task
tests/performance/metrics.py:221: in _run
    python_bytes=tracemalloc.get_traced_memory()[0],
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/performance/test_metrics.py:322: in _get_traced_memory
    raise RuntimeError("tracemalloc not tracing")
E   RuntimeError: tracemalloc not tracing
=========================== short test summary info ============================
FAILED tests/performance/test_metrics.py::test_process_sampler_starts_and_stops_owned_tracemalloc
1 failed, 1 passed, 10 deselected in 0.08s
```

GREEN commands:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q -k tracemalloc
uv run pytest -p no:tach tests/performance/test_metrics.py -q
```

GREEN output:
```text
..                                                                       [100%]
2 passed, 10 deselected in 0.05s

............                                                             [100%]
12 passed in 0.07s
```

### Finding 4: Count relists only after a 410 recovery

RED command:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q -k relist
```

RED output:
```text
.F                                                                       [100%]
=================================== FAILURES ===================================
__________ test_api_summary_does_not_treat_repeated_lists_as_relists ___________
tests/performance/test_metrics.py:128: in test_api_summary_does_not_treat_repeated_lists_as_relists
    assert report.api.relists == 0
E   AssertionError: assert 1 == 0
E    +  where 1 = ApiSummary(operations=mappingproxy({'list': 2}), paths=mappingproxy({'/api/v1/pods': mappingproxy({'list': 2})}), decoded_bytes=0, object_count=0, watch_events=0, reconnects=0, relists=1, throttles=0, authorization_failures=0).relists
E    +    where ApiSummary(operations=mappingproxy({'list': 2}), paths=mappingproxy({'/api/v1/pods': mappingproxy({'list': 2})}), decoded_bytes=0, object_count=0, watch_events=0, reconnects=0, relists=1, throttles=0, authorization_failures=0) = BenchmarkReport(manifest=RunManifest(profile_id='smoke-1k', profile_hash='profile-hash', korvid_sha='3cbe600996043cd6c...orization_failures=0), rendered_updates=0, render_passes=0, coalesced_updates=0, dropped_updates=0, final_digest='abc').api
=========================== short test summary info ============================
FAILED tests/performance/test_metrics.py::test_api_summary_does_not_treat_repeated_lists_as_relists
1 failed, 1 passed, 12 deselected in 0.07s
```

GREEN commands:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q -k relist
uv run pytest -p no:tach tests/performance/test_metrics.py -q
```

GREEN output:
```text
..                                                                       [100%]
2 passed, 12 deselected in 0.05s

..............                                                           [100%]
14 passed in 0.07s
```

### Final validation after review fixes

Commands:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q
uv run ruff check --fix tests/performance/metrics.py tests/performance/test_metrics.py
uv run ruff format tests/performance/metrics.py tests/performance/test_metrics.py
uv run mypy tests/performance/metrics.py tests/performance/test_metrics.py
```

Output:
```text
..............                                                           [100%]
14 passed in 0.12s
All checks passed!
2 files left unchanged
Success: no issues found in 2 source files
```

### Self-Review for Review Fixes
- Published `ApiSummary` collections now expose immutable mappings, while `report_payload()` deep-copies them into fresh JSON-ready dicts.
- `ProcessSampler.start()` now fails deterministically on double-start and tracks tracemalloc ownership so Task 6 snapshots remain intact when tracing was already active.
- Relist counting now follows the design requirement: only a later `list` after a same-path status-410 event increments `relists`.
- The fix stayed isolated to the Task 4 metrics module, Task 4 tests, and this durable report.

### Finding 5: Keep process-global tracemalloc active across overlapping samplers

RED command:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q -k keeps_owned_tracemalloc_until_last_overlapping_sampler_stops
```

RED output:
```text
F                                                                        [100%]
=================================== FAILURES ===================================
_ test_process_sampler_keeps_owned_tracemalloc_until_last_overlapping_sampler_stops _
tests/performance/test_metrics.py:374: in test_process_sampler_keeps_owned_tracemalloc_until_last_overlapping_sampler_stops
    assert lifecycle == ["start"]
E   AssertionError: assert ['start', 'stop'] == ['start']
E
E     Left contains one more item: 'stop'
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/performance/test_metrics.py::test_process_sampler_keeps_owned_tracemalloc_until_last_overlapping_sampler_stops
1 failed, 14 deselected in 0.07s
```

GREEN command:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q -k keeps_owned_tracemalloc_until_last_overlapping_sampler_stops
```

GREEN output:
```text
.                                                                        [100%]
1 passed, 14 deselected in 0.05s
```

Final validation commands:
```bash
uv run pytest -p no:tach tests/performance/test_metrics.py -q
uv run ruff check --fix tests/performance/metrics.py tests/performance/test_metrics.py
uv run ruff format tests/performance/metrics.py tests/performance/test_metrics.py
uv run mypy tests/performance/metrics.py tests/performance/test_metrics.py
git diff --check
```

Final validation output:
```text
...............                                                          [100%]
15 passed in 0.04s
All checks passed!
2 files left unchanged
Success: no issues found in 2 source files
```

Self-review:
- Added one focused async regression test that proves overlapping samplers must not stop process-global tracing until the last internally-managed sampler exits.
- Replaced per-instance tracemalloc ownership with a single class-level managed-user count so internally-started tracing stays alive across overlap, while externally-started tracing still remains untouched.
- Kept double-start rejection unchanged and limited production edits to `ProcessSampler`.
