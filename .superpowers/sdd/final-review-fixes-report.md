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
