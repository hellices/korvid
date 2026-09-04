# Task 1 Report — Shared Built-in Stream Limits

## Outcome

Implemented the shared provider stream limit helpers and moved `ProviderError`
to a dedicated module while keeping `korvid.providers.openai_compat.ProviderError`
import-compatible.

## Changes

- Added `src/korvid/providers/errors.py` with `ProviderError`.
- Added `src/korvid/providers/stream_limits.py` with:
  - `MAX_TOOL_CALLS`
  - `MAX_TOOL_ARGUMENTS_BYTES`
  - `MAX_REASONING_BYTES`
  - `MAX_PROBE_TEXT_BYTES`
  - `append_bounded(...)`
  - `require_count(...)`
- Updated `src/korvid/providers/openai_compat.py` to re-export `ProviderError`
  from `korvid.providers.errors`.
- Added `tests/providers/test_stream_limits.py`.

## TDD Evidence

### RED

Command:

```bash
cd /Users/hwang-inhwan/workspace/kube/.worktrees/security-336-provider-stream-safety
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_stream_limits.py -q
```

Result:

```text
ModuleNotFoundError: No module named 'korvid.providers.errors'
```

### GREEN

Command:

```bash
cd /Users/hwang-inhwan/workspace/kube/.worktrees/security-336-provider-stream-safety
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_stream_limits.py -q
```

Result:

```text
5 passed
```

## Quality Checks

- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check src/korvid/providers/errors.py src/korvid/providers/stream_limits.py src/korvid/providers/openai_compat.py tests/providers/test_stream_limits.py`
  - passed
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy src/korvid/providers/errors.py src/korvid/providers/stream_limits.py src/korvid/providers/openai_compat.py`
  - passed

## Self-Review

- No placeholders or TODOs remain.
- `ProviderError` remains importable from `korvid.providers.openai_compat`.
- Byte-budget checks use UTF-8 byte counts and do not include accumulated
  provider content in the exception message.
- The new helpers are isolated to provider stream safety and do not change
  unrelated provider behavior.
