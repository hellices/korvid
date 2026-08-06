# Task 4 Report

## RED evidence
- `uv run pytest -p no:tach tests/agent/test_runtime.py -k 'test_over_ceiling_request_drops_the_oldest_turn_and_still_reaches_the_model or test_estimated_prompt_cost_reflects_the_history_actually_sent'`
  - Result: `1 failed, 1 passed, 159 deselected`
  - Failure: `IndexError: list index out of range` in `test_estimated_prompt_cost_reflects_the_history_actually_sent` because the first 6,000-char prompt was blocked before a second provider call existed.
- Root cause: the test still used `max_request_chars=12_000`, which was now too small once the compact `READ_TOOLS` schema grew.

## GREEN evidence
- Added `_read_tools_request_ceiling(non_tool_request_budget)` so both 6,000+6,000 trimming tests derive the ceiling from compact `READ_TOOLS` JSON plus a non-tool budget.
- Switched both tests to `_read_tools_request_ceiling(10_000)`, which leaves headroom for one prompt but still forces the second request to trim history.
- In the estimated-cost test, kept both collected event lists and asserted neither turn emitted `AgentError` before indexing `provider.calls[1]`.

## Exact commands and results
- `uv run pytest -p no:tach tests/agent/test_runtime.py -k 'test_over_ceiling_request_drops_the_oldest_turn_and_still_reaches_the_model or test_estimated_prompt_cost_reflects_the_history_actually_sent'`
  - Result: `2 passed, 159 deselected`
- `uv run ruff check tests/agent/test_runtime.py && uv run ruff format --check tests/agent/test_runtime.py`
  - Result: `All checks passed!` / `1 file already formatted`
- `git commit -m "fix: harden runtime request-ceiling tests" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`
  - Result: commit `261448e`

## Self-review
- Change is limited to `tests/agent/test_runtime.py` and this report.
- The ceiling is now derived in one helper instead of duplicated local calculations.
- No production code or tool surface was changed.
- The two targeted tests and Ruff checks passed.
