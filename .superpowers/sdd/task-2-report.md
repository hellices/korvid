# Task 2 Report

## Status

Done for Task 2 scope: applied the shared `posix_only()` marker only to the two confirmed POSIX `~user` expansion tests and added a structural invariant test that rejects ad-hoc marker drift for those cases.

## TDD Evidence

1. Added `test_posix_user_tilde_cases_use_the_shared_marker` in `tests/test_platforms.py`.
2. Ran `uv run pytest -p no:tach tests/test_platforms.py::test_posix_user_tilde_cases_use_the_shared_marker -q` and got 2 expected failures because neither target test had the shared marker yet.
3. Added `@posix_only("requires POSIX ~user account expansion behavior")` to:
   - `tests/core/test_transfer.py::TestValidateSpec::test_unknown_user_tilde_is_a_validation_error`
   - `tests/ui/test_transfer_picker.py::TestBrowseGating::test_unexpandable_tilde_falls_back_to_home`
4. Reran `uv run pytest -p no:tach tests/test_platforms.py::test_posix_user_tilde_cases_use_the_shared_marker tests/core/test_transfer.py::TestValidateSpec::test_unknown_user_tilde_is_a_validation_error tests/ui/test_transfer_picker.py::TestBrowseGating::test_unexpandable_tilde_falls_back_to_home -q` and got `4 passed`.

## Verification

- `uv run pytest -p no:tach tests/test_platforms.py tests/core/test_transfer.py::TestValidateSpec::test_unknown_user_tilde_is_a_validation_error tests/ui/test_transfer_picker.py::TestBrowseGating::test_unexpandable_tilde_falls_back_to_home -q`
- `uv run ruff check tests/test_platforms.py tests/core/test_transfer.py tests/ui/test_transfer_picker.py`
- `uv run ruff format --check tests/test_platforms.py tests/core/test_transfer.py tests/ui/test_transfer_picker.py`
- `uv run mypy tests/test_platforms.py tests/core/test_transfer.py tests/ui/test_transfer_picker.py`
- `git diff --check`

## Self-review

- Confirmed the shared helper remains `tests.platforms.posix_only`; no new ad-hoc `pytest.mark.skipif(...)` was introduced for these cases.
- Confirmed the capability reason is the same precise `~user`-expansion reason in both affected tests.
- Confirmed Task 2 does not mark `/tmp` path failures, permission/fsync tests, Unicode console failures, picker separators, proposal timing, or mocked remote-path cases.

## Concerns

- I did not run a Windows job from this worktree, so the expected outcome is based on the authoritative log classification plus the new invariant coverage rather than a fresh remote Windows rerun.
