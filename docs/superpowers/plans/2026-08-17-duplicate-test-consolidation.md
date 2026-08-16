# Duplicate Test Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate truly redundant test scenarios while retaining identical-looking bodies that validate distinct security, layer, or regression contracts.

**Architecture:** Use parametrization only where inputs and expected behavior are the sole differences. Preserve separate names when execution layer, fixture domain, or regression purpose differs.

**Tech Stack:** Python 3.13, pytest.

## Global Constraints

- Do not consolidate solely because AST-normalized bodies match.
- Parameter IDs must describe the rejected or accepted behavior.
- Preserve distinct redaction, transfer-permission, runtime-adapter, and fail-closed executor contracts.
- Product code remains unchanged.

---

### Task 1: Remove the Redundant PVC Grader Scenario

**Files:**
- Modify: `tests/evals/test_grader.py:532`
- Modify: `tests/evals/test_grader.py:615`

**Interfaces:**
- Consumes: existing `Evidence`, `_scenario`, `_record`, and `grade`
- Produces: one canonical PVC diagnostic evidence test

- [ ] **Step 1: Confirm both tests are identical and passing**

```bash
uv run --no-sync pytest -p no:tach \
  tests/evals/test_grader.py::test_grade_credits_diagnose_pvc_against_name_keyed_evidence \
  tests/evals/test_grader.py::test_grade_still_credits_a_diagnostic_call_for_its_own_kind -q
```

Expected: two tests pass.

- [ ] **Step 2: Keep one behaviorally named test**

Retain a single function:

```python
def test_grade_credits_diagnostic_pvc_for_its_name_keyed_evidence() -> None:
    evidence = Evidence(
        tool="get_resource",
        contains="phase: Pending",
        args={"kind": "persistentvolumeclaims", "name": "data", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="diagnose_pvc",
            result="outcome: findings\nphase: Pending",
            arguments={"pvc": "data", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert result.evidence_fetched
```

Delete the second identical scenario; do not parametrize identical data merely
to preserve the old test count.

- [ ] **Step 3: Verify**

```bash
uv run --no-sync pytest -p no:tach tests/evals/test_grader.py -q
uv run --no-sync ruff check tests/evals/test_grader.py
uv run --no-sync ruff format --check tests/evals/test_grader.py
```

Expected: all commands pass and collection decreases by one.

- [ ] **Step 4: Commit**

```bash
git add tests/evals/test_grader.py
git commit -m "test: remove duplicate PVC grading scenario"
```

### Task 2: Consolidate Window Validation Inputs

**Files:**
- Modify: `tests/obs/test_connector.py:75-84`

**Interfaces:**
- Consumes: existing window-resolution API and exception contract
- Produces: one parameterized invalid-window test

- [ ] **Step 1: Run the two existing tests**

```bash
uv run --no-sync pytest -p no:tach \
  tests/obs/test_connector.py -k 'non_positive_window or non_integer_window' -q
```

Expected: all invalid-window cases pass.

- [ ] **Step 2: Merge the parameter tables**

Replace the two functions with:

```python
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="zero"),
        pytest.param(-5, id="negative"),
        pytest.param(1.5, id="float"),
        pytest.param("30", id="string"),
        pytest.param(True, id="boolean"),
    ],
)
def test_an_invalid_window_is_refused(self, value: object) -> None:
    with pytest.raises(ConnectorError, match="minutes"):
        resolve_window(value, QueryLimits())
```

- [ ] **Step 3: Verify**

```bash
uv run --no-sync pytest -p no:tach tests/obs/test_connector.py -q
uv run --no-sync ruff check tests/obs/test_connector.py
uv run --no-sync ruff format --check tests/obs/test_connector.py
```

Expected: all commands pass; the case count is unchanged.

- [ ] **Step 4: Commit**

```bash
git add tests/obs/test_connector.py
git commit -m "test: consolidate invalid connector windows"
```

### Task 3: Verify Intentional Identical Bodies Remain Distinct

**Files:**
- Verify: `tests/core/test_redaction.py`
- Verify: `tests/core/test_transfer.py`
- Verify: `tests/agent/test_runtime.py`
- Verify: `tests/agent/test_provider_plugin.py`
- Verify: `tests/tools/test_executor.py`

**Interfaces:**
- Consumes: all prior tasks
- Produces: a reviewed duplicate-body report

- [ ] **Step 1: Re-run the AST duplicate-body report**

Use the same docstring-stripping AST normalization from the baseline review.

Expected remaining pairs:

- credential vocabulary versus AWS-specific redaction boundary;
- general non-credential names versus neighboring AWS names;
- basic transfer validation versus permission-aware writable paths;
- string-only executor compatibility versus caller-composed adapter;
- internal done-check error preservation versus public double-done validation;
- malformed provider event shape versus valid-shape values exceeding bounds;
- recorded-execution redaction failure versus malformed-manifest fail-closed behavior.

- [ ] **Step 2: Run all touched tests**

```bash
uv run --no-sync pytest -p no:tach \
  tests/evals/test_grader.py tests/obs/test_connector.py \
  tests/agent/test_provider_plugin.py tests/agent/test_runtime.py \
  tests/core/test_redaction.py tests/core/test_transfer.py \
  tests/tools/test_executor.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run static checks**

```bash
uv run --no-sync ruff check \
  tests/evals/test_grader.py tests/obs/test_connector.py \
  tests/agent/test_provider_plugin.py
uv run --no-sync ruff format --check \
  tests/evals/test_grader.py tests/obs/test_connector.py \
  tests/agent/test_provider_plugin.py
```

Expected: all commands pass.
