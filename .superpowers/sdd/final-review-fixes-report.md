# Final review fix report

## Issue
Final review issue 1: `_base_screen_ready` was already true on `run_test()` entry in these four UI test files, so `until(_base_screen_ready)` was vacuous:
- `tests/ui/test_agent_write.py`
- `tests/ui/test_agent_wiring.py`
- `tests/ui/test_dryrun_preview.py`
- `tests/ui/test_node_shell.py`

## Audit decisions
- `tests/ui/test_agent_write.py`
  - Removed `_base_screen_ready` and every `until(..._base_screen_ready...)` call.
  - Kept real downstream waits already tied to the exercised behavior: `ConfirmScreen` appearance, task completion-or-dialog expiry, notifications, and write side effects.
  - Direct error-path tests now rely on the awaited `agent_request_write(...)` call itself instead of a vacuous entry-true predicate.
  - Removed `pilot` bindings only where the test body no longer used pilot after the vacuous wait removal.
- `tests/ui/test_agent_wiring.py`
  - Removed `_base_screen_ready` and both no-op waits.
  - Kept the real waits on setup-screen initialization and notification text.
- `tests/ui/test_dryrun_preview.py`
  - Removed `_base_screen_ready` and every no-op wait.
  - Kept `_to_deployments()` unchanged, including its deployments-row/render wait.
- `tests/ui/test_node_shell.py`
  - Removed `_base_screen_ready` and every no-op wait.
  - Kept `_to_nodes()` unchanged, including its nodes-row/render wait.
- `tests/ui/test_agent_interrupt.py`
  - A dependent import breakage appeared during commit-time mypy because this file imported `_base_screen_ready` from `test_agent_write.py`.
  - Removed those imports and the same vacuous waits from the three dependent approval-interrupt tests.
  - Kept the real downstream waits on `ConfirmScreen`, cancellation cleanup, and write completion.

## Static RED/GREEN evidence

### RED (before change)
Command:
```bash
rg -n "_base_screen_ready" tests/ui/test_agent_write.py tests/ui/test_agent_wiring.py tests/ui/test_dryrun_preview.py tests/ui/test_node_shell.py
```
Observed before editing: matches existed in all four files for the helper definition and its call sites.

### GREEN (after change)
Command:
```bash
rg -n "_base_screen_ready" tests/ui/test_agent_write.py tests/ui/test_agent_wiring.py tests/ui/test_dryrun_preview.py tests/ui/test_node_shell.py
```
Observed after editing: no matches.

## Exact tests and checks
Initial required four-file run:
```bash
uv run pytest -p no:tach -q tests/ui/test_agent_write.py tests/ui/test_agent_wiring.py tests/ui/test_dryrun_preview.py tests/ui/test_node_shell.py
```
Result:
- `91 passed in 32.32s`

Dependent follow-up after import breakage surfaced:
```bash
uv run pytest -p no:tach -q tests/ui/test_agent_write.py tests/ui/test_agent_wiring.py tests/ui/test_dryrun_preview.py tests/ui/test_node_shell.py tests/ui/test_agent_interrupt.py
uv run ruff check tests/ui/test_agent_write.py tests/ui/test_agent_wiring.py tests/ui/test_dryrun_preview.py tests/ui/test_node_shell.py tests/ui/test_agent_interrupt.py
uv run ruff format --check tests/ui/test_agent_write.py tests/ui/test_agent_wiring.py tests/ui/test_dryrun_preview.py tests/ui/test_node_shell.py tests/ui/test_agent_interrupt.py
uv run mypy src/ tests/ui/test_agent_write.py tests/ui/test_agent_wiring.py tests/ui/test_dryrun_preview.py tests/ui/test_node_shell.py tests/ui/test_agent_interrupt.py
```

Results:
- `112 passed in 40.33s`
- `All checks passed!`
- `5 files already formatted`
- `Success: no issues found in 159 source files`

## Commit
- Message: `test: remove vacuous UI readiness waits`
- Trailer: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`

---

## Issue
Final review issue 2: `tests/ui/test_agent_interrupt.py` imported branch-added helpers from `tests/ui/test_agent_write.py`, coupling one test module to another.

## Move inventory and equivalence
- Added neutral support module: `tests/ui/agent_write_support.py`
- Moved source-equivalently:
  - `Recorder`
  - `_expand_panel`
  - `make_app`
  - required shared constants/helpers for those symbols: `_DEPLOY_META`, `_ALIASES`
- Support module imports only stdlib + product modules; it does not import any `test_*.py`.
- `tests/ui/test_agent_write.py`
  - Now imports shared support symbols from `tests/ui/agent_write_support.py`.
  - Test bodies/assertions/decorators/node IDs were preserved; the `_DEPLOY_META` identity assertion still points at the moved constant.
- `tests/ui/test_agent_interrupt.py`
  - Replaced the three `from tests.ui.test_agent_write ...` lines with module-level imports from the support module.
  - Kept scenario-specific doubles (`SlowRecorder`, `ShieldProbe`) in place; no behavior changes.

## Collect equivalence
Baseline and final normalized collect node-id sets were compared for both files and matched exactly.

| File | Baseline count | Final count | Exact normalized set |
| --- | ---: | ---: | --- |
| `tests/ui/test_agent_write.py` | 26 | 26 | unchanged |
| `tests/ui/test_agent_interrupt.py` | 21 | 21 | unchanged |

## Verification
Commands:
```bash
uv run --no-sync pytest -p no:tach --collect-only -q tests/ui/test_agent_write.py
uv run --no-sync pytest -p no:tach --collect-only -q tests/ui/test_agent_interrupt.py
uv run --no-sync pytest -p no:tach -q tests/ui/test_agent_write.py tests/ui/test_agent_interrupt.py
uv run --no-sync ruff check tests/ui/agent_write_support.py tests/ui/test_agent_write.py tests/ui/test_agent_interrupt.py
uv run --no-sync ruff format --check tests/ui/agent_write_support.py tests/ui/test_agent_write.py tests/ui/test_agent_interrupt.py
uv run --no-sync mypy tests/ui/agent_write_support.py tests/ui/test_agent_write.py tests/ui/test_agent_interrupt.py
rg -n "from tests\\.ui\\.test_agent_write|from \\.test_agent_write|import test_agent_write" tests/ui/agent_write_support.py tests/ui/test_agent_write.py tests/ui/test_agent_interrupt.py
```

Results:
- collect equivalence: exact normalized node-id sets unchanged (`26`, `21`)
- pytest: `47 passed in 10.96s`
- ruff check: `All checks passed!`
- ruff format --check: `3 files already formatted`
- mypy: `Success: no issues found in 3 source files`
- rg: no matches in touched files

## Commit
- Message: `test: extract agent write test support`
- Trailer: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`

---

## Issue
Second whole-branch review issue: `_base_screen_ready` in `tests/ui/test_helm_view.py` and `tests/ui/test_hierarchy_nav.py` was already true on `run_test()` entry, so every `until(..._base_screen_ready...)` call was vacuous.

## Audit decisions
- `tests/ui/test_helm_view.py`
  - Removed `_base_screen_ready` and all five startup waits.
  - Kept the real downstream observables already attached to the behavior under test: `_navigate(..., expect_kind=...)`, release/revision row counts, revision drill-down activation, and describe-call capture.
- `tests/ui/test_hierarchy_nav.py`
  - Removed `_base_screen_ready` and all startup waits, including the helper definition.
  - Kept each test anchored to its actual dependency instead of a startup-default predicate:
    - `_navigate(..., expect_kind=...)` for command-driven view switches.
    - row counts / selected row keys for table readiness.
    - `HierarchyScreen` appearance/dismissal for tree open/close flows.
    - `_empty_pods_view_rendered(...)` for the explicit pods navigation cases.
    - store population after `watch_manager.start(...)` for watch-backed scenarios.
    - hierarchy worker creation/finish predicates for the fetch-race scenario.
    - notification capture for missing jump-target behavior.
  - `test_refresh_hierarchy_survives_an_empty_screen_stack()` no longer binds an unused `pilot`; the teardown interleaving still starts only after `run_test()` entry and immediately patches `App.screen` to the failure seam it exercises.
  - No sleeps or replacement startup predicates were introduced.

## Static RED/GREEN evidence

### RED (before change)
Command:
```bash
rg -n "_base_screen_ready" tests/ui/test_helm_view.py tests/ui/test_hierarchy_nav.py
```
Observed before editing: `32 matches across 2 files` (`6` in `test_helm_view.py`, `26` in `test_hierarchy_nav.py`) covering the helper definitions and all call sites.

### GREEN (after change)
Command:
```bash
rg -n "_base_screen_ready" tests/ui/test_helm_view.py tests/ui/test_hierarchy_nav.py
```
Observed after editing: no matches.

## Exact tests and checks
Commands:
```bash
uv run --no-sync pytest -p no:tach -q tests/ui/test_helm_view.py tests/ui/test_hierarchy_nav.py
uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_hierarchy_nav.py::test_return_is_refused_when_a_ctx_switch_starts_during_the_navigate \
  tests/ui/test_hierarchy_nav.py::test_tree_does_not_open_when_view_changed_during_fetch
uv run --no-sync ruff check tests/ui/test_helm_view.py tests/ui/test_hierarchy_nav.py
uv run --no-sync ruff format --check tests/ui/test_helm_view.py tests/ui/test_hierarchy_nav.py
uv run --no-sync mypy tests/ui/test_helm_view.py tests/ui/test_hierarchy_nav.py
```

Results:
- pytest (both files): `32 passed in 32.02s`
- repeated representative navigation/worker race tests: `2 passed in 2.39s`
- ruff check: `All checks passed!`
- ruff format --check: `2 files already formatted`
- mypy: `Success: no issues found in 2 source files`

## Commit
- Message: `test: remove remaining vacuous startup waits`
- Trailer: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
