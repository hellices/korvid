# Task 1 Report — issue #334

## Scope and implementation note
- Task: close the fail-open final Pod UID revalidation before name-based transfer/debug exec.
- In this branch, the shared Pod UID guard lives in `src/korvid/ui/resource_inspect_controller.py` rather than `src/korvid/ui/app.py`, so the fix was applied there while preserving the existing wiring.

## RED evidence

### 1) Transfer regression added first, then verified failing
Command:
```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_transfer.py::test_transfer_blocked_when_final_uid_lookup_unavailable \
  -q
```
Result:
- `4 failed in 23.30s`
- All four parameter cases failed with `WaitTimeout: retryable verification warning not met within 5.0s`
- Captured log evidence showed the existing fail-open behavior:
  - `uid lookup for default/api-1 timed out; writing without precondition`
  - `uid lookup for default/api-1 failed; writing without precondition`

### 2) Debug regression added first, then verified failing
Command:
```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_shell.py::test_debug_aborts_when_final_pod_uid_lookup_unavailable \
  -q
```
Result:
- `1 failed in 6.03s`
- Failure: `WaitTimeout: condition not met within 5.0s`
- Captured log evidence showed the same fail-open path:
  - `uid lookup for default/api-1 timed out; writing without precondition`

## GREEN implementation
- Changed the shared Pod UID helper to fail closed when the final lookup returns `None`.
- New behavior:
  - `ApiStatusError` => notify `no longer exists`, return `False`
  - `None` UID => notify `could not be verified. Retry when the cluster is reachable.`, return `False`
  - mismatched UID => notify `was replaced since the prompt was shown.`, return `False`
  - exact non-`None` match => return `True`

## GREEN verification commands and results

### Transfer regression set
Command:
```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_transfer.py::test_transfer_blocked_when_final_uid_lookup_unavailable \
  tests/ui/test_transfer.py::test_upload_blocked_when_pod_replaced_after_approval \
  tests/ui/test_transfer.py::test_download_blocked_when_pod_replaced \
  tests/ui/test_transfer.py::test_upload_proceeds_when_uid_unchanged \
  -q
```
Result:
- `7 passed in 3.27s`

### Shared debug boundary set
Command:
```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_shell.py::test_debug_aborts_when_final_pod_uid_lookup_unavailable \
  tests/ui/test_shell.py::test_debug_aborts_when_pod_replaced_after_prompt \
  tests/ui/test_shell.py::test_debug_runs_when_pod_uid_unchanged \
  -q
```
Result:
- `3 passed in 1.86s`

### Complete targeted behavior suite
Command:
```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_transfer.py \
  tests/ui/test_transfer_controller.py \
  tests/ui/test_transfer_picker.py \
  tests/ui/test_shell.py \
  -q
```
Result:
- `142 passed in 29.18s`

### Final verification pass
Command:
```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_transfer.py \
  tests/ui/test_transfer_controller.py \
  tests/ui/test_transfer_picker.py \
  tests/ui/test_shell.py \
  -q
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check \
  src/korvid/ui/app.py src/korvid/ui/resource_inspect_controller.py \
  tests/ui/test_transfer.py tests/ui/test_shell.py
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check \
  src/korvid/ui/app.py src/korvid/ui/resource_inspect_controller.py \
  tests/ui/test_transfer.py tests/ui/test_shell.py
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy \
  src/korvid/ui/app.py src/korvid/ui/resource_inspect_controller.py
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/tach check
git diff --check
```
Result:
- `142 passed in 29.70s`
- `All checks passed!`
- `4 files already formatted`
- `Success: no issues found in 2 source files`
- `✅ All modules validated!`
- `git diff --check` exited 0

## Files changed
- `src/korvid/ui/resource_inspect_controller.py`
- `tests/ui/test_transfer.py`
- `tests/ui/test_shell.py`

## Commit
- `0e01809eef4f3a4c511db3d41f822a544bb4513e`
- Message: `security: fail closed on unavailable pod UID`
- Includes required trailer:
  - `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`

## Self-review
- Reviewed the final diff directly with `git --no-pager diff -- src/korvid/ui/resource_inspect_controller.py tests/ui/test_transfer.py tests/ui/test_shell.py`.
- Confirmed the production change is surgical: only the shared Pod UID revalidation semantics changed.
- Confirmed the new tests cover both transfer directions and debug, and assert the guarded operations never reach exec or audit when final UID lookup is unavailable.
- Confirmed the pre-existing replaced/no-longer-exists behaviors remain covered by adjacent regression tests.
- No follow-up code issues found in self-review.

## Concerns
- No functional concerns.
- Informational: the task brief referenced `src/korvid/ui/app.py`, but this branch's current decomposition places the shared helper in `src/korvid/ui/resource_inspect_controller.py`; the fix was applied at that live shared boundary.

## 2026-09-02 addendum — verified review findings follow-up

### RED evidence for the stale resource-inspect expectation
Command:
```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_resource_inspect_controller.py::test_pod_uid_unchanged_fails_open_when_the_uid_is_unknown \
  -q
```
Result:
- `1 failed in 0.37s`
- Assertion proved the test still expected the old fail-open contract:
  - `assert False`

### Test updates applied
- Replaced `test_pod_uid_unchanged_fails_open_when_the_uid_is_unknown` with
  `test_pod_uid_unchanged_refuses_an_unverifiable_pod`.
- New assertions now require:
  - `False` from `pod_uid_unchanged(...)`
  - a warning containing both `could not be verified` and `Retry`
  - no `no longer exists` / `was replaced` wording
- Simplified `tests/ui/test_transfer.py` parametrization to `failure_factory`
  only, with explicit `timeout` and `runtime-error` ids.

### GREEN verification for the follow-up fixes
Command:
```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_resource_inspect_controller.py::test_pod_uid_unchanged_refuses_an_unverifiable_pod \
  -q
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_resource_inspect_controller.py \
  -q
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_transfer.py \
  -q
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_transfer.py \
  tests/ui/test_transfer_controller.py \
  tests/ui/test_transfer_picker.py \
  tests/ui/test_shell.py \
  -q
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check --fix \
  tests/ui/test_resource_inspect_controller.py tests/ui/test_transfer.py
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format \
  tests/ui/test_resource_inspect_controller.py tests/ui/test_transfer.py
git diff --check
```
Result:
- `1 passed in 0.29s`
- `37 passed in 0.24s`
- `24 passed in 7.80s`
- `142 passed in 30.53s`
- `ruff check --fix`: `All checks passed!`
- `ruff format`: `1 file reformatted, 1 file left unchanged`
- `git diff --check` exited 0
