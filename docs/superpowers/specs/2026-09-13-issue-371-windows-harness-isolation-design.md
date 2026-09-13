# Issue 371 Windows Harness Isolation Design

**Date:** 2026-09-13
**Issue:** [#371](https://github.com/hellices/korvid/issues/371)

## Goal

Keep the Windows documentation-harness regression fully enforced while
removing the long-lived shared pytest process that is necessary for its
intermittent timeout.

## Evidence

Two Windows jobs ran against commit
`ede0ed45a75e7fd5b4606ee124bc5abad8893ef6`:

- The [isolated diagnostic run](https://github.com/hellices/korvid/actions/runs/34737636730)
  passed 300 consecutive trials. Every trial launched a fresh pytest process
  containing only
  `test_harness_nonzero_exit_preserves_captured_output`.
- The [ordinary Windows CI job](https://github.com/hellices/korvid/actions/runs/34737636646/job/103671756336)
  failed the related
  `test_harness_true_hang_preserves_bounded_file_diagnostics` case after about
  17 minutes in a shared pytest process. At that point 11,041 tests had
  passed. A post-timeout Node probe still succeeded.

The same source, runner image, Node version, and Python version therefore
behaved differently across process boundaries. This establishes the
long-running shared pytest process as a necessary observed condition. It does
not identify a defective JavaScript statement or justify weakening the
harness assertions.

## Selected approach

The existing `windows-test` job runs
`tests/test_docs_landing_behavior.py` once, immediately after environment
setup and seed reporting, in its own pytest process. The later Windows suite
adds `--ignore=tests/test_docs_landing_behavior.py`, alongside its existing
native-terminal exclusion and audit deselection.

Both commands retain the explicit seed derived from `github.run_id`. The
dedicated command remains conditional on code changes, just like the full
Windows suite, and runs before native-terminal smoke coverage. A failure in
the dedicated command fails the job normally. There is no retry,
`continue-on-error`, timeout increase, assertion change, skip, or deselection
of a documentation-harness test.

This is process isolation, not test omission: every test in the file runs
exactly once on Windows.

## Alternatives

### Separate Windows job

A separate job would provide even stronger process and machine isolation, but
it would add runner startup, dependency installation, required-check surface,
and cache contention. The experiment only establishes a pytest-process
boundary as necessary, so a second machine is not warranted.

### Rewrite the Node lifecycle boundary

Changing subprocess termination, streams, or the JavaScript harness would be
speculative. The 300 clean isolated trials and successful post-timeout probes
do not identify a faulty lifecycle operation. Such a rewrite could hide the
symptom while introducing platform-specific behavior.

### Retry, skip, or extend the timeout

These choices would turn a correctness signal into a probabilistic pass or
make a genuinely hung subprocess consume more CI time. They are explicitly
out of scope.

## Workflow contract

`tests/test_platforms.py` pins the isolation policy structurally:

1. A dedicated step runs exactly
   `tests/test_docs_landing_behavior.py` with `-p no:tach`, `-q`, and the
   explicit Windows seed.
2. The dedicated step precedes the long Windows suite.
3. The long suite ignores exactly the already-executed documentation-harness
   file in addition to its existing exclusions.
4. Both steps use the same code-change condition and neither permits failure.

The contract test is written and observed failing before `.github/workflows/ci.yml`
is changed.

## Verification

Run the targeted workflow contract test through the shared uv environment,
then the complete repository gate with `make check`. After pushing, require
the Windows job and every other required pull-request check to succeed. Review
comments are handled through the repository's normal review loop.

No dependency, lockfile, product code, harness deadline, or harness assertion
changes are part of this fix.
