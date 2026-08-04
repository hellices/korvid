# Task 1 Report

## Status

Done for Task 1 scope: added shared Windows/POSIX test helpers, added RED/GREEN coverage in `tests/test_platforms.py`, and added a required `windows-test` CI job without changing the existing Linux coverage job.

## TDD Evidence

1. `uv run pytest -p no:tach tests/test_platforms.py -q` failed during collection with `ModuleNotFoundError: No module named 'tests.platforms'`.
2. Added `tests/platforms.py`; reran the same test file and got `2 passed`.
3. Added a workflow invariant test; `uv run pytest -p no:tach tests/test_platforms.py::test_ci_workflow_defines_the_required_windows_test_job -q` failed with `ValueError: substring not found` for `windows-test`.
4. Added the `windows-test` workflow job; reran `uv run pytest -p no:tach tests/test_platforms.py -q` and got `3 passed`.

## Verification

- `uv run pytest -p no:tach tests/test_platforms.py -q`
- `uv run ruff check tests/test_platforms.py tests/platforms.py`
- `uv run ruff format --check tests/test_platforms.py tests/platforms.py`
- `uvx zizmor --min-severity medium .github/workflows/ci.yml`
- `git diff --check`

## Self-review

- Confirmed `tests/platforms.py` stays test-only and does not affect product code.
- Confirmed `tests/test_platforms.py` pins `WINDOWS`, `POSIX`, and `posix_only(reason)` marker shape plus the `windows-test` workflow invariant.
- Confirmed `.github/workflows/ci.yml` adds only a new Windows job and leaves the Linux `test` job's coverage command unchanged.

## Concerns

- This task intentionally does **not** mark existing POSIX-only tests yet; that remains for Tasks 2-4.
- I did not capture exact failing Windows node IDs because the current instructions forbid pushing/opening a PR and no Windows runner was executed from this worktree.

## Review Follow-up: SHA-pinning invariant maintenance fix

- Replaced literal action SHA assertions in `tests/test_platforms.py` with a structural helper that requires `actions/checkout@` and `astral-sh/setup-uv@` to use lowercase 40-hex commit SHAs, not tags.
- Added helper coverage for accepted SHA-pinned refs, rejected tag/non-lowercase refs, and UTF-8 file reading.
- Updated the CI workflow reader to use `read_text(encoding="utf-8")`.

### Review-fix TDD Evidence

1. `uv run pytest -p no:tach tests/test_platforms.py -q` failed during collection with `ImportError: cannot import name 'assert_pinned_action_ref' from 'tests.platforms'`.
2. Added `assert_pinned_action_ref()` and `read_text_utf8()` to `tests/platforms.py`.
3. Reran `uv run pytest -p no:tach tests/test_platforms.py -q` and got `8 passed`.

### Review-fix Verification

- `uv run pytest -p no:tach tests/test_platforms.py -q`
- `uv run ruff check tests/test_platforms.py tests/platforms.py`
- `uv run ruff format --check tests/test_platforms.py tests/platforms.py`
- `uvx zizmor --min-severity medium .github/workflows/ci.yml`
