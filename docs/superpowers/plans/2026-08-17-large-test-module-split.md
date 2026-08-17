# Large Test Module Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 4,003-line executor tests and 3,827-line runtime tests into behavior-focused modules without changing assertions or collection semantics.

**Architecture:** Shared fakes move to ordinary support modules, not pytest `conftest` files. Executor and runtime tests each split into four modules; no README, Makefile, CI, or product-code change is part of this reorganization.

**Tech Stack:** Python 3.13, pytest, pytest-randomly, ruff, mypy.

## Global Constraints

- Preserve the baseline collection of 180 executor tests and 157 runtime tests before duplicate consolidation.
- Run collection after each move so dropped decorators or duplicate names are caught immediately.
- Do not change assertions while moving tests.
- Do not introduce imports from one `test_*.py` module into another.
- Shared mutable fake state must be initialized per instance.
- Product code, CI workflows, and pytest configuration remain unchanged.

---

### Task 1: Extract Executor Test Support

**Files:**
- Create: `tests/tools/executor_fakes.py`
- Modify: `tests/tools/test_executor.py`

**Interfaces:**
- Produces: `FakeKube`, `FakeLogKube`, `FakeEventKube`, `FakeBridge`
- Produces: `FakeDiagnoseKube`, `ServiceDiagnosisKube`, `PVCDiagnosisKube`
- Produces: existing executor-builder and manifest-builder helpers with their current signatures

- [ ] **Step 1: Record collection and focused pass count**

```bash
uv run --no-sync pytest -p no:tach tests/tools/test_executor.py --collect-only -q
uv run --no-sync pytest -p no:tach tests/tools/test_executor.py -q
```

Expected: 180 test functions collected and all parameter cases pass.

- [ ] **Step 2: Move shared fakes without changing bodies**

Move top-level fake classes and builder helpers used by more than one future
module into `executor_fakes.py`. Keep private helpers used by only one section
beside that section.

The support module imports product interfaces directly:

```python
from typing import Any

from korvid.k8s.discovery import PODS_META
from korvid.tools.executor import ToolExecutor


def make_executor(kube: Any) -> ToolExecutor:
    return ToolExecutor(kube, {"pods": PODS_META, "pod": PODS_META})
```

Keep the concrete fake method signatures exactly; this is a move, not a new
abstraction.

- [ ] **Step 3: Update imports and verify**

```bash
uv run --no-sync pytest -p no:tach tests/tools/test_executor.py --collect-only -q
uv run --no-sync pytest -p no:tach tests/tools/test_executor.py -q
uv run --no-sync ruff check tests/tools/executor_fakes.py tests/tools/test_executor.py
uv run --no-sync mypy tests/tools/executor_fakes.py tests/tools/test_executor.py
```

Expected: the same collection count and passing result.

- [ ] **Step 4: Commit**

```bash
git add tests/tools/executor_fakes.py tests/tools/test_executor.py
git commit -m "test: extract executor test support"
```

### Task 2: Split Executor Tests by Behavior

**Files:**
- Rename: `tests/tools/test_executor.py` to `tests/tools/test_executor_core.py`
- Create: `tests/tools/test_executor_diagnosis.py`
- Create: `tests/tools/test_executor_security.py`
- Create: `tests/tools/test_executor_contracts.py`
- Reuse: `tests/tools/executor_fakes.py`

**Interfaces:**
- Consumes: executor fakes and builders from Task 1
- Produces: four independently collectible executor modules

- [ ] **Step 1: Rename the original core module**

```bash
git mv tests/tools/test_executor.py tests/tools/test_executor_core.py
```

Keep schema, identifier validation, ordinary reads, events/logs, UI dispatch,
OLM listing, resize, and write-proposal tests in the core module.

- [ ] **Step 2: Move diagnosis tests**

Move pod diagnosis beginning with `_diagnose_aliases`, service diagnosis
beginning with `ServiceDiagnosisKube`, and PVC diagnosis beginning with
`PVCDiagnosisKube` into `test_executor_diagnosis.py`. Move each private helper
with the tests that consume it.

- [ ] **Step 3: Move security tests**

Move credential masking, nested-secret, oversized-result redaction, parent
credential, deep manifest, and fail-closed malformed data tests into
`test_executor_security.py`.

- [ ] **Step 4: Move recorded-execution contracts**

Move `_ui_def`, `as_recorded`, `RecordedExecution`, result-compaction, and
recorded provenance/identity tests into `test_executor_contracts.py`.

- [ ] **Step 5: Verify exact collection**

```bash
uv run --no-sync pytest -p no:tach \
  tests/tools/test_executor_core.py \
  tests/tools/test_executor_diagnosis.py \
  tests/tools/test_executor_security.py \
  tests/tools/test_executor_contracts.py \
  --collect-only -q
```

Expected: the executor collection equals the pre-task count, adjusted only if
the duplicate-consolidation plan already removed one executor test.

- [ ] **Step 6: Run and statically check all executor modules**

```bash
uv run --no-sync pytest -p no:tach \
  tests/tools/test_executor_core.py \
  tests/tools/test_executor_diagnosis.py \
  tests/tools/test_executor_security.py \
  tests/tools/test_executor_contracts.py -q
uv run --no-sync ruff check tests/tools/executor_fakes.py tests/tools/test_executor_*.py
uv run --no-sync ruff format --check tests/tools/executor_fakes.py tests/tools/test_executor_*.py
uv run --no-sync mypy tests/tools/executor_fakes.py tests/tools/test_executor_*.py
```

Expected: all commands pass.

- [ ] **Step 7: Commit**

```bash
git add tests/tools
git commit -m "test: split executor tests by behavior"
```

### Task 3: Extract Runtime Test Support

**Files:**
- Create: `tests/agent/runtime_fakes.py`
- Modify: `tests/agent/test_runtime.py`

**Interfaces:**
- Produces: `ScriptedProvider`, `EchoExecutor`, `RaisingExecutor`
- Produces: existing collection, profile-runtime, manifest, and scripted-turn helpers with current signatures

- [ ] **Step 1: Record collection and focused result**

```bash
uv run --no-sync pytest -p no:tach tests/agent/test_runtime.py --collect-only -q
uv run --no-sync pytest -p no:tach tests/agent/test_runtime.py -q
```

Expected: 157 test functions collected and all parameter cases pass.

- [ ] **Step 2: Move only cross-section helpers**

Move `ScriptedProvider`, `EchoExecutor`, `RaisingExecutor`, and helper factories
used by multiple future modules into `runtime_fakes.py`. Keep scenario-specific
helpers, including credential and protocol doubles, with their tests.

- [ ] **Step 3: Update imports and verify**

```bash
uv run --no-sync pytest -p no:tach tests/agent/test_runtime.py --collect-only -q
uv run --no-sync pytest -p no:tach tests/agent/test_runtime.py -q
uv run --no-sync ruff check tests/agent/runtime_fakes.py tests/agent/test_runtime.py
uv run --no-sync mypy tests/agent/runtime_fakes.py tests/agent/test_runtime.py
```

Expected: collection and behavior are unchanged.

- [ ] **Step 4: Commit**

```bash
git add tests/agent/runtime_fakes.py tests/agent/test_runtime.py
git commit -m "test: extract runtime test support"
```

### Task 4: Split Runtime Tests by Behavior

**Files:**
- Rename: `tests/agent/test_runtime.py` to `tests/agent/test_runtime_core.py`
- Create: `tests/agent/test_runtime_budget_history.py`
- Create: `tests/agent/test_runtime_security.py`
- Create: `tests/agent/test_runtime_contracts.py`
- Reuse: `tests/agent/runtime_fakes.py`

**Interfaces:**
- Consumes: runtime support from Task 3
- Produces: four independently collectible runtime modules

- [ ] **Step 1: Rename the original core module**

```bash
git mv tests/agent/test_runtime.py tests/agent/test_runtime_core.py
```

Keep basic text turns, tool calls, provider failures, event emission, defaults,
and interruption lifecycle tests in the core module.

- [ ] **Step 2: Move budget and history tests**

Move request ceilings, usage estimation, tool-result budgets, profile budgets,
history trimming, and retained-turn behavior into
`test_runtime_budget_history.py`.

- [ ] **Step 3: Move security and provenance tests**

Move credential sanitization, manifest snapshots, redaction inventory,
provenance records, deep-secret handling, and provider-refusal behavior into
`test_runtime_security.py`.

- [ ] **Step 4: Move protocol and adapter contracts**

Move custom-tool execution, repeated keys, retargeting, malformed events,
done-event rules, duck adapters, and provider protocol compatibility into
`test_runtime_contracts.py`.

- [ ] **Step 5: Verify exact collection**

```bash
uv run --no-sync pytest -p no:tach \
  tests/agent/test_runtime_core.py \
  tests/agent/test_runtime_budget_history.py \
  tests/agent/test_runtime_security.py \
  tests/agent/test_runtime_contracts.py \
  --collect-only -q
```

Expected: runtime collection equals the pre-task count, adjusted only for the
duplicate-consolidation plan.

- [ ] **Step 6: Run and statically check all runtime modules**

```bash
uv run --no-sync pytest -p no:tach \
  tests/agent/test_runtime_core.py \
  tests/agent/test_runtime_budget_history.py \
  tests/agent/test_runtime_security.py \
  tests/agent/test_runtime_contracts.py -q
uv run --no-sync ruff check tests/agent/runtime_fakes.py tests/agent/test_runtime_*.py
uv run --no-sync ruff format --check tests/agent/runtime_fakes.py tests/agent/test_runtime_*.py
uv run --no-sync mypy tests/agent/runtime_fakes.py tests/agent/test_runtime_*.py
```

Expected: all commands pass.

- [ ] **Step 7: Commit**

```bash
git add tests/agent
git commit -m "test: split runtime tests by behavior"
```

### Task 5: Verify Reorganization Invariants

**Files:**
- Verify: `tests/tools/executor_fakes.py`
- Verify: `tests/tools/test_executor_*.py`
- Verify: `tests/agent/runtime_fakes.py`
- Verify: `tests/agent/test_runtime_*.py`

**Interfaces:**
- Consumes: all prior tasks
- Produces: verified collection and random-order isolation

- [ ] **Step 1: Verify no test-module imports**

```bash
rg -n 'from tests\.(tools|agent)\.test_' tests/tools tests/agent
```

Expected: no matches.

- [ ] **Step 2: Verify total collection**

```bash
uv run --no-sync pytest -p no:tach \
  tests/tools/test_executor_*.py tests/agent/test_runtime_*.py \
  --collect-only -q
```

Expected: 337 test functions before duplicate consolidation; the exact
post-consolidation count documented by that plan otherwise.

- [ ] **Step 3: Verify random-order isolation**

```bash
uv run --no-sync pytest -p no:tach \
  tests/tools/test_executor_*.py tests/agent/test_runtime_*.py -q
uv run --no-sync pytest -p no:tach \
  tests/tools/test_executor_*.py tests/agent/test_runtime_*.py -q \
  --randomly-seed=17
```

Expected: both runs pass.

- [ ] **Step 4: Verify formatting and types**

```bash
uv run --no-sync ruff check tests/tools tests/agent
uv run --no-sync ruff format --check tests/tools tests/agent
uv run --no-sync mypy tests/tools tests/agent
```

Expected: all commands pass.
