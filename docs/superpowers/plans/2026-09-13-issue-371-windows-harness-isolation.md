# Issue 371 Windows Harness Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the Windows documentation landing harness once in a fresh pytest process, then exclude only that already-executed file from the long Windows suite.

**Architecture:** The existing `windows-test` job keeps one runner and one dependency installation but uses two pytest processes. A workflow contract test pins command identity, ordering, conditioning, and exactly-once execution before the workflow is changed.

**Tech Stack:** GitHub Actions, PowerShell runner shell, uv, pytest, PyYAML, zizmor

## Global Constraints

- Keep every test in `tests/test_docs_landing_behavior.py` enforced on Windows exactly once.
- Run the file in a fresh pytest process before the long Windows suite.
- Preserve the explicit `pytest-randomly` seed derived from `github.run_id` in both processes.
- Do not retry, skip, deselect, weaken assertions, add `continue-on-error`, or extend the harness timeout.
- Do not add another Windows runner job or dependency installation.
- Do not change or regenerate `uv.lock`.
- Use the shared uv environment and the developer's global uv proxy configuration for local checks.
- Do not merge, enable auto-merge, or approve the pull request.

---

### Task 1: Enforce and implement fresh-process isolation

**Files:**
- Modify: `tests/test_platforms.py`
- Modify: `tests/test_ci_workflow.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `tests.platforms.read_text_utf8`, `_workflow_job()`, the existing `windows-test` step list, and `github.run_id`
- Produces: one dedicated pytest command for `tests/test_docs_landing_behavior.py`, followed by one full-suite command that ignores that already-executed file

- [ ] **Step 1: Write the failing workflow contract**

In `test_ci_workflow_defines_the_required_windows_test_job`, replace the
`full_suite` value with the new required command:

```python
    full_suite = (
        "uv run pytest -q --ignore=tests/windows/test_native_terminal.py"
        " --ignore=tests/test_docs_landing_behavior.py"
        f" --deselect={audit_regression}"
        " --randomly-seed=${{ github.run_id }}"
    )
```

Add this independent contract below that test:

```python
def test_ci_windows_docs_harness_runs_once_in_fresh_process() -> None:
    windows_job = _workflow_job(_ci_workflow(), "windows-test")
    steps = windows_job["steps"]
    assert isinstance(steps, list)
    runs = [
        step["run"] for step in steps if isinstance(step, dict) and "run" in step
    ]
    docs_harness = (
        "uv run pytest -p no:tach tests/test_docs_landing_behavior.py -q"
        " --randomly-seed=${{ github.run_id }}"
    )
    audit_regression = "tests/core/test_audit.py::test_concurrent_appends_across_instances"
    native_smoke = (
        "uv run pytest -p no:tach tests/windows/test_native_terminal.py"
        f" {audit_regression} -q"
    )
    full_suite = (
        "uv run pytest -q --ignore=tests/windows/test_native_terminal.py"
        " --ignore=tests/test_docs_landing_behavior.py"
        f" --deselect={audit_regression}"
        " --randomly-seed=${{ github.run_id }}"
    )

    assert runs.count(docs_harness) == 1
    assert runs.index(docs_harness) < runs.index(native_smoke) < runs.index(full_suite)
    docs_step = next(step for step in steps if step.get("run") == docs_harness)
    assert docs_step.get("if") == "needs.changes.outputs.code == 'true'"
    assert docs_step.get("continue-on-error", False) is False
```

Replace the existing seed contract in `tests/test_ci_workflow.py` with:

```python
def test_windows_pytest_processes_print_and_share_one_deterministic_seed() -> None:
    windows_job = _jobs(CI_WORKFLOW)["windows-test"]
    runs = _run_steps(windows_job)
    seed_messages = [run for run in runs if "pytest-randomly seed:" in run]
    seeded_runs = [run for run in runs if "--randomly-seed=" in run]

    assert seed_messages == [f'Write-Output "pytest-randomly seed: {WINDOWS_SEED}"']
    assert len(seeded_runs) == 2
    assert all(run.endswith(f" --randomly-seed={WINDOWS_SEED}") for run in seeded_runs)
    assert sum(run.count("--randomly-seed=") for run in runs) == 2
```

- [ ] **Step 2: Run the contract and observe RED**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.worktrees/repository-stabilization-20260911/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run pytest -p no:tach \
  tests/test_platforms.py::test_ci_workflow_defines_the_required_windows_test_job \
  tests/test_platforms.py::test_ci_windows_docs_harness_runs_once_in_fresh_process \
  tests/test_ci_workflow.py::test_windows_pytest_processes_print_and_share_one_deterministic_seed -q
```

Expected: all three tests fail because the dedicated command, the new
full-suite exclusion, and the second seeded pytest process do not yet exist.

- [ ] **Step 3: Add the minimal workflow isolation**

After the seed-reporting step in `.github/workflows/ci.yml`, add:

```yaml
      # This Node subprocess harness is stable in a fresh pytest process but
      # can time out after prolonged execution in the shared Windows process.
      - name: Exercise the docs landing harness in a fresh process
        if: needs.changes.outputs.code == 'true'
        run: uv run pytest -p no:tach tests/test_docs_landing_behavior.py -q --randomly-seed=${{ github.run_id }}
```

Replace the final Windows suite step with:

```yaml
      # The docs landing harness ran above in a fresh process; do not execute
      # it a second time in the long-lived Windows pytest process.
      - if: needs.changes.outputs.code == 'true'
        run: uv run pytest -q --ignore=tests/windows/test_native_terminal.py --ignore=tests/test_docs_landing_behavior.py --deselect=tests/core/test_audit.py::test_concurrent_appends_across_instances --randomly-seed=${{ github.run_id }}
```

- [ ] **Step 4: Run the contract and observe GREEN**

Run the Step 2 command again.

Expected: `3 passed`.

- [ ] **Step 5: Verify the affected workflow surface**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.worktrees/repository-stabilization-20260911/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run pytest -p no:tach tests/test_platforms.py tests/test_ci_workflow.py -q
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.worktrees/repository-stabilization-20260911/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run ruff check tests/test_platforms.py tests/test_ci_workflow.py
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.worktrees/repository-stabilization-20260911/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run ruff format --check tests/test_platforms.py tests/test_ci_workflow.py
uvx zizmor --min-severity medium .github/workflows/ci.yml
```

Expected: `31 passed`, Ruff reports no errors or formatting changes, and
zizmor reports no medium-or-higher findings.

- [ ] **Step 6: Run the complete gate and commit**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.worktrees/repository-stabilization-20260911/.venv \
PYTHONPATH="$PWD/src:$PWD" \
make check
git add tests/test_platforms.py tests/test_ci_workflow.py .github/workflows/ci.yml
git commit -m "ci: isolate Windows docs harness for #371"
```

Expected: all repository checks and commit hooks pass; the commit contains
only the workflow and its contract tests.

### Task 2: Publish and review the permanent fix

**Files:**
- Modify: no additional repository files unless review finds a credible defect

**Interfaces:**
- Consumes: the committed workflow isolation and draft pull request `#386`
- Produces: an updated ready-for-review pull request with all required checks successful and no unresolved blocking review threads

- [ ] **Step 1: Push and convert the diagnostic PR into the fix PR**

Run:

```bash
git push origin fix/371-windows-node-harness-exit
gh pr edit 386 \
  --title "ci: isolate Windows docs harness for #371" \
  --body $'Closes #371\n\n## Evidence\n\n- The isolated Windows run passed 300/300 fresh pytest processes at the same SHA.\n- Ordinary Windows CI failed after roughly 17 minutes in its shared pytest process.\n- The pair is strong evidence that the long-lived pytest process materially contributes; it is not proof of necessity or an exclusive cause.\n\n## Fix\n\n- Run `tests/test_docs_landing_behavior.py` once in an early fresh Windows pytest process.\n- Exclude only that already-executed file from the later full Windows suite.\n- Keep the same random seed and fail normally; no retry, skip, timeout increase, or assertion weakening.\n\n## Validation\n\n- Workflow contract: RED then GREEN\n- Targeted platform tests, Ruff, and zizmor\n- `make check`'
gh pr ready 386
```

Expected: branch push succeeds and PR `#386` is ready for review with the fix
title and description.

- [ ] **Step 2: Request and inspect Copilot review**

Run:

```bash
gh api -X POST repos/hellices/korvid/pulls/386/requested_reviewers \
  -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
gh api graphql -f query='query { repository(owner:"hellices", name:"korvid") { pullRequest(number:386) { reviewRequests(first:20) { nodes { requestedReviewer { ... on Bot { login } ... on User { login } } } } reviews(last:20) { nodes { author { login } state body submittedAt commit { oid } } } reviewThreads(first:100) { nodes { id isResolved comments(first:100) { nodes { databaseId author { login } body path line } } } } } } }'
```

Expected: the request is accepted. Inspect every review body and every thread,
including findings inside `<details>` blocks. Correctness, security,
architecture, data-loss, and required-check findings are blocking; unsupported
low-confidence suggestions are advisory.

- [ ] **Step 3: Apply credible review findings with TDD**

For each credible code finding, first add and run a failing contract test, then
make the smallest fix and run `make check`. Commit without amending. Reply to
the individual comment with the new commit and test name, then resolve its
thread using the comment's `databaseId` and thread `id` from Step 2:

```bash
fix_commit=$(git rev-parse --short HEAD)
comment_id=$(gh api graphql -f query='query { repository(owner:"hellices", name:"korvid") { pullRequest(number:386) { reviewThreads(first:100) { nodes { isResolved comments(last:1) { nodes { databaseId } } } } } } }' --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .comments.nodes[0].databaseId' | head -n 1)
thread_id=$(gh api graphql -f query='query { repository(owner:"hellices", name:"korvid") { pullRequest(number:386) { reviewThreads(first:100) { nodes { id isResolved } } } } }' --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .id' | head -n 1)
: "${comment_id:?unresolved review comment not found}"
: "${thread_id:?unresolved review thread not found}"
gh api "repos/hellices/korvid/pulls/386/comments/$comment_id/replies" \
  -f body="Fixed in $fix_commit; regression coverage: tests/test_platforms.py."
gh api graphql -f query="mutation { resolveReviewThread(input:{threadId:\"$thread_id\"}) { thread { isResolved } } }"
```

Run these commands only after the first unresolved thread is the addressed
finding; otherwise classify the returned thread list and select the matching
IDs from it before replying. Push the new commit, request another review, and repeat.
Stop after two consecutive review rounds containing only suppressed
low-confidence findings and no unresolved blocking finding.

- [ ] **Step 4: Verify every required check and hand back**

Run:

```bash
gh pr checks 386 --watch --fail-fast
gh pr view 386 --json statusCheckRollup,reviews,reviewRequests,url
gh api graphql -f query='query { repository(owner:"hellices", name:"korvid") { pullRequest(number:386) { reviewThreads(first:100) { nodes { id isResolved comments(first:20) { nodes { author { login } body path line } } } } } } }'
```

Expected: every required check has conclusion `SUCCESS`, review requests are
settled, and no credible blocking thread remains unresolved. Report the PR URL
to the maintainer and stop without merging or enabling auto-merge.
