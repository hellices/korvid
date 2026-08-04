# Task 5 Report

## Status

Done for Task 5 scope: added `docs/windows.md`, linked it from `README.md`,
updated the crash-restart wording in `docs/ops.md`, and added structural tests
that pin the Windows support doc and README link.

## TDD Evidence

1. Added README/doc invariant tests in `tests/test_platforms.py`.
2. Ran `uv run pytest -p no:tach tests/test_platforms.py -q` before adding the
   docs; it failed because `docs/windows.md` did not exist and the README link
   was missing.
3. Added `docs/windows.md`, the README link, and the matching `docs/ops.md`
   wording update.
4. Reran `uv run pytest -p no:tach tests/test_platforms.py -q` and got
   `12 passed`.

## Verification

- `uv run pytest -p no:tach tests/test_platforms.py -q` → `12 passed`
- `uv run ruff check src/ tests/` → passed
- `uv run mypy` → `Success: no issues found in 287 source files`
- `uv run tach check` → `✅ All modules validated!`
- `uvx zizmor --min-severity medium .github/workflows/` → no findings
- `make check` → `3391 passed, 21 skipped` + `tach check` passed
- `git diff --check` → passed

## Self-review

- Confirmed `docs/windows.md` documents the exact proving run
  (`30930727214`, `3373 passed / 37 skipped / 0 failures`) instead of guessing.
- Confirmed the skip inventory is explicit: 21 opt-in contract-suite skips and
  16 Windows capability skips, including the 2 POSIX `~user` cases.
- Confirmed the doc states the real NTFS boundary: `0o600` is requested and
  atomicity/durability are tested, but no ACL confidentiality claim is made.
- Confirmed the doc captures the exact LF (`newline=""`) and ASCII
  (`->`, `--`) fixes plus cross-platform `op_factory` cancellation safety.

## Concerns

- No fresh Windows rerun was triggered from this worktree; the documentation is
  anchored to the already-proven CI run required by the brief.
