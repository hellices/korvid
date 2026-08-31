# Task 1 Report — Private-key structural redaction

## Files changed
- `src/korvid/core/redaction.py`
- `tests/core/test_redaction.py`
- `tests/tools/test_executor_security.py`
- `tests/mcp/test_server.py`

## RED evidence
Command:
```bash
cd /Users/hwang-inhwan/workspace/kube/.worktrees/fix-331-private-key-redaction && PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m pytest -p no:tach tests/core/test_redaction.py tests/tools/test_executor_security.py tests/mcp/test_server.py -k 'private_key_names or public_and_generic_key_names or private_key_fields or private_key_fields_before_bounding or mcp_resource_results_mask_private_key_fields' -q
```

Result:
- `8 failed, 5 passed, 195 deselected`
- Expected failures:
  - `denotes_secret()` returned `False` for `privateKey`, `private_key`, `client-private-key`, `client-key-data`, and `clientKeyData`
  - `privateKey` and `client-key-data` stayed unmasked in the executor and MCP regression tests

## GREEN evidence
Command:
```bash
cd /Users/hwang-inhwan/workspace/kube/.worktrees/fix-331-private-key-redaction && PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m pytest -p no:tach tests/core/test_redaction.py tests/tools/test_executor_security.py tests/mcp/test_server.py -k 'private_key_names or public_and_generic_key_names or private_key_fields or private_key_fields_before_bounding or mcp_resource_results_mask_private_key_fields' -q
```

Result:
- `13 passed, 195 deselected`

## Static checks
Commands:
```bash
cd /Users/hwang-inhwan/workspace/kube/.worktrees/fix-331-private-key-redaction && /Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check src/korvid/core/redaction.py tests/core/test_redaction.py tests/tools/test_executor_security.py tests/mcp/test_server.py
cd /Users/hwang-inhwan/workspace/kube/.worktrees/fix-331-private-key-redaction && /Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check src/korvid/core/redaction.py tests/core/test_redaction.py tests/tools/test_executor_security.py tests/mcp/test_server.py
```

Result:
- Ruff check: `All checks passed!`
- Ruff format: `4 files already formatted`

## Commit
- Code commit: `a3f203f8` (`fix: redact private-key fields`)

## Concerns
- None.
