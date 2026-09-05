# Task 4 Report

## Status
Done. Provider probe overflow is now bounded with `append_bounded(...)`, and the provider is always closed in the existing `try/finally`.

## Commit
`fad87eeb`

## Tests
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_configurator.py -q -k cumulative_utf8_overflow` (RED observed, then fixed)
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_stream_limits.py tests/providers/test_openai_compat.py tests/providers/test_ollama.py tests/providers/test_configurator.py -q`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check src/korvid/providers/configurator.py tests/providers/test_configurator.py`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy src/korvid/providers/configurator.py`

## Concerns
- Full repository gate and independent review were intentionally skipped per instructions.
- No `uv sync`, lockfile, push, or PR actions were performed.

---

## 2026-09-05 Focused TDD Wave

### Status
- Replaced repeated whole-string UTF-8 re-encoding in provider accumulation paths with `BoundedTextAccumulator`.
- Kept exact byte-limit/count error text and preserved the accepted Ollama terminal-chunk behavior (`done: true` stops before processing the terminal message).
- Made `korvid.providers.openai_compat.ProviderError` an explicit re-export for strict mypy.
- Removed `raising=False` from provider limit monkeypatches.

### TDD Evidence
- RED: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_stream_limits.py -q -k counts_only_new_fragments`
  - Failed during collection with `ImportError: cannot import name 'BoundedTextAccumulator'`.
- RED: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy tests/providers/test_ollama.py`
  - Failed with `Module "korvid.providers.openai_compat" does not explicitly export attribute "ProviderError"`.
- GREEN: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_stream_limits.py -q -k counts_only_new_fragments`
  - Passed.
- GREEN: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy tests/providers/test_ollama.py`
  - Passed.

### Checks
- `/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check --fix src/korvid/providers/stream_limits.py src/korvid/providers/openai_compat.py src/korvid/providers/ollama.py src/korvid/providers/configurator.py tests/providers/test_stream_limits.py tests/providers/test_openai_compat.py tests/providers/test_ollama.py tests/providers/test_configurator.py`
- `/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format src/korvid/providers/stream_limits.py src/korvid/providers/openai_compat.py src/korvid/providers/ollama.py src/korvid/providers/configurator.py tests/providers/test_stream_limits.py tests/providers/test_openai_compat.py tests/providers/test_ollama.py tests/providers/test_configurator.py`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers -q`
  - Result: `243 passed in 2.98s`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy src/korvid/providers/stream_limits.py src/korvid/providers/openai_compat.py src/korvid/providers/ollama.py src/korvid/providers/configurator.py tests/providers/test_stream_limits.py tests/providers/test_openai_compat.py tests/providers/test_ollama.py tests/providers/test_configurator.py`
  - Result: success, 8 files checked.
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy src tests`
  - Result: unrelated pre-existing failure in `src/korvid/tools/structured.py:145` (`no-untyped-call` on `dispose`).

### Concerns
- Full mypy still fails on an unrelated existing typed-context call outside the provider scope; this wave did not change that file.
- No push or PR action was performed.

---

## 2026-09-05 PR #363 Round 2

### Status
- Added strict TDD regressions for mid-JSON transport EOF in OpenAI SSE and Ollama NDJSON streams.
- Translated streamed JSON decode failures into typed, content-free `ProviderError` messages.
- Locked in the accepted Ollama stop-at-first-terminal contract with a regression that discards terminal-chunk `message` content/tool data while still emitting usage.
- Documented that Ollama's serialized-arguments cap is a post-parse rejection policy, unlike fragmented OpenAI arguments.

### TDD Evidence
- RED: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_openai_compat.py -q -k mid_json_sse_payload_raises_typed_provider_error`
  - Failed with `json.decoder.JSONDecodeError: Unterminated string...` from `src/korvid/providers/openai_compat.py:211`.
- RED: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_ollama.py -q -k 'mid_json_ndjson_chunk_raises_typed_provider_error or terminal_chunk_discards_message_content_and_tool_calls'`
  - Failed on `test_mid_json_ndjson_chunk_raises_typed_provider_error` with `json.decoder.JSONDecodeError: Unterminated string...` from `src/korvid/providers/ollama.py:214`.
  - Passed `test_terminal_chunk_discards_message_content_and_tool_calls`, confirming the existing stop-at-first-terminal behavior before production changes.
- GREEN: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_openai_compat.py -q -k mid_json_sse_payload_raises_typed_provider_error`
  - Result: `1 passed, 21 deselected`.
- GREEN: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_ollama.py -q -k 'mid_json_ndjson_chunk_raises_typed_provider_error or terminal_chunk_discards_message_content_and_tool_calls'`
  - Result: `2 passed, 38 deselected`.

### Checks
- `/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check --fix src/korvid/providers/openai_compat.py src/korvid/providers/ollama.py tests/providers/test_openai_compat.py tests/providers/test_ollama.py`
- `/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format src/korvid/providers/openai_compat.py src/korvid/providers/ollama.py tests/providers/test_openai_compat.py tests/providers/test_ollama.py`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers -q`
  - Result: `246 passed in 3.08s`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy src/korvid/providers/openai_compat.py src/korvid/providers/ollama.py tests/providers/test_openai_compat.py tests/providers/test_ollama.py`
  - Result: success, no issues found in 4 source files.

### Concerns
- Final verification covered the full provider test suite plus changed-file Ruff and mypy only, per instructions.
- No push or PR action was performed.

---

## 2026-09-05 PR #363 Round 3

### Status
- Replaced the invalid terminal-chunk regression with one that requires the final Ollama `done: true` chunk to emit its own content, tool call, usage, and remembered thinking exactly once.
- Added terminal-chunk limit regressions for hidden reasoning and serialized tool arguments so terminal payloads are bounded before success events can be emitted.
- Restructured `OllamaProvider.complete` to process each chunk's `message` once, capture usage from the terminal chunk afterward, and break before any later chunks are read.
- Updated the provider-stream-safety design and plan docs to describe preserving the terminal chunk payload instead of discarding it.

### TDD Evidence
- RED: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_ollama.py -q -k terminal_chunk_emits_message_content_and_tool_calls_before_done`
  - Failed because the provider emitted `usage` immediately after `REQUEST_SENT`, proving the terminal chunk payload was still discarded.
- RED: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_ollama.py -q -k 'terminal_chunk_emits_message_content_and_tool_calls_before_done or terminal_chunk_reasoning_respects_byte_limit or terminal_chunk_tool_arguments_respect_byte_limit'`
  - Failed three ways: missing terminal text/tool-call emission, missing terminal reasoning overflow, and missing terminal tool-argument overflow.
- GREEN: `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_ollama.py -q -k 'terminal_chunk_emits_message_content_and_tool_calls_before_done or terminal_chunk_reasoning_respects_byte_limit or terminal_chunk_tool_arguments_respect_byte_limit'`
  - Result: `3 passed, 39 deselected`.

### Checks
- `/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check --fix src/korvid/providers/ollama.py tests/providers/test_ollama.py`
- `/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format src/korvid/providers/ollama.py tests/providers/test_ollama.py`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers -q`
  - Result: `248 passed in 3.64s`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy src/korvid/providers/ollama.py tests/providers/test_ollama.py`
  - Result: success, no issues found in 2 source files.

### Concerns
- Verification stayed scoped to provider tests plus changed-file Ruff/format and mypy, per instructions.
- No push or PR action was performed.

---

## 2026-09-05 PR #363 Round 4

### Status
- Added regressions for syntactically valid non-object JSON in OpenAI SSE and Ollama NDJSON streams.
- The stream decoders now validate that `json.loads(...)` returns a `dict` before continuing; non-object payloads now raise the same content-free invalid-payload `ProviderError` as decode failures.
- Both providers stop before any final `done` event when the payload is invalid.

### TDD Evidence
- RED: `uv run pytest -p no:tach tests/providers/test_openai_compat.py -q --maxfail=1`
  - Failed with `AttributeError: 'str' object has no attribute 'get'` from `src/korvid/providers/openai_compat.py:214` on `test_non_object_sse_payload_raises_typed_provider_error[oops]`.
- RED: `uv run pytest -p no:tach tests/providers/test_ollama.py -q --maxfail=1`
  - Failed with `AttributeError: 'str' object has no attribute 'get'` from `src/korvid/providers/ollama.py:218` on `test_non_object_ndjson_payload_raises_typed_provider_error[oops]`.
- GREEN: `uv run pytest -p no:tach tests/providers/test_openai_compat.py tests/providers/test_ollama.py -q`
  - Result: `70 passed in 0.48s`.

### Checks
- `uv run ruff check --fix src/korvid/providers/openai_compat.py src/korvid/providers/ollama.py tests/providers/test_openai_compat.py tests/providers/test_ollama.py`
- `uv run ruff format src/korvid/providers/openai_compat.py src/korvid/providers/ollama.py tests/providers/test_openai_compat.py tests/providers/test_ollama.py`
- `uv run mypy src/korvid/providers/openai_compat.py src/korvid/providers/ollama.py tests/providers/test_openai_compat.py tests/providers/test_ollama.py`

### Concerns
- Commit: `7128db66`
- No push or PR action was performed.

---

## 2026-09-05 PR #363 Round 5

### Status
- Hardened OpenAI tool-call fragment parsing so missing, string, and bool
  `index` values now raise the typed `ProviderError` contract instead of leaking
  `KeyError` or mixed-type sorting failures.
- Strengthened the OpenAI post-`[DONE]` regression to include a trailing tool
  call delta and confirm no ghost tool event is emitted.
- Added the provider-stream-safety design non-goal that individual transport
  line size before `aiter_lines()` is out of scope for #336.

### TDD Evidence
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest -p no:tach tests/providers/test_openai_compat.py tests/providers/test_ollama.py -q`
  - Result: `73 passed in 0.68s`.

### Checks
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check --fix src/korvid/providers/openai_compat.py tests/providers/test_openai_compat.py`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format src/korvid/providers/openai_compat.py tests/providers/test_openai_compat.py`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check src/korvid/providers/openai_compat.py tests/providers/test_openai_compat.py src/korvid/providers/ollama.py tests/providers/test_ollama.py`
  - Result: `All checks passed!`
- `PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy src/korvid/providers/openai_compat.py tests/providers/test_openai_compat.py src/korvid/providers/ollama.py tests/providers/test_ollama.py`
  - Result: `Success: no issues found in 4 source files`

### Concerns
- Commit: `c1753749`
- No push or PR action was performed.
