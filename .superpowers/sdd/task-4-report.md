# Task 4 Report

## Status
DONE

## Summary
Added provider-boundary regression tests for sanitized canonical payloads crossing into the real OpenAI-compatible and Ollama adapters, plus an optional-extra import probe for `korvid.agent.outbound`.

## What was verified
- OpenAI-compatible requests keep `messages`/`tools` exactly equal to the prepared snapshot payload.
- Ollama request conversion adds only the expected adapter fields (`tool_name`, parsed tool arguments, optional `thinking`) while preserving sanitized content.
- Transport credentials stay out of the snapshot payload.
- Base imports still avoid optional `[mcp]` / `[agent]` dependencies, including `korvid.agent.outbound`.

## Tests run
- `uv run pytest -p no:tach tests/providers/test_openai_compat.py tests/providers/test_ollama.py tests/test_optional_extras.py -q`
- Result: 45 passed
- `uv run ruff check --fix tests/providers/test_openai_compat.py tests/providers/test_ollama.py tests/test_optional_extras.py`
- `uv run ruff format tests/providers/test_openai_compat.py tests/providers/test_ollama.py tests/test_optional_extras.py`

## Notes / concerns
- No production code changes were required; the existing adapters already satisfied the expected behavior.
- The new tests act as characterization/guardrails against regressions in canonical→wire conversion and optional-extra boundaries.

## Verification update
- `uv run pytest -p no:tach tests/providers/test_openai_compat.py tests/providers/test_ollama.py tests/test_optional_extras.py -q`
  - `46 passed in 4.71s`
- `uv run ruff check --fix tests/providers/test_openai_compat.py tests/providers/test_ollama.py tests/test_optional_extras.py`
  - `All checks passed!`
- `uv run ruff format tests/providers/test_openai_compat.py tests/providers/test_ollama.py tests/test_optional_extras.py`
  - `3 files left unchanged`
- Test-boundary commit: `e8c2e12af7e8181e2765edc89d82ec31ada0c1a6`
