# Task 1 Report — Core bounded timeline model and config

## Files changed
- `src/korvid/core/session_timeline.py` (new)
- `src/korvid/core/config.py`
- `tests/core/test_session_timeline.py` (new)
- `tests/core/test_config.py`

## RED evidence
- Added failing tests first in `tests/core/test_session_timeline.py` and `tests/core/test_config.py`.
- RED command:

```bash
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/.copilot/session-state/ad4ab209-0f8a-4406-81cd-3034704d187b/files/venv-281-linked \
UV_NO_SYNC=1 \
PYTHONPATH=src \
uv run pytest -p no:tach tests/core/test_session_timeline.py tests/core/test_config.py -q
```

- RED result:

```text
ERROR collecting tests/core/test_session_timeline.py
ModuleNotFoundError: No module named 'korvid.core.session_timeline'
```

This was the expected pre-implementation failure proving the new model surface did not exist yet.

## GREEN implementation summary
- Added the pure `SessionTimeline` ring-buffer model with bounded entry-count and encoded-byte eviction/refusal behavior.
- Added typed timeline payload/resource/snapshot/result dataclasses and `TimelineSource` enum.
- Reused config positive-int parsing by renaming `_observability_int` to `_mapping_positive_int` and using it for the new nested `timeline.max_entries` / `timeline.max_bytes` settings.
- Normalized Warning event text through the existing redaction/control-character boundary before storage.
- Preserved the required resource matching rule where UID participates only when both sides carry one.
- Adjusted the oversized-warning RED/GREEN test input from the plan snippet so it still exercises refusal under the current encoded-size shape after normalization/redaction.

## GREEN commands and results

```bash
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/.copilot/session-state/ad4ab209-0f8a-4406-81cd-3034704d187b/files/venv-281-linked \
UV_NO_SYNC=1 \
PYTHONPATH=src \
uv run pytest -p no:tach tests/core/test_session_timeline.py tests/core/test_config.py -q

UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/.copilot/session-state/ad4ab209-0f8a-4406-81cd-3034704d187b/files/venv-281-linked \
UV_NO_SYNC=1 \
PYTHONPATH=src \
uv run ruff check --fix src/korvid/core/session_timeline.py src/korvid/core/config.py tests/core/test_session_timeline.py tests/core/test_config.py

UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/.copilot/session-state/ad4ab209-0f8a-4406-81cd-3034704d187b/files/venv-281-linked \
UV_NO_SYNC=1 \
PYTHONPATH=src \
uv run ruff format src/korvid/core/session_timeline.py src/korvid/core/config.py tests/core/test_session_timeline.py tests/core/test_config.py

UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/.copilot/session-state/ad4ab209-0f8a-4406-81cd-3034704d187b/files/venv-281-linked \
UV_NO_SYNC=1 \
PYTHONPATH=src \
uv run mypy src/korvid/core/session_timeline.py src/korvid/core/config.py
```

```text
pytest: 163 passed in 0.92s
ruff check --fix: All checks passed!
ruff format: 4 files left unchanged
mypy: Success: no issues found in 2 source files
```

## Commit SHA
- Implementation commit: `d068f8e` (`feat: add bounded session timeline core`)

## Self-review
- Stayed within Task 1 surfaces only: pure core model + config + targeted tests.
- Reused the existing config validation pattern instead of adding a duplicate parser.
- Kept the timeline implementation in `core/` with no UI/producer imports, preserving tach boundaries.
- Ensured strict typing in tests so repository pre-commit mypy remained green.
- Verified warning-note storage is normalized and redacted rather than truncated.

## Concerns
- None at handoff. The only noteworthy deviation was strengthening the oversized-warning test payload so the refusal assertion remained real after normalization/redaction; this preserves the task requirement rather than changing it.
