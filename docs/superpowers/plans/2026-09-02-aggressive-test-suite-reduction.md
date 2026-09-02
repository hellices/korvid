# Aggressive Test Suite Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove at least 20,000 net lines of low-value tests while retaining security invariants, public contracts, real regressions, and core user behavior.

**Architecture:** Reduce the suite in independently reviewable behavioral domains. In each domain, establish retained boundary coverage first, delete implementation-coupled and duplicate tests second, then run the complete domain suite before committing. Production code remains unchanged.

**Tech Stack:** Python 3.11+, pytest, Textual pilot tests, Node.js JavaScript harnesses, ruff, mypy, tach, Make.

## Global Constraints

- Net reduction must be at least 20,000 test lines; moving, parameterizing, or compressing assertions does not count.
- Continue beyond 20,000 lines while reviewed low-value tests remain.
- Do not change production code as part of test deletion.
- Preserve approval-gate keystroke ownership, audit fail-closed behavior, redaction, kubectl validation, release integrity, public serialization, and optional-extra boundaries.
- Retain one primary test at the strongest practical boundary for each observable behavior.
- Delete private-state, exact-call, wiring, CSS, prose, topology, fake-helper, and same-branch permutation tests.
- Run targeted checks per domain and `make check` after all domains.
- Never re-lock `uv.lock`; restore mirror-only rewrites immediately after each `uv run`.

---

### Task 1: Establish the Reduction Baseline and Critical Contract Set

**Files:**
- Read: `tests/`
- Read: `src/korvid/`
- Read: `Makefile`
- Read: `pyproject.toml`

**Interfaces:**
- Consumes: Current branch at the committed aggressive-reduction design.
- Produces: Baseline line count, collected-test count, full-gate result, and a named critical-contract command used by every later task.

- [ ] **Step 1: Confirm a clean starting point**

Run:

```bash
git status --short
git diff --check origin/main...HEAD
```

Expected: no uncommitted changes and no whitespace errors.

- [ ] **Step 2: Record line and collection baselines**

Run:

```bash
find tests -type f \( -name '*.py' -o -name '*.mjs' \) -print0 \
  | xargs -0 wc -l | tail -1
uv run --frozen --no-sync pytest --collect-only -q \
  | tail -1
git restore --worktree -- uv.lock
```

Expected: approximately 153,000 lines and approximately 9,900 collected
pytest cases. Record exact values in the task summary.

- [ ] **Step 3: Run the critical-contract baseline**

Run:

```bash
uv run --frozen --no-sync pytest -p no:tach -q \
  tests/core/test_audit.py \
  tests/core/test_redaction.py \
  tests/core/test_secrets.py \
  tests/k8s/test_client.py \
  tests/obs/test_fail_closed.py \
  tests/contract \
  tests/ui/test_protected_contexts.py \
  tests/ui/test_keybindings.py \
  tests/ui/test_confirm_screen.py \
  tests/ui/test_write_ops.py \
  tests/test_release_scripts.py
git restore --worktree -- uv.lock
```

Expected: all selected tests pass.

---

### Task 2: Remove Documentation and Composition-Root Coupling

**Files:**
- Modify: `tests/test_docs_readability.py`
- Modify: `tests/test_docs_build_config.py`
- Modify: `tests/test_main_wiring.py`
- Read and retain: `tests/test_docs_links.py`
- Read and retain: `tests/test_docs_agent_contracts.py`
- Read and retain: `tests/test_docs_media_assets.py`
- Read and retain: `tests/test_docs_landing_behavior.py`
- Read and retain: `tests/test_docs_workflow.py`
- Read and retain: `tests/test_release_scripts.py`
- Read and retain: `tests/test_release_policy.py`

**Interfaces:**
- Consumes: Working links, executable examples, immutable release inputs, provider startup errors, cleanup, and secret-safety contracts.
- Produces: A documentation and composition suite without prose, visual topology, private proxy, registry-list, or AST-wiring duplication.

- [ ] **Step 1: Prove the retained boundary tests pass**

Run:

```bash
uv run --frozen --no-sync pytest -p no:tach -q \
  tests/test_docs_links.py \
  tests/test_docs_agent_contracts.py \
  tests/test_docs_media_assets.py \
  tests/test_docs_landing_behavior.py \
  tests/test_docs_workflow.py \
  tests/test_release_policy.py \
  tests/test_agent_replacement_guard.py
git restore --worktree -- uv.lock
```

Expected: all selected tests pass.

- [ ] **Step 2: Delete prose and visual-topology contracts**

In `tests/test_docs_readability.py`, delete tests whose assertions parse exact
sentences, section counts, SVG coordinates, responsive layout, exact status-row
placement, CSS classes, or historical plan wording. Retain tests that prove
security guidance is truthful, commands are executable, and public links or
anchors resolve.

Expected deletion: about 35 tests and 1,375 lines.

- [ ] **Step 3: Delete duplicated build-shape assertions**

In `tests/test_docs_build_config.py`, delete Homebrew, installation-variant,
diagram-wiring, `.gitignore`, and MkDocs topology assertions already covered by
release-policy, link, asset, or build execution tests. Retain lock integrity,
pinned action inputs, reviewed-byte preservation, and actual script inclusion.

Expected deletion: about 13 tests and 215 lines.

- [ ] **Step 4: Delete private composition-root tests**

In `tests/test_main_wiring.py`, delete tests that assert proxy forwarding,
late-binding object shape, exact registry contents, constructor arguments,
discovery alias assembly, MCP builder calls, or AST import topology duplicated
by `tests/test_agent_replacement_guard.py`. Retain startup degradation and
operator-facing errors, provider authentication, cleanup ordering, and all
secret-safety assertions.

Expected deletion: 28-30 tests and about 2,900 lines.

- [ ] **Step 5: Verify and commit the domain**

Run:

```bash
uv run --frozen --no-sync ruff check \
  tests/test_docs_readability.py \
  tests/test_docs_build_config.py \
  tests/test_main_wiring.py
uv run --frozen --no-sync ruff format --check \
  tests/test_docs_readability.py \
  tests/test_docs_build_config.py \
  tests/test_main_wiring.py
uv run --frozen --no-sync pytest -p no:tach -q \
  tests/test_docs_*.py \
  tests/test_main_wiring.py \
  tests/test_agent_replacement_guard.py \
  tests/test_release_scripts.py \
  tests/test_release_policy.py
git restore --worktree -- uv.lock
git diff --check
```

Expected: all checks pass and the domain has at least 4,000 net deleted lines.

Commit:

```bash
git add tests
git commit -m "test: remove documentation and wiring coupling" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: Remove UI Rendering and Test-Helper Internals

**Files:**
- Delete: `tests/ui/test_navigation.py`
- Delete: `tests/ui/test_table_row_memo.py`
- Delete: `tests/ui/test_waits.py`
- Modify: `tests/ui/test_table_diff_update.py`
- Modify: `tests/ui/test_resource_table_cells.py`
- Modify: `tests/ui/test_agent_wiring.py`
- Modify: `tests/ui/test_split_pane.py`
- Modify: `tests/ui/test_transfer_picker.py`
- Retain: `tests/ui/test_protected_contexts.py`
- Retain: `tests/ui/test_keybindings.py`
- Retain: `tests/ui/test_confirm_screen.py`
- Retain: `tests/ui/test_write_ops.py`
- Retain: `tests/ui/test_agent_write.py`

**Interfaces:**
- Consumes: Textual pilot-level user actions, visible table results, scroll preservation, resource severity, approval ownership, and destructive-action confirmation.
- Produces: A UI suite that observes user behavior rather than private renderer, worker, message-routing, and fake-call mechanics.

- [ ] **Step 1: Prove retained visible behavior and safety**

Run:

```bash
uv run --frozen --no-sync pytest -p no:tach -q \
  tests/ui/test_app.py \
  tests/ui/test_protected_contexts.py \
  tests/ui/test_keybindings.py \
  tests/ui/test_confirm_screen.py \
  tests/ui/test_write_ops.py \
  tests/ui/test_agent_write.py
git restore --worktree -- uv.lock
```

Expected: all selected tests pass.

- [ ] **Step 2: Delete helper and optimization-only suites**

Delete `test_navigation.py`, `test_table_row_memo.py`, and `test_waits.py`.
Their subjects are a private navigation stack, row memoization spies, and the
test polling helper itself. Retained application tests exercise navigation,
rendered rows, and `until()` through real pilots.

Expected deletion: 371 lines and 23 tests.

- [ ] **Step 3: Reduce table diff tests to visible outcomes**

In `test_table_diff_update.py`, retain only tests that prove an added, removed,
or reordered resource appears correctly and that user scroll/cursor position
survives an update. Delete geometry walks, private cache generation, repaint
counts, width bookkeeping, private-store fallbacks, renderer probing, and
fast-path retirement metrics.

Expected deletion: about 770 lines and 28 tests.

- [ ] **Step 4: Remove exact presentation assertions**

In `test_resource_table_cells.py`, delete phase, readiness, and restart tests
that only assert exact Rich style strings. In retained usage and severity
tests, remove style-string assertions unless the severity itself is the
operator-visible warning contract. Preserve per-container limit severity,
rounding boundaries, missing metrics, and Issue #50/#51 regressions.

Expected deletion: about 182 lines and 11 tests.

- [ ] **Step 5: Remove message-routing and fake-call duplication**

Review every test in `test_agent_wiring.py`, `test_split_pane.py`, and
`test_transfer_picker.py` against its pilot-level counterpart. Delete tests
whose only result is a posted message type, exact worker call, private
reactive value, widget composition, or fake history. Retain keyboard/mouse
actions with visible results, cancellation, focus transfer, error recovery,
and accessibility behavior.

Expected deletion: at least 1,400 net lines across the three files.

- [ ] **Step 6: Verify and commit the domain**

Run:

```bash
uv run --frozen --no-sync ruff check tests/ui
uv run --frozen --no-sync ruff format --check tests/ui
uv run --frozen --no-sync pytest -p no:tach -q tests/ui
git restore --worktree -- uv.lock
git diff --check
```

Expected: all checks pass and the UI domain has at least 2,700 net deleted
lines.

Commit:

```bash
git add tests/ui
git commit -m "test: keep UI behavior instead of renderer internals" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Remove Trivial Core and Adapter Unit Duplication

**Files:**
- Delete: `tests/core/test_sorting.py`
- Delete: `tests/core/test_store.py`
- Delete: `tests/core/test_logbuffer.py`
- Modify: `tests/k8s/test_columns.py`
- Modify: `tests/k8s/test_csp.py`
- Modify: `tests/providers/test_registry.py`
- Modify: `tests/core/test_config.py`
- Retain: `tests/core/test_audit.py`
- Retain: `tests/core/test_redaction.py`
- Retain: `tests/core/test_secrets.py`
- Retain: `tests/k8s/test_models.py`
- Retain: `tests/k8s/test_client.py`
- Retain: `tests/obs/test_fail_closed.py`
- Retain: `tests/contract/`

**Interfaces:**
- Consumes: Watch-to-store integration, rendered sorting, log export, provider authentication, config security validation, Kubernetes translation, and fail-closed boundaries.
- Produces: Core and adapter coverage without trivial data-structure permutations or mock delegation.

- [ ] **Step 1: Prove stronger boundary coverage**

Run:

```bash
uv run --frozen --no-sync pytest -p no:tach -q \
  tests/core/test_watch.py \
  tests/core/test_logexport.py \
  tests/ui/test_hierarchy_screen.py \
  tests/k8s/test_client.py \
  tests/providers/test_plugin_registry.py \
  tests/contract
git restore --worktree -- uv.lock
```

Expected: all selected tests pass.

- [ ] **Step 2: Delete duplicated data-structure suites**

Delete sorting permutations, ResourceStore bucket/cache mechanics, and
LogBuffer ring mechanics after confirming the retained watch, hierarchy, and
log-export suites exercise their observable results.

Expected deletion: 717 lines and about 64 tests.

- [ ] **Step 3: Delete trivial adapter permutations**

In `test_columns.py`, retain ordered multi-column evaluation and delete
individual JSONPath/label/annotation parsing permutations. In `test_csp.py`,
retain client caching and error behavior and delete provider-prefix/label
matching permutations.

Expected deletion: about 140 lines and 20 tests.

- [ ] **Step 4: Delete provider and config same-branch cases**

In `test_registry.py`, remove duplicate `isinstance`, unavailable-provider,
and identical routing cases while preserving authentication, option
validation, and actual provider construction. In `test_config.py`, remove
default-value echoes and trivial integer/fallback parsing while preserving all
secret-key, ASCII, depth, size, persistence, and validation tests.

Expected deletion: at least 160 lines and 35 tests.

- [ ] **Step 5: Verify and commit the domain**

Run:

```bash
uv run --frozen --no-sync ruff check tests/core tests/k8s tests/providers
uv run --frozen --no-sync ruff format --check tests/core tests/k8s tests/providers
uv run --frozen --no-sync pytest -p no:tach -q \
  tests/core tests/k8s tests/providers tests/tools tests/agent tests/mcp \
  tests/obs tests/contract
uv run --frozen --no-sync tach check
git restore --worktree -- uv.lock
git diff --check
```

Expected: all checks pass and the domain has at least 1,000 net deleted lines.

Commit:

```bash
git add tests
git commit -m "test: remove duplicated core adapter coverage" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: Remove Performance-Harness Tests That Do Not Measure Korvid

**Files:**
- Delete: `tests/performance/test_metrics.py`
- Delete: `tests/performance/test_pacing.py`
- Modify: `tests/performance/test_cli.py`
- Modify: `tests/performance/test_profile.py`
- Modify: `tests/performance/test_manifests.py`
- Modify: `tests/performance/test_live.py`
- Modify: `tests/performance/test_replay.py`
- Modify: `tests/performance/test_workload.py`
- Retain: `tests/performance/test_agent_policy_boundary.py`

**Interfaces:**
- Consumes: Published 1,000-pod/24-events-per-second workload, digest convergence, replay resilience, deterministic workload hashes, and performance report artifacts.
- Produces: Performance coverage that measures product objectives rather than validating the benchmark harness.

- [ ] **Step 1: Prove product-threshold tests pass**

Run:

```bash
uv run --frozen --no-sync pytest -p no:tach -q \
  tests/performance/test_profile.py::test_aks_live_1k_profile_matches_the_published_live_plan \
  tests/performance/test_live.py::test_run_live_replay_full_happy_path_matches_cluster_digest \
  tests/performance/test_replay.py::test_replay_uses_real_app_and_reaches_expected_digest \
  tests/performance/test_agent_policy_boundary.py
git restore --worktree -- uv.lock
```

Expected: all selected tests pass.

- [ ] **Step 2: Delete metrics and pacing harness suites**

Delete `test_metrics.py` and `test_pacing.py`. Do not delete support modules
that production benchmark commands import.

Expected deletion: about 944 lines and 37 tests.

- [ ] **Step 3: Remove CLI, profile, manifest, live, and replay harness validation**

Delete tests that assert argument forwarding, parser rejection permutations,
error-message formatting, temporary file mechanics, manifest-builder
internals, topology guard implementation, churn-task bookkeeping, or synthetic
replay validation. Retain report serialization, published profile constants,
cluster digest correctness, reconnect/slow-event behavior, and workload
determinism.

Expected deletion: at least 3,200 additional lines and 85 tests.

- [ ] **Step 4: Verify and commit the domain**

Run:

```bash
uv run --frozen --no-sync ruff check tests/performance
uv run --frozen --no-sync ruff format --check tests/performance
uv run --frozen --no-sync pytest -p no:tach -q tests/performance
git restore --worktree -- uv.lock
git diff --check
```

Expected: all checks pass and the domain has at least 4,100 net deleted lines.

Commit:

```bash
git add tests/performance
git commit -m "test: remove benchmark harness self-tests" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: Collapse Evaluation Syntax Variants to Semantic Decisions

**Files:**
- Modify: `tests/evals/test_operation_grader.py`
- Modify: `tests/evals/test_operation_journeys.py`
- Modify: `tests/evals/test_operation_outcome.py`
- Modify: `tests/evals/test_operation_schema.py`
- Modify: `tests/evals/test_runner.py`
- Modify: `tests/evals/test_journeys_cli.py`
- Modify: `tests/evals/test_cli.py`
- Modify: `tests/evals/test_operation_journal.py`
- Modify: `tests/evals/test_operation_state.py`
- Modify: `tests/evals/test_grader.py`
- Retain: `tests/evals/test_tool_surface_arm.py`
- Retain: `tests/evals/test_operation_import_boundary.py`

**Interfaces:**
- Consumes: Distinct truthfulness, negation scope, causal boundary, evidence matching, journey completion, report serialization, and tool-surface decisions.
- Produces: One representative true and false case per semantic branch instead of many grammar, target-name, and error-plumbing variants.

- [ ] **Step 1: Prove core semantic coverage**

Run:

```bash
uv run --frozen --no-sync pytest -p no:tach -q \
  tests/evals/test_grader.py \
  tests/evals/test_tool_surface_arm.py \
  tests/evals/test_operation_import_boundary.py
git restore --worktree -- uv.lock
```

Expected: all selected tests pass.

- [ ] **Step 2: Reduce operation grading and outcome variants**

For each parametrized group in `test_operation_grader.py`,
`test_operation_journeys.py`, and `test_operation_outcome.py`, retain one
representative for each distinct branch outcome. Delete variants that differ
only in grammar, resource name, namespace, clause ordering that normalizes to
the same token sequence, or auxiliary failed action with the same score.
Preserve causal-boundary, negation-scope, wrong-target, and partial-success
decisions.

Expected deletion: at least 1,350 lines and 70 tests.

- [ ] **Step 3: Remove schema, runner, CLI, journal, and state plumbing**

Delete repeated invalid-input cases selecting the same validation branch,
configuration forwarding, parser error formatting, and private state
transitions. Preserve typed target identity, checkpoint ordering, report wire
format, audit-trail output, cancellation, and state snapshot correctness.

Expected deletion: at least 2,000 lines and 100 tests.

- [ ] **Step 4: Reduce diagnosis grader normalization variants**

In `test_grader.py`, retain distinct grading decisions and remove repeated
camel-case, spacing, punctuation, and keyword-normalization examples after one
positive and one negative representative remain.

Expected deletion: at least 500 lines and 15 tests.

- [ ] **Step 5: Continue the eval pass until every remaining case names a distinct semantic branch**

Review the remaining files under `tests/evals/` in descending line-count
order. For each case, state the branch it uniquely protects; delete it if an
already retained case names the same branch and differs only in fixture data
or syntax.

Expected total eval deletion: at least 5,000 net lines and 290 tests.

- [ ] **Step 6: Verify and commit the domain**

Run:

```bash
uv run --frozen --no-sync ruff check tests/evals
uv run --frozen --no-sync ruff format --check tests/evals
uv run --frozen --no-sync pytest -p no:tach -q tests/evals
git restore --worktree -- uv.lock
git diff --check
```

Expected: all checks pass and the domain has at least 5,000 net deleted lines.

Commit:

```bash
git add tests/evals
git commit -m "test: keep evaluation semantics over syntax variants" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: Reach the Floor Without Gaming the Metric

**Files:**
- Modify: remaining low-value files under `tests/ui/`, `tests/evals/`, and root `tests/test_*.py`

**Interfaces:**
- Consumes: Net line reductions from Tasks 2-6.
- Produces: At least 20,000 net deleted test lines with no relocated or compressed equivalent assertions.

- [ ] **Step 1: Measure the current net reduction**

Run:

```bash
git diff --numstat 85518e6f..HEAD -- tests \
  | awk '{ added += $1; deleted += $2 } END { print added, deleted, deleted-added }'
```

Expected: the third number is at least 16,800 before this pass.

- [ ] **Step 2: Review the remaining highest-coupling files**

Run:

```bash
rg -l 'query_one|monkeypatch\.setattr|assert_awaited_once_with|assert_called_once_with|private|_spy' \
  tests/ui tests/evals tests/test_*.py \
  | xargs wc -l | sort -nr | head -20
```

For each returned file, delete tests whose only observable is private state,
exact call shape, fake history, widget topology, or an already retained
semantic branch. Do not modify the critical-contract files from Task 1.

- [ ] **Step 3: Re-measure and stop only after both conditions hold**

Run:

```bash
git diff --numstat 85518e6f..HEAD -- tests \
  | awk '{ added += $1; deleted += $2 } END { print "added=" added, "deleted=" deleted, "net=" deleted-added }'
```

Expected: `net` is at least 20,000 and no reviewed low-value test remains in
the 20 files from Step 2.

- [ ] **Step 4: Verify and commit the final reduction batch**

Run:

```bash
uv run --frozen --no-sync ruff check tests
uv run --frozen --no-sync ruff format --check tests
uv run --frozen --no-sync pytest -p no:tach -q tests/ui tests/evals tests/test_*.py
git restore --worktree -- uv.lock
git diff --check
```

Expected: all checks pass.

Commit:

```bash
git add tests
git commit -m "test: remove remaining implementation-coupled coverage" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 8: Run the Full Gate and Report the Result

**Files:**
- Read: `tests/`
- Read: `src/korvid/`

**Interfaces:**
- Consumes: All committed domain reductions.
- Produces: Fresh full-gate evidence, final reduction totals, and a clean branch.

- [ ] **Step 1: Run the full repository gate**

Run:

```bash
make check
```

Expected: ruff, format, mypy, tach, deptry, pytest, and coverage all pass.

- [ ] **Step 2: Restore mirror pollution and confirm repository integrity**

Run:

```bash
git restore --worktree -- uv.lock
git status --short
git diff --check origin/main...HEAD
```

Expected: clean working tree and no whitespace errors.

- [ ] **Step 3: Record final measurements**

Run:

```bash
find tests -type f \( -name '*.py' -o -name '*.mjs' \) -print0 \
  | xargs -0 wc -l | tail -1
uv run --frozen --no-sync pytest --collect-only -q | tail -1
git restore --worktree -- uv.lock
git diff --numstat 85518e6f..HEAD -- tests \
  | awk '{ added += $1; deleted += $2 } END { print "added=" added, "deleted=" deleted, "net=" deleted-added }'
git status --short
```

Expected: at least 20,000 net deleted test lines, materially fewer collected
tests, and no uncommitted lockfile change.

- [ ] **Step 4: Review the complete diff**

Run a code review over `85518e6f..HEAD` and reject any deletion that weakens a
security, release, public serialization, compatibility, or user-visible
contract without a retained boundary test.

Expected: no Critical or Important findings.
