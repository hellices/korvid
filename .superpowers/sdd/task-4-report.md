# Task 4 Report: Expose `diagnose_pvc` Tool

## Status
✅ Complete — all tests pass, all static checks pass.

## Commit
`687fe4d` — `feat: expose deterministic PVC diagnosis`

## Files Changed
| File | Change |
|------|--------|
| `src/korvid/tools/registry.py` | Added `diagnose_pvc` ToolDef (cluster_read, structured_yaml, `_ALL_SURFACES`) |
| `src/korvid/tools/executor.py` | Added imports from `pvc_analysis`; `StorageClass` to `_DIAGNOSE_BUILTIN_METAS`; renamed `_diagnose_pvc` → `_diagnose_pvc_related`; added `_pvc_binding_snapshot`, `_pvc_event_evidence`, `_pvc_storage_classes` helpers; added `_diagnose_pvc` dispatch method |
| `src/korvid/tools/follow.py` | Added `diagnose_pvc` to `FOLLOWABLE_TOOLS`; added `diagnose_pvc` branch in `_mirror_diagnose` → `agent_open_describe("persistentvolumeclaims", ...)` |
| `tests/tools/test_executor.py` | Added `PVCDiagnosisKube`, `_pvc_manifest`, `_storage_class`, `_pvc_executor` helpers; 8 new tests; updated `test_read_tools_schema_names` golden list |
| `tests/tools/test_registry.py` | Added `diagnose_pvc` to `_READ_ORDER`; added `test_registry_dispatches_diagnose_pvc`; updated `test_every_tool_declares_an_outbound_result_format` |
| `tests/tools/test_follow.py` | Added `test_diagnose_pvc_follow_opens_claim_describe`; updated `test_every_followable_tool_reaches_the_bridge` args dict |
| `docs/agent.md` | Added `diagnose_pvc` to compound-tool description and follow-mode list |
| `docs/mcp.md` | Added PVC binding diagnosis bullet to the result-format section |

## RED/GREEN Evidence

### RED phase
```
uv run pytest -p no:tach tests/tools/test_executor.py -k diagnose_pvc tests/tools/test_registry.py tests/tools/test_follow.py -q
→ 4 failed, 1 passed, 247 deselected
  FAILED test_every_followable_tool_reaches_the_bridge[diagnose_pvc]
  FAILED test_diagnose_pvc_follow_opens_claim_describe
  FAILED test_registry_dispatches_diagnose_pvc
  FAILED test_diagnose_pvc_is_shared_structured_read
```

### GREEN phase
```
uv run pytest -p no:tach tests/core/test_service_analysis.py tests/core/test_pvc_analysis.py tests/k8s/test_models.py tests/tools/test_list_resources.py tests/tools/test_executor.py tests/tools/test_registry.py tests/tools/test_follow.py -q
→ 393 passed in 1.16s
```

### Full gate (pre-commit)
```
uv run ruff check [...] → All checks passed!
uv run ruff format --check [...] → 14 files already formatted
uv run mypy src/ → Success: no issues found in 130 source files
uv run tach check → ✅ All modules validated!
pre-commit hooks → all passed, committed 687fe4d
```

## Implementation Notes
- Conditional I/O preserved exactly: Bound/Lost → 1 GET only; Pending → 1 GET + 1 events list + 1 StorageClass LIST (unless `storageClassName == ""`).
- `ApiStatusError` on events or StorageClass LIST becomes a `gaps[]` entry; `RuntimeError` (transport) remains a tool error (propagated to executor's catch-all).
- Event messages clamped with `_clamp_line` (240 chars) before creating `WarningEventSnapshot`; Warning-only filter applied.
- `_pvc_binding_snapshot`: `"storageClassName"` absent → `None`; `""` → `""` (explicit none, skips LIST).
- Follow: `diagnose_pvc` → `agent_open_describe("persistentvolumeclaims", pvc, namespace)`.
- One registry definition for both agent surfaces and MCP.
- `StorageClass` added to `_DIAGNOSE_BUILTIN_METAS` so the tool works without background discovery.

## Concerns
None — all requirements from the brief are satisfied and all tests pass.

---

## Task 4 Addendum: Small-profile `diagnose_pvc` trim fix

### Status
✅ Complete — the small profile now trims `diagnose_pvc` and the profile test file passes.

### Commit
`9bc0c09` — `fix: trim small pvc diagnosis`

### RED / GREEN Evidence
**RED (pre-fix, per the reported regression):**
- `tests/agent/test_profiles.py::test_small_profile_trims_verbose_descriptions` failed because `diagnose_pvc` was 419 chars in the full registry and had no small-profile override.

**GREEN:**
- `uv run pytest -p no:tach tests/agent/test_profiles.py::test_small_profile_trims_verbose_descriptions`
  - `1 passed`
- `uv run pytest -p no:tach tests/agent/test_profiles.py`
  - `19 passed`

### Commands / Results
- `uv run ruff check src/korvid/agent/prompts.py tests/agent/test_profiles.py`
  - `All checks passed!`
- `uv run ruff format --check src/korvid/agent/prompts.py tests/agent/test_profiles.py`
  - `2 files already formatted`

### Self-review
- Added only the concise small-profile override for `diagnose_pvc`.
- Strengthened the profile test to compare the small and full descriptions for both `diagnose_pod` and `diagnose_pvc`.
- Left the full and MCP registry descriptions unchanged.
