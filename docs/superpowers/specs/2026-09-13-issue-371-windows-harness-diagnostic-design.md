# Issue 371 Windows Harness Diagnostic Design

**Date:** 2026-09-13
**Issue:** [#371](https://github.com/hellices/korvid/issues/371)

## Goal

Determine whether the smallest failing documentation harness can reproduce its
intermittent timeout in a clean GitHub-hosted Windows process, without changing
the harness deadline or converting a failure into a retry.

The experiment answers one question: does
`test_harness_nonzero_exit_preserves_captured_output` fail when it is the only
test workload on `windows-latest`?

## Evidence and hypothesis

Recorded recurrences have stopped at different boundaries: before the first
JavaScript milestone, after every scene assertion, while CPython waited for
captured-output EOF, while CPython waited for the Node process handle, and
inside the synchronous `finish()` path. Some post-timeout Python and Node
controls succeeded immediately; in another recurrence every control timed out.
Unchanged-source Windows runs also passed.

That evidence does not identify one defective JavaScript statement. It supports
testing the narrower hypothesis that a clean Windows process is more stable and
that the long-running full-suite environment materially contributes.

## Experiment

Add a temporary pull-request workflow on the issue branch. Its only job runs on
`windows-latest` and performs the following steps:

1. Check out the pull-request source without persisted credentials.
2. Install the locked development environment with the repository's pinned uv
   setup action and `uv sync --locked --dev --all-extras`.
3. Report the resolved Windows image, Node executable, Node version, Python
   version, and uv version.
4. Launch 300 independent pytest processes. Each process runs only:

   ```text
   tests/test_docs_landing_behavior.py::test_harness_nonzero_exit_preserves_captured_output
   ```

5. Stop on the first non-zero exit. Preserve pytest's existing timeout error,
   bounded captured output, lifecycle milestones, process snapshot, and startup
   probes in the Actions log.

Each successful trial is another observation, not a retry of a failed trial. A
failed trial ends the job immediately. The harness retains its ten-second
deadline, and no assertion is skipped or weakened. The job has a 20-minute
outer bound so the diagnostic itself cannot consume a runner indefinitely.

The workflow is diagnostic-only. It is removed after evidence is captured and
is never proposed for merge as the permanent fix.

## Interpretation

- **Any trial fails:** the timeout is reproducible without the full suite. Use
  the captured last milestone to choose the next lifecycle or process-boundary
  experiment; do not claim that pytest-process isolation fixes the issue.
- **All 300 trials pass:** clean-process isolation is supported, but not proved,
  as the relevant boundary. Compare this result with the ordinary Windows CI
  job from the same pull-request SHA. If that job recurs, the paired result is
  stronger evidence that long-running full-suite state materially contributes;
  it is not proof that the state is necessary or the exclusive cause.
- **Both targeted and ordinary jobs pass:** record the clean 300-trial result
  as negative evidence. Do not close #371 from that result alone; use it to
  design the next discriminating experiment.

## Alternatives rejected

- A local Windows container cannot run on the current ARM macOS host because
  Windows containers require a compatible Windows kernel.
- An Azure Windows VM would add cost and a different image while being less
  faithful than the GitHub-hosted runner where the failures occurred.
- Re-running the full 20–30 minute Windows suite hundreds of times would be
  expensive and would not isolate whether the individual harness is unstable.
- Extending the timeout, retrying after failure, or omitting the Windows
  assertion would hide rather than diagnose the failure.

## Verification and safety

Before pushing, parse the workflow as YAML, run the repository's workflow
contract tests, and run pre-commit normally. The workflow receives only
`contents: read`, persists no checkout credential, uses pinned actions, and
does not read or print secrets. No dependency or lockfile change is permitted.
