# UI Wait Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace non-semantic fixed-duration UI waits with observable condition polling while preserving tests whose subject is elapsed time.

**Architecture:** Reuse `tests.ui.waits.until(pilot, cond, timeout=5.0, label="condition")`. Each migrated wait names the state transition it observes; cadence, debounce, and delayed-cleanup tests retain numeric waits because time is their input.

**Tech Stack:** Python 3.13, pytest, pytest-asyncio, Textual Pilot.

## Global Constraints

- Do not change product code solely to expose a test condition.
- Preserve time-based waits when elapsed time is the behavior under test.
- Never replace a fixed wait with a larger fixed wait.
- Every `until` call has a specific `label`.
- Run UI tests with `-p no:tach`.

---

### Task 1: Remove the Duplicate Polling Helper

**Files:**
- Modify: `tests/ui/test_metrics_wiring.py:80`
- Reuse: `tests/ui/waits.py:23`

**Interfaces:**
- Consumes: `until(pilot: Any, cond: Callable[[], object], timeout: float = 5.0, label: str = "condition") -> None`
- Produces: no new interface

- [ ] **Step 1: Record the current focused result**

Run:

```bash
uv run --no-sync pytest -p no:tach tests/ui/test_metrics_wiring.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Replace the local helper**

Delete `_until` and import the shared helper:

```python
from tests.ui.waits import until
```

Convert each call from:

```python
await _until(pilot, lambda: bool(calls))
```

to:

```python
await until(pilot, lambda: bool(calls), label="metrics poll recorded")
```

- [ ] **Step 3: Verify the focused file**

Run:

```bash
uv run --no-sync pytest -p no:tach tests/ui/test_metrics_wiring.py -q
uv run --no-sync ruff check tests/ui/test_metrics_wiring.py
uv run --no-sync ruff format --check tests/ui/test_metrics_wiring.py
```

Expected: all commands pass.

- [ ] **Step 4: Commit**

```bash
git add tests/ui/test_metrics_wiring.py
git commit -m "test: reuse deterministic UI wait helper"
```

### Task 2: Migrate the Four Largest Fixed-Wait Contributors

**Files:**
- Modify: `tests/ui/test_log_pane.py`
- Modify: `tests/ui/test_app.py`
- Modify: `tests/ui/test_shell.py`
- Modify: `tests/ui/test_drilldown.py`

**Interfaces:**
- Consumes: `tests.ui.waits.until`
- Produces: observable predicates local to each test

- [ ] **Step 1: Capture the batch baseline**

Run:

```bash
uv run --no-sync pytest -p no:tach \
  tests/ui/test_log_pane.py \
  tests/ui/test_app.py \
  tests/ui/test_shell.py \
  tests/ui/test_drilldown.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Convert action-settling waits**

After commands, key presses, and worker starts, poll the asserted state directly.
Use patterns such as:

```python
await until(pilot, lambda: table.row_count == 1, label="filtered row rendered")
await until(pilot, lambda: app.current_kind == "deployments", label="deployment view active")
await until(pilot, lambda: app.screen is expected_screen, label="screen opened")
await until(pilot, lambda: "findme" in _richlog_text(app), label="search result rendered")
```

If the assertion immediately following a wait is already a predicate, move that
predicate into `until` and retain the assertion only when it verifies additional
detail.

- [ ] **Step 3: Keep time semantics explicit**

Retain numeric waits only in tests whose names or assertions verify polling
cadence, debounce, delayed teardown, absence during a time window, or repeated
activity over time. Do not retain a wait merely because the predicate is
inconvenient.

- [ ] **Step 4: Verify the batch**

Run the Task 2 pytest command, then:

```bash
uv run --no-sync ruff check \
  tests/ui/test_log_pane.py tests/ui/test_app.py \
  tests/ui/test_shell.py tests/ui/test_drilldown.py
uv run --no-sync ruff format --check \
  tests/ui/test_log_pane.py tests/ui/test_app.py \
  tests/ui/test_shell.py tests/ui/test_drilldown.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit**

```bash
git add tests/ui/test_log_pane.py tests/ui/test_app.py \
  tests/ui/test_shell.py tests/ui/test_drilldown.py
git commit -m "test: replace fixed waits in core UI flows"
```

### Task 3: Migrate Write and Navigation Flows

**Files:**
- Modify: `tests/ui/test_write_ops.py`
- Modify: `tests/ui/test_agent_write.py`
- Modify: `tests/ui/test_hierarchy_nav.py`
- Modify: `tests/ui/test_node_ops.py`
- Modify: `tests/ui/test_describe.py`
- Modify: `tests/ui/test_resize_flow.py`
- Modify: `tests/ui/test_node_shell.py`

**Interfaces:**
- Consumes: `tests.ui.waits.until`
- Produces: no new shared interface

- [ ] **Step 1: Run the focused batch before edits**

```bash
uv run --no-sync pytest -p no:tach \
  tests/ui/test_write_ops.py tests/ui/test_agent_write.py \
  tests/ui/test_hierarchy_nav.py tests/ui/test_node_ops.py \
  tests/ui/test_describe.py tests/ui/test_resize_flow.py \
  tests/ui/test_node_shell.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Replace non-semantic waits**

Use approval-screen visibility, bridge call counts, selected resource identity,
screen-stack state, and completed subprocess calls as predicates:

```python
await until(pilot, lambda: len(bridge.calls) == 1, label="approved write dispatched")
await until(pilot, lambda: app.screen.__class__ is ConfirmScreen, label="confirmation opened")
await until(pilot, lambda: selected_name(table) == expected, label="expected resource selected")
```

Import existing local accessors rather than introducing sleeps or reaching into
new product internals.

- [ ] **Step 3: Run pytest and formatting**

Run the Task 3 pytest command and ruff check/format on the seven files.

Expected: all commands pass.

Note: the vacuous-capture mutation probe was temporary evidence only; it is
not retained in the committed suite.

- [ ] **Step 4: Commit**

```bash
git add tests/ui/test_write_ops.py tests/ui/test_agent_write.py \
  tests/ui/test_hierarchy_nav.py tests/ui/test_node_ops.py \
  tests/ui/test_describe.py tests/ui/test_resize_flow.py \
  tests/ui/test_node_shell.py
git commit -m "test: make write and navigation waits condition based"
```

### Task 4: Migrate the Remaining Fixed-Wait Files

**Files:**
- Modify: `tests/ui/test_containers_screen.py`
- Modify: `tests/ui/test_dryrun_preview.py`
- Modify: `tests/ui/test_hint_wiring.py`
- Modify: `tests/ui/test_helm_view.py`
- Modify: `tests/ui/test_agent_interrupt.py`
- Modify: `tests/ui/test_agent_wiring.py`
- Modify: `tests/ui/test_ctx_switch.py`

**Interfaces:**
- Consumes: `tests.ui.waits.until`
- Produces: no new shared interface

- [ ] **Step 1: Run the focused batch before edits**

```bash
uv run --no-sync pytest -p no:tach \
  tests/ui/test_containers_screen.py tests/ui/test_dryrun_preview.py \
  tests/ui/test_hint_wiring.py tests/ui/test_helm_view.py \
  tests/ui/test_agent_interrupt.py tests/ui/test_agent_wiring.py \
  tests/ui/test_ctx_switch.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Replace observable waits and retain time contracts**

Apply the same direct-predicate rule. For negative assertions, wait for the
positive prerequisite first and then assert the forbidden state is absent; do
not poll a condition that is true before the action begins.

- [ ] **Step 3: Verify the batch**

Run the Task 4 pytest command and ruff check/format on the seven files.

Expected: all commands pass.

- [ ] **Step 4: Commit**

```bash
git add tests/ui/test_containers_screen.py tests/ui/test_dryrun_preview.py \
  tests/ui/test_hint_wiring.py tests/ui/test_helm_view.py \
  tests/ui/test_agent_interrupt.py tests/ui/test_agent_wiring.py \
  tests/ui/test_ctx_switch.py
git commit -m "test: finish deterministic UI wait migration"
```

### Task 5: Measure and Validate the UI Migration

**Files:**
- Verify: `tests/ui/`

**Interfaces:**
- Consumes: all prior tasks
- Produces: before/after wait metrics

- [ ] **Step 1: Measure remaining numeric waits**

```bash
rg -n 'pilot\.pause\([0-9]' tests/ui -g '*.py'
```

Expected: every remaining match tests elapsed-time behavior; no generic
action-settling wait remains.

- [ ] **Step 2: Run all UI tests**

```bash
uv run --no-sync pytest -p no:tach tests/ui -q
```

Expected: all UI tests pass.

- [ ] **Step 3: Run static checks**

```bash
uv run --no-sync ruff check tests/ui
uv run --no-sync ruff format --check tests/ui
uv run --no-sync mypy tests/ui
```

Expected: all commands pass.

- [ ] **Step 4: Record final metrics in the commit body**

Count numeric waits and their encoded duration using the same commands as the
baseline review. In the report, say the last 11 generic sleeps were removed,
while one intentional `0.3s` absence window remains. Include the before values
`531 calls` and `66.20 seconds`, the measured after values, and note that the
regex-vs-AST delta is a `tests/ui/waits.py` docstring false positive.

- [ ] **Step 5: Commit any final import-only cleanup**

```bash
git add tests/ui
git commit -m "test: complete deterministic UI wait cleanup"
```

Skip this commit if Task 5 produces no changes.
