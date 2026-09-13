# Issue 371 Windows Harness Diagnostic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the single #371 documentation-harness regression test in 300 fresh pytest processes on GitHub-hosted Windows and preserve the first failure without masking it.

**Architecture:** A temporary pull-request workflow owns one least-privilege `windows-latest` job. PowerShell invokes one test node per fresh pytest process, exits on the first non-zero status, and leaves the existing ten-second harness timeout and diagnostics untouched; after the run is recorded, the temporary workflow is removed before any permanent solution is proposed.

**Tech Stack:** GitHub Actions, PowerShell 7, uv, pytest, PyYAML, zizmor

## Global Constraints

- Run only `tests/test_docs_landing_behavior.py::test_harness_nonzero_exit_preserves_captured_output`.
- Run at most 300 independent pytest processes and stop on the first failure.
- Keep the harness's existing 10-second timeout; do not retry, skip, deselect, or weaken the assertion.
- Bound the GitHub Actions job to 20 minutes.
- Use `windows-latest`, Python 3.12, and the locked development environment.
- Use the exact `actions/checkout` and `astral-sh/setup-uv` revisions pinned in `.github/workflows/ci.yml`.
- Grant only `contents: read`, persist no checkout credentials, and access no secrets.
- Do not change or regenerate `uv.lock`.
- Remove the diagnostic workflow after capturing its run URL and logs; never merge the temporary workflow.
- Do not merge, enable auto-merge, or approve the pull request.

---

### Task 1: Add and validate the temporary Windows diagnostic

**Files:**
- Create: `.github/workflows/issue-371-diagnostic.yml`
- Test: `tests/test_platforms.py`

**Interfaces:**
- Consumes: the existing pytest node `tests/test_docs_landing_behavior.py::test_harness_nonzero_exit_preserves_captured_output` and action pins from `.github/workflows/ci.yml`
- Produces: a pull-request workflow job named `windows-harness-diagnostic` whose process exit status is the first failed trial's status, or zero after 300 passes

- [ ] **Step 1: Add the diagnostic workflow**

```yaml
name: Issue 371 Windows harness diagnostic

on:
  pull_request:
    branches: [main]
    types: [opened, reopened, synchronize]

permissions:
  contents: read

concurrency:
  group: issue-371-diagnostic-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  windows-harness-diagnostic:
    if: >-
      github.event.pull_request.head.repo.full_name == github.repository &&
      github.event.pull_request.head.ref == 'fix/371-windows-node-harness-exit'
    runs-on: windows-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d  # v10.0.1
        with:
          python-version: "3.12"
          enable-cache: true
      - name: Install the locked development environment
        run: uv sync --locked --dev --all-extras
      - name: Report runner and tool versions
        shell: pwsh
        run: |
          Write-Output "ImageOS=$env:ImageOS"
          Write-Output "ImageVersion=$env:ImageVersion"
          Get-Command node | Format-List -Property Source,Version
          node --version
          python --version
          uv --version
      - name: Run 300 isolated harness trials
        shell: pwsh
        run: |
          $test = "tests/test_docs_landing_behavior.py::test_harness_nonzero_exit_preserves_captured_output"
          for ($trial = 1; $trial -le 300; $trial++) {
            Write-Output "::group::trial $trial/300"
            uv run pytest -p no:tach $test -q
            $status = $LASTEXITCODE
            Write-Output "::endgroup::"
            if ($status -ne 0) {
              Write-Output "::error::trial $trial/300 failed with exit code $status"
              exit $status
            }
          }
          Write-Output "All 300 isolated trials passed."
```

- [ ] **Step 2: Parse the workflow as YAML**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.worktrees/repository-stabilization-20260911/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run python -c 'from pathlib import Path; import yaml; data = yaml.safe_load(Path(".github/workflows/issue-371-diagnostic.yml").read_text()); assert isinstance(data, dict); assert "jobs" in data'
```

Expected: exit 0 with no output.

- [ ] **Step 3: Run the workflow pin and Windows contract tests**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.worktrees/repository-stabilization-20260911/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run pytest -p no:tach tests/test_platforms.py tests/test_ci_workflow.py -q
```

Expected: all selected tests pass, including the scan of every setup-uv action reference.

- [ ] **Step 4: Run the security linter against the workflow**

Run:

```bash
uvx zizmor --min-severity medium .github/workflows/issue-371-diagnostic.yml
```

Expected: exit 0 with no medium-or-higher findings.

- [ ] **Step 5: Run pre-commit and commit the workflow**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.worktrees/repository-stabilization-20260911/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run pre-commit run --all-files --show-diff-on-failure
git add .github/workflows/issue-371-diagnostic.yml
git commit -m "ci: isolate issue 371 Windows harness"
```

Expected: every pre-commit hook passes and the commit records only the temporary workflow.

### Task 2: Publish the experiment and capture the Windows result

**Files:**
- Modify: no repository files

**Interfaces:**
- Consumes: the committed `windows-harness-diagnostic` job from Task 1
- Produces: a draft pull request plus a durable Actions run URL and complete diagnostic log

- [ ] **Step 1: Push the diagnostic branch**

Run:

```bash
git push --set-upstream origin fix/371-windows-node-harness-exit
```

Expected: the remote branch points at the diagnostic workflow commit.

- [ ] **Step 2: Open a draft pull request**

Run:

```bash
gh pr create --draft --base main --head fix/371-windows-node-harness-exit \
  --title "diagnostic: isolate #371 on Windows" \
  --body $'Diagnostic-only experiment for #371.\n\nRuns only `tests/test_docs_landing_behavior.py::test_harness_nonzero_exit_preserves_captured_output` in 300 independent pytest processes on `windows-latest`, stopping on the first failure. It does not retry, skip, weaken assertions, or extend the harness timeout. The temporary workflow will be removed after evidence is captured and must not be merged.'
```

Expected: GitHub returns the URL of a new draft pull request targeting `main`.

- [ ] **Step 3: Resolve and watch the diagnostic run**

Run:

```bash
gh run list --branch fix/371-windows-node-harness-exit \
  --workflow issue-371-diagnostic.yml --event pull_request --limit 1 \
  --json databaseId,status,conclusion,url,headSha
```

After confirming the listed run's `headSha` is the pushed commit, assign its
`databaseId` and watch it:

```bash
ISSUE_371_RUN_ID=$(gh run list --branch fix/371-windows-node-harness-exit \
  --workflow issue-371-diagnostic.yml --event pull_request --limit 1 \
  --json databaseId --jq '.[0].databaseId')
: "${ISSUE_371_RUN_ID:?issue 371 diagnostic run not found}"
gh run watch "$ISSUE_371_RUN_ID" --exit-status
```

Expected: the watch ends when the job succeeds, fails on the first test failure, or reaches the 20-minute outer timeout.

- [ ] **Step 4: Capture the result and full log**

Run:

```bash
gh run view "$ISSUE_371_RUN_ID" --json databaseId,headSha,status,conclusion,url,jobs
gh run view "$ISSUE_371_RUN_ID" --log
```

Expected on success: the log ends with `All 300 isolated trials passed.` Expected on failure: the log contains `trial N/300 failed`, followed by the existing pytest harness timeout and captured lifecycle diagnostics.

- [ ] **Step 5: Capture the ordinary CI result from the same head SHA**

Run:

```bash
gh run list --branch fix/371-windows-node-harness-exit \
  --workflow ci.yml --event pull_request --limit 1 \
  --json databaseId,status,conclusion,url,headSha
ISSUE_371_CI_RUN_ID=$(gh run list --branch fix/371-windows-node-harness-exit \
  --workflow ci.yml --event pull_request --limit 1 \
  --json databaseId --jq '.[0].databaseId')
: "${ISSUE_371_CI_RUN_ID:?issue 371 ordinary CI run not found}"
gh run watch "$ISSUE_371_CI_RUN_ID" --exit-status
gh run view "$ISSUE_371_CI_RUN_ID" --json headSha,status,conclusion,url,jobs
```

Expected: the listed `headSha` equals the diagnostic run's head SHA. Record the
`windows-test` job's conclusion; if it fails in the target harness, capture that
job log with `gh run view "$ISSUE_371_CI_RUN_ID" --log-failed`.

### Task 3: Remove the temporary workflow and interpret the evidence

**Files:**
- Delete: `.github/workflows/issue-371-diagnostic.yml`

**Interfaces:**
- Consumes: the run URL, head SHA, conclusion, completed-trial count, and first-failure diagnostics from Task 2
- Produces: a branch that cannot merge the diagnostic workflow and a bounded evidence statement for selecting the next #371 experiment or fix

- [ ] **Step 1: Delete only the temporary workflow**

Apply this exact patch:

```diff
*** Delete File: .github/workflows/issue-371-diagnostic.yml
```

- [ ] **Step 2: Verify the diagnostic workflow is absent and the lockfile is unchanged**

Run:

```bash
test ! -e .github/workflows/issue-371-diagnostic.yml
git diff origin/main -- uv.lock
git status --short
```

Expected: the first command exits 0, the lockfile diff is empty, and status shows only the workflow deletion before committing.

- [ ] **Step 3: Commit and push the cleanup**

Run:

```bash
git add .github/workflows/issue-371-diagnostic.yml
git commit -m "ci: remove issue 371 diagnostic workflow"
git push
```

Expected: the temporary workflow disappears from the pull-request diff while its completed Actions run remains linked to the earlier commit.

- [ ] **Step 4: Classify the evidence without overstating it**

Use exactly one conclusion:

- Any trial failed: report the first failing trial and last captured lifecycle milestone; treat clean-process instability as reproduced and choose the next process-boundary experiment from that evidence.
- All 300 trials passed while ordinary Windows CI failed at the same head SHA: report that full-suite state is required by the paired observation and design a permanent isolation fix.
- Both targeted and ordinary Windows jobs passed: record 300 clean-process passes as negative evidence only; do not close #371 or claim isolation is proven.

In all cases, report the Actions run URL and exact head SHA to the maintainer. Do not merge, auto-merge, or approve the draft pull request.
