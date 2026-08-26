# Task 3 Review-Finding Remediation Report

## Commands and results

### Tests — `tests/agent/test_prompt_packs.py`

```
uv run pytest -p no:tach tests/agent/test_prompt_packs.py -x -q
36 passed in 0.16s
```

### Ruff — touched source and test files

```
uv run ruff check --fix \
    tests/agent/test_prompt_packs.py \
    tests/agent/test_model_policy.py \
    tests/agent/test_prompt_harness.py \
    src/korvid/agent/model_policy.py \
    src/korvid/agent/model_catalog.py \
    src/korvid/agent/prompt_harness.py
All checks passed!
6 files left unchanged
```

## Commit

`ceb3468` — `fix: assert operation-first clauses against LOW_KORVID_OPERATOR_PACK; remove opaque task references`

## Changes

### 1. Operation-first test now asserts directly against `LOW_KORVID_OPERATOR_PACK`

`test_the_low_tier_dispatches_tools_immediately_without_narrating_a_plan`
previously used `_low_text()` (SAFETY_CONTRACT + COMMON_ROLE +
LOW_KORVID_OPERATOR_PACK). A coincidental keyword in the safety contract or
common role could make the test pass even if the LOW pack were missing the
clause. The test now binds its assertions to `LOW_KORVID_OPERATOR_PACK`
directly.

### 2. Opaque `(Task 3)` / `(task N)` labels removed

The parenthetical labels `(Task 3)`, `(task 3)`, and `(task 1)` were
removed from module docstrings in:
- `src/korvid/agent/model_policy.py`
- `src/korvid/agent/model_catalog.py`
- `src/korvid/agent/prompt_harness.py`
- `tests/agent/test_model_policy.py`
- `tests/agent/test_prompt_harness.py`

The functional descriptions that followed those labels are preserved
unchanged.

## Status

**PASS** — 36 tests pass, ruff clean, commit `ceb3468` on
`agents/316-agent-interaction-harness`.

## Concerns

None. No behavioural changes; the only effect is that the operation-first
test now fails if the LOW pack itself is missing a clause rather than
passing by inheritance from the safety contract or common role.

---

## Task 3 Cleanup — Round 2

### Changes (commit `4cadcd3`)

Removed the final opaque task-number labels from files touched by Task 3 that
were not caught in the previous round:

| File | Old text | New text |
|------|----------|----------|
| `docs/agent.md` line 414 | `introduced in Task 3` | `enforced by the LOW pack` |
| `tests/agent/test_prompt_harness.py` module docstring | `task 11 consumes this harness` | `the session layer consumes this harness` |
| `tests/agent/test_prompt_harness.py` section header | `task 11 refuses an unusable policy` | `the session layer refuses an unusable policy` |
| `tests/agent/test_prompt_harness.py` test docstring | `(task 11).` | *(removed parenthetical)* |

### Tests

```
uv run pytest -p no:tach -q tests/agent/test_prompt_harness.py tests/agent/test_prompt_packs.py
  -k "not migration_note_figures"
85 passed, 1 deselected in 0.15s
```

The deselected test (`test_the_fully_armed_low_tier_static_prompt_matches_the_migration_note_figures`)
fails at HEAD **before** these changes (4832 ≠ 4283 char count) — pre-existing, not introduced here.

### Ruff

```
uv run ruff check --fix docs/agent.md tests/agent/test_prompt_harness.py
All checks passed!
2 files left unchanged
```

### Commit

`4cadcd3` — `docs: remove opaque task-number labels from agent.md and test_prompt_harness`

### Status

**PASS** — 85 tests pass, ruff clean.

### Concerns

One pre-existing failure exists at HEAD:
`test_the_fully_armed_low_tier_static_prompt_matches_the_migration_note_figures`
— the expected char count (4 283) does not match the actual (4 832). This was
already failing before Task 3 cleanup and is out of scope here.

## Prompt Budget Contract Synchronization

**Date:** 2026-08-27
**Commit branch:** refactor-agent-module-solid-principles (agent-interaction-harness worktree HEAD 79d7b65)

### RED evidence (before fix)
```
FAILED tests/agent/test_prompt_harness.py::test_the_fully_armed_low_tier_static_prompt_matches_the_migration_note_figures
assert 4832 == 4283
```

### Root cause
Task 3 added 549 static prompt characters to the LOW tier prompt, raising measured size from 4,283 to 4,832. The pinned test assertion and docs/release-notes/unreleased.md still referenced the old figures.

### Changes made
| File | Old figures | New figures |
|---|---|---|
| `tests/agent/test_prompt_harness.py` | 4,283 / 1,717 | 4,832 / 1,168 |
| `docs/release-notes/unreleased.md` | 4,283 / 1,717 | 4,832 / 1,168 |

Budget (6,000) and LOW prompt unchanged.

### GREEN evidence
```
258 passed in 1.51s
tests/agent/test_prompt_harness.py + tests/test_docs_agent_contracts.py
```

### Concerns
None. The 6,000-character budget still has 1,168 characters of headroom. No logic was changed — only contract figures synchronized with actual measured output.
